"""Offline tests for the terminal channel and the keyboard mapping.

No device and no pytest required:

    .venv/bin/python test_terminal.py

The key mapping is checked without a QApplication (translate_key takes plain
ints). The channel is driven against a scripted session, so the flow-control
ack -- the thing that makes /export hang when it is wrong -- is asserted.
"""

import threading
import time

from x3.proto import message as m
from x3.proto.modern import ModernSession
from x3.proto.terminal import TerminalChannel

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def ok(label, cond):
    if not cond:
        failures.append(label)


def wait_for(pred, secs=2.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


TERM_FROM = (0, 201)


class FakeSession(ModernSession):
    """ModernSession with the socket replaced by a scripted frame queue."""

    def __init__(self):
        super().__init__("192.0.2.1")
        self._authenticated = True
        self.timeout = 1.0
        self._q = []
        self._cond = threading.Condition()
        self.sent = []
        self.dead = False

    def _recv_message(self):
        with self._cond:
            while not self._q and not self.dead:
                self._cond.wait(2.0)
            if self.dead:
                raise ConnectionError("peer closed during read")
            return self._q.pop(0)

    def _send_encrypted(self, payload):
        self.sent.append(m.Message.from_bytes(payload))

    def feed(self, *msgs):
        with self._cond:
            self._q.extend(msgs)
            self._cond.notify_all()


def open_reply(sid):
    msg = m.Message()
    msg.add_u32_array(m.SYS_TO, list(TERM_FROM))
    msg.add_u32_array(m.SYS_FROM, [76])
    msg.set_command(0xA0065)
    msg.set_request_id(1)
    msg.add_u32(0xFE0001, sid)
    return msg


def output(data):
    msg = m.Message()
    msg.add_u32_array(m.SYS_TO, list(TERM_FROM))
    msg.add_u32_array(m.SYS_FROM, [76])
    msg.set_command(0xA0067)
    msg.add_raw(0x000002, data)
    return msg


# --- the channel: open, ack, input, winch, close -----------------------------

sess = FakeSession()
sess._sock = type("S", (), {
    "gettimeout": lambda self: 10.0, "settimeout": lambda self, v: None,
    "shutdown": lambda self, h: None, "close": lambda self: None,
})()
demux = sess.start_demux()
got: list[bytes] = []
closed: list = []
chan = TerminalChannel(
    sess, password="secret", from_path=TERM_FROM, cols=100, rows=24,
    on_output=got.append, on_closed=closed.append,
)
try:
    threading.Thread(
        target=lambda: (time.sleep(0.05), sess.feed(open_reply(42))), daemon=True
    ).start()
    check("open returns the device's session id", chan.open(), 42)

    opened = sess.sent[0]
    check("open goes to handler 76", opened.find(m.SYS_TO)[1], [76])
    check("open uses our own SYS_FROM", opened.find(m.SYS_FROM)[1], list(TERM_FROM))
    check("open carries cols/rows", (opened.get_u32(0x05), opened.get_u32(0x06)), (100, 24))

    chan.start_reader()
    sess.feed(output(b"banner\r\n"), output(b"[admin@lab] > "))
    ok("output is handed up", wait_for(lambda: len(got) == 2))
    check("output arrives verbatim", b"".join(got), b"banner\r\n[admin@lab] > ")
    check("bytes_in counts them", chan.bytes_in, len(b"banner\r\n[admin@lab] > "))

    # The ack is cumulative over the session, and one goes out per chunk. This
    # is what stops the device stalling at ~8 KB (see terminal.py's docstring).
    acks = [
        f.get_u32(0x000003) for f in sess.sent
        if f.get_u32(m.SYS_COMMAND) == 0xA0067 and f.get_raw(0x000002) is None
    ]
    check("one ack per received chunk", len(acks), 2)
    check("the ack is cumulative", acks, [8, 22])

    chan.write(b"/export\r")
    inp = [f for f in sess.sent if f.get_raw(0x000002) == b"/export\r"]
    check("input is sent as raw bytes", len(inp), 1)
    check("input carries no request id", inp[0].find(m.SYS_REQUEST_ID), None)
    check("bytes_out counts it", chan.bytes_out, 8)

    chan.resize(120, 40)
    winch = [f for f in sess.sent if f.get_u32(m.SYS_COMMAND) == 0xA0068]
    check("resize sends a winch", len(winch), 1)
    check("winch carries the new size",
          (winch[0].get_u32(0x05), winch[0].get_u32(0x06)), (120, 40))
    chan.resize(120, 40)
    check("an unchanged size sends nothing",
          len([f for f in sess.sent if f.get_u32(m.SYS_COMMAND) == 0xA0068]), 1)

    # There is no terminal-close command, so closing asks the router to quit.
    chan.close(grace=0.05)
    quits = [f for f in sess.sent if f.get_raw(0x000002) == b"/quit\r"]
    check("close asks the console to quit", len(quits), 1)
    check("close clears the session id", chan.sid, None)
    ok("the reader reported it stopped", wait_for(lambda: len(closed) == 1))
finally:
    sess.stop_demux()

# Two consoles on one session must not see each other's output.
sess2 = FakeSession()
sess2._sock = type("S", (), {
    "gettimeout": lambda self: 10.0, "settimeout": lambda self, v: None,
    "shutdown": lambda self, h: None, "close": lambda self: None,
})()
sess2.start_demux()
a_out, b_out = [], []
a = TerminalChannel(sess2, password="p", from_path=(0, 301), on_output=a_out.append)
b = TerminalChannel(sess2, password="p", from_path=(0, 302), on_output=b_out.append)
try:
    def answer(path, sid, delay):
        time.sleep(delay)
        msg = m.Message()
        msg.add_u32_array(m.SYS_TO, list(path))
        msg.add_u32_array(m.SYS_FROM, [76])
        msg.set_command(0xA0065)
        msg.set_request_id(1 if sid == 7 else 2)
        msg.add_u32(0xFE0001, sid)
        sess2.feed(msg)

    threading.Thread(target=answer, args=((0, 301), 7, 0.05), daemon=True).start()
    a.open()
    a.start_reader()
    threading.Thread(target=answer, args=((0, 302), 8, 0.05), daemon=True).start()
    b.open()
    b.start_reader()

    def out_for(path, data):
        msg = m.Message()
        msg.add_u32_array(m.SYS_TO, list(path))
        msg.add_u32_array(m.SYS_FROM, [76])
        msg.set_command(0xA0067)
        msg.add_raw(0x000002, data)
        return msg

    sess2.feed(out_for((0, 301), b"for-a"), out_for((0, 302), b"for-b"))
    ok("both consoles got their own output",
       wait_for(lambda: a_out == [b"for-a"] and b_out == [b"for-b"]))
    check("distinct session ids", (a.sid, b.sid), (7, 8))
finally:
    a.close(quit_cmd=False)
    b.close(quit_cmd=False)
    sess2.stop_demux()

# --- the keyboard mapping ---------------------------------------------------

from PySide6.QtCore import Qt  # noqa: E402

from x3.ui.widgets.terminal_window import translate_key  # noqa: E402

NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
K = Qt.Key

cases = [
    ("Enter sends CR, not LF", K.Key_Return, NONE, "\r", b"\r"),
    ("Tab completes", K.Key_Tab, NONE, "\t", b"\t"),
    ("Shift+Tab", K.Key_Backtab, NONE, "", b"\x1b[Z"),
    ("Up recalls history", K.Key_Up, NONE, "", b"\x1b[A"),
    ("Down", K.Key_Down, NONE, "", b"\x1b[B"),
    ("Right", K.Key_Right, NONE, "", b"\x1b[C"),
    ("Left", K.Key_Left, NONE, "", b"\x1b[D"),
    ("Home", K.Key_Home, NONE, "", b"\x1b[H"),
    ("End", K.Key_End, NONE, "", b"\x1b[F"),
    ("PageUp", K.Key_PageUp, NONE, "", b"\x1b[5~"),
    ("PageDown", K.Key_PageDown, NONE, "", b"\x1b[6~"),
    ("Delete", K.Key_Delete, NONE, "", b"\x1b[3~"),
    ("Backspace", K.Key_Backspace, NONE, "\b", b"\x7f"),
    ("Escape", K.Key_Escape, NONE, "\x1b", b"\x1b"),
    ("F1 opens help", K.Key_F1, NONE, "", b"\x1bOP"),
    ("Ctrl+C interrupts", K.Key_C, CTRL, "\x03", b"\x03"),
    ("Ctrl+D exits", K.Key_D, CTRL, "\x04", b"\x04"),
    ("plain text passes through", K.Key_A, NONE, "a", b"a"),
    ("utf-8 passes through", K.Key_unknown, NONE, "é", b"\xc3\xa9"),
    ("a bare modifier sends nothing", K.Key_Shift, NONE, "", b""),
]
for label, key, mods, text, want in cases:
    check(f"key: {label}", translate_key(int(key), mods, text), want)

# --- the window: pyte screen, key forwarding, winch, teardown ---------------
# Forced offscreen so this runs without a display.

import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF  # noqa: E402
from PySide6.QtGui import QKeyEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from x3.ui import style as ui_style  # noqa: E402

# scale=1.0 explicitly: without it the theme comes up at whatever zoom is
# saved in the developer's own config, and the window floor (which scales)
# would move under the size checks below.
ui_style.apply_theme(app, "classic", scale=1.0, persist=False)

from x3.proto.device import DeviceClient  # noqa: E402
from x3.ui.widgets.terminal_window import TerminalWindow  # noqa: E402

PROMPT = b"\x1b[9999B\x1b[K[\x1b[m\x1b[36madmin\x1b[m@lab] > "


def pump(secs=0.4):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


class EchoSession(FakeSession):
    """Answers a terminal open and echoes whatever is typed, like a console."""

    def _send_encrypted(self, payload):
        msg = m.Message.from_bytes(payload)
        self.sent.append(msg)
        if msg.get_u32(m.SYS_COMMAND) == 0xA0065:
            frm = msg.find(m.SYS_FROM)[1]
            reply = m.Message()
            reply.add_u32_array(m.SYS_TO, frm)
            reply.add_u32_array(m.SYS_FROM, [76])
            reply.set_command(0xA0065)
            reply.set_request_id(msg.find(m.SYS_REQUEST_ID)[1])
            reply.add_u32(0xFE0001, 99)
            self.feed(reply)
            self.push(frm, PROMPT)
        data = msg.get_raw(0x000002)
        if data and msg.get_u32(m.SYS_COMMAND) == 0xA0067 and bytes(data) != b"/quit\r":
            self.push(msg.find(m.SYS_FROM)[1], bytes(data))

    def push(self, to, data):
        out = m.Message()
        out.add_u32_array(m.SYS_TO, list(to))
        out.add_u32_array(m.SYS_FROM, [76])
        out.set_command(0xA0067)
        out.add_raw(0x000002, data)
        self.feed(out)


class StubClient(DeviceClient):
    def __init__(self, session):
        self.session = session
        self.password = "secret"
        self.client_ident = None
        self.session_log = None
        self._terminals = []
        self._from_seq = 200
        self._lock = threading.RLock()


es = EchoSession()
es._sock = type("S", (), {
    "gettimeout": lambda self: 10.0, "settimeout": lambda self, v: None,
    "shutdown": lambda self, h: None, "close": lambda self: None,
})()
es.start_demux()
client = StubClient(es)
win = TerminalWindow(client, seq=1)
win.show()
pump(0.5)
try:
    check("the window opened a console", win._channel.sid, 99)
    # The reported bug: the caret used to sit in the wrong place. The router ends
    # every prompt with \x1b[9999B, which pyte clamps to the bottom margin.
    cx, cy = win.view.cursor_pos()
    row = win.view.screen_text().split("\n")[cy]
    ok("the caret sits just past the prompt", row[:cx].endswith("> "))

    for chx in "/int":
        win.view.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, ord(chx.upper()), NONE, chx)
        )
    pump(0.3)
    ok("typing reaches the screen", "/int" in win.view.screen_text())

    # Tab has to be caught in event(): the focus chain takes it first.
    tabs_before = len([f for f in es.sent if f.get_raw(0x000002) == b"\t"])
    win.view.event(QKeyEvent(QEvent.Type.KeyPress, int(K.Key_Tab), NONE, "\t"))
    pump(0.2)
    check("Tab is forwarded, not consumed by the focus chain",
          len([f for f in es.sent if f.get_raw(0x000002) == b"\t"]) - tabs_before, 1)

    win.view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(K.Key_C), CTRL, "\x03"))
    pump(0.2)
    ok("Ctrl+C interrupts", any(f.get_raw(0x000002) == b"\x03" for f in es.sent))

    # Escape is a terminal key here, so it must not close the window.
    win.view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(K.Key_Escape), NONE, "\x1b"))
    pump(0.2)
    ok("Escape goes to the device", any(f.get_raw(0x000002) == b"\x1b" for f in es.sent))
    ok("Escape did not close the console", win._channel is not None)

    # Rapid resizes coalesce: the 120ms debounce means dragging a window edge
    # must not emit a winch per pixel. (Standalone, 15 resizes give exactly 1;
    # inside a layout the view is re-laid between them, so allow a few.)
    drags = list(range(500, 760, 20))
    for width in drags:
        win.view.resize(width, 260)
        app.processEvents()
    pump(0.5)
    winches = [f for f in es.sent if f.get_u32(m.SYS_COMMAND) == 0xA0068]
    ok(f"resizes coalesce ({len(drags)} resizes -> {len(winches)} winches)",
       0 < len(winches) <= 4)
    check("the last winch carries the current size",
          (winches[-1].get_u32(0x05), winches[-1].get_u32(0x06)),
          (win.view._cols, win.view._rows))

    # The /export stress case: many frames, one coalesced feed per tick.
    bulk = b"".join(f"line {i:04d} of export output\r\n".encode() for i in range(400))
    es.push((0, 200), bulk)
    pump(0.8)
    check("bulk output is fully consumed", len(win._inbox), 0)
    ok("the screen shows the tail", "line 0399" in win.view.screen_text())

    # --- selection, copy, clear ------------------------------------------
    # A terminal is where you read output you then want to paste somewhere.
    # There was no way to get text out of it at all: no mouse handling, no
    # menu, and Ctrl+C is SIGINT here rather than Copy.
    view = win.view
    clip = QApplication.clipboard()
    clip.clear()

    ok("nothing is selected to begin with", not view.has_selection())
    check("...so there is nothing to copy", view.copy_selection(), False)

    view.select_all()
    ok("Select All holds something", view.has_selection())
    ok("...the whole visible screen",
       "line 0399" in view.selected_text())
    ok("copying puts it on the clipboard", view.copy_selection())
    ok("...as the text that was on screen", "line 0399" in clip.text())

    view.clear_selection()
    ok("the selection can be dropped", not view.has_selection())

    # A drag selects in reading order, whichever way it is dragged, and
    # trailing blanks are trimmed per line the way a terminal does it.
    # Coordinates are document lines, not screen rows.
    last = view.doc_length() - 1
    doc = [view._doc_text(i) for i in range(view.doc_length())]
    view._sel_anchor, view._sel_head = (0, last - 2), (5, last - 1)
    fwd = view.selected_text()
    view._sel_anchor, view._sel_head = (5, last - 1), (0, last - 2)
    check("dragging up selects the same text as dragging down",
          view.selected_text(), fwd)
    check("a selection spans whole lines but its own end",
          fwd.split("\n"), [doc[last - 2].rstrip(), doc[last - 1][:6].rstrip()])
    ok("no trailing padding survives",
       not any(line.endswith(" ") for line in fwd.split("\n")))

    # Clear wipes the client's view; it does not run anything on the device.
    sent_before = len(es.sent)
    view.clear_screen()
    pump(0.2)
    check("Clear Screen sends nothing to the device", len(es.sent), sent_before)
    check("...and empties the screen", view.screen_text().strip(), "")
    ok("...and drops the selection", not view.has_selection())
    check("...while the session stays open", win._channel.sid, 99)

    # Paste is typed input, with newlines normalised to Enter.
    clip.setText("/system identity print\nfoo\r\nbar")
    view.paste_clipboard()
    pump(0.2)
    typed = b"".join(
        f.get_raw(0x000002) or b"" for f in es.sent[sent_before:]
    )
    check("pasting sends the text as typing",
          typed, b"/system identity print\rfoo\rbar")

    # --- scrollback -------------------------------------------------------
    # A terminal you cannot scroll back through is a terminal that loses
    # everything the moment output passes 24 lines, which /export does in
    # about a line and a half.
    view.clear_screen()
    pump(0.2)
    check("a fresh screen has nothing banked", view.max_scroll(), 0)
    check("...so the bar has no range", (win.scrollbar.minimum(),
                                         win.scrollbar.maximum()), (0, 0))
    ok("...and no handle to grab", not win.scrollbar.isEnabled())

    es.push((0, 200), b"".join(f"row {i:04d}\r\n".encode() for i in range(300)))
    pump(0.8)
    banked = view.max_scroll()
    ok("output past the screen is banked", banked > 250, )
    check("the bar spans the history", win.scrollbar.maximum(), banked)
    check("...and sits at the bottom", win.scrollbar.value(), banked)
    ok("...where the newest line is", "row 0299" in view.screen_text())
    ok("the bar is usable now", win.scrollbar.isEnabled())

    def document():
        """History plus the live screen: the whole thing you can scroll."""
        top = [view._line_text(ln) for ln in view._screen.history.top]
        buf = [view._line_text(view._screen.buffer[y]) for y in range(view._rows)]
        return top + buf

    def window_at(offset):
        doc = document()
        end = len(doc) - offset
        return doc[end - view._rows:end]

    view.scroll_by(50)
    pump(0.2)
    check("scrolling up moves the view", view.scroll_offset(), 50)
    check("...and the bar follows", win.scrollbar.value(), banked - 50)
    check("...by exactly 50 lines", view.visible_text(), window_at(50))
    shown = view.screen_text()
    ok("...so older output is on screen",
       any(f"row {i:04d}" in shown for i in range(200, 260)))
    ok("...and not the newest", "row 0299" not in shown)

    win.scrollbar.setValue(0)
    pump(0.2)
    check("dragging the bar to the top scrolls all the way back",
          view.scroll_offset(), banked)
    ok("...to the oldest line kept", "row 0000" in view.screen_text())

    win.scrollbar.setValue(win.scrollbar.maximum())
    pump(0.2)
    check("...and back to the bottom", view.scroll_offset(), 0)
    ok("...where the newest line is again", "row 0299" in view.screen_text())

    # Reading back must not be yanked away by output still arriving.
    view.scroll_by(100)
    held = view.screen_text().split("\n")[0]
    es.push((0, 200), b"".join(f"tail {i:03d}\r\n".encode() for i in range(20)))
    pump(0.5)
    check("output does not move what you are reading",
          view.screen_text().split("\n")[0], held)
    ok("...but it is banked all the same", view.max_scroll() > banked)

    # The wheel is how anyone actually scrolls a terminal.
    def wheel(notches):
        view.wheelEvent(QWheelEvent(
            QPointF(10, 10), QPointF(10, 10), QPoint(0, 0),
            QPoint(0, 120 * notches), Qt.MouseButton.NoButton, NONE,
            Qt.ScrollPhase.NoScrollPhase, False))
        pump(0.1)

    view.scroll_to_bottom()
    wheel(1)
    check("one wheel notch scrolls three lines", view.scroll_offset(), 3)
    wheel(3)
    check("...and it accumulates", view.scroll_offset(), 12)
    wheel(-2)
    check("...and goes back down", view.scroll_offset(), 6)
    wheel(-40)
    check("the bottom is as far down as it goes", view.scroll_offset(), 0)

    # Shift+PageUp/Home page it; unshifted those keys belong to the router.
    SHIFT = Qt.KeyboardModifier.ShiftModifier
    def press(key, mods=SHIFT):
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(key), mods))
        pump(0.1)

    press(Qt.Key.Key_PageUp)
    check("Shift+PageUp pages back", view.scroll_offset(), view._rows - 1)
    press(Qt.Key.Key_Home)
    check("Shift+Home goes to the oldest line kept",
          view.scroll_offset(), view.max_scroll())
    press(Qt.Key.Key_End)
    check("Shift+End comes back to the prompt", view.scroll_offset(), 0)

    sent_before2 = len(es.sent)
    press(Qt.Key.Key_PageUp, NONE)
    typed_pgup = b"".join(f.get_raw(0x000002) or b"" for f in es.sent[sent_before2:])
    from x3.ui.widgets.terminal_window import translate_key
    check("plain PageUp still goes to the device, not the scrollback",
          typed_pgup, translate_key(int(Qt.Key.Key_PageUp), NONE, ""))
    check("...and does not move the view", view.scroll_offset(), 0)

    # Typing is aimed at the prompt, so it goes back to the prompt.
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, ord("X"), NONE, "x"))
    pump(0.2)
    check("typing returns to the live screen", view.scroll_offset(), 0)

    # Copying takes what is on screen, scrollback included.
    # Select All means all of it. It used to mean "the 24 lines in view",
    # which quietly threw away everything you had scrolled back to read.
    view.scroll_to_bottom()
    view.select_all()
    held = view.selected_text()
    check("Select All covers the whole document",
          len(held.split("\n")), view.doc_length())
    ok("...the oldest line kept", "row 0000" in held)
    ok("...and the newest", "tail 019" in held)

    view.select_screen()
    check("Select Screen covers only what is in view",
          view.selected_text().split("\n"),
          [ln.rstrip() for ln in view.visible_text()])

    # A selection is on the text, not on a screen position, so scrolling
    # neither drops it nor slides it onto whatever moved underneath.
    view.scroll_to_bottom()
    view.select_all()
    before_scroll = view.selected_text()
    view.scroll_by(150)
    ok("a selection survives being scrolled", view.has_selection())
    check("...and still holds the same text", view.selected_text(), before_scroll)

    view.scroll_to_bottom()
    view._sel_anchor, view._sel_head = (0, 10), (5, 11)
    dragged = view.selected_text()
    view.scroll_by(100)
    check("...whichever lines it was made on", view.selected_text(), dragged)

    # Copy All is the same reach without having to select first.
    view.clear_selection()
    clip.clear()
    ok("Copy All copies something", view.copy_all())
    copied = clip.text()
    lines = copied.split("\n")
    ok("...the oldest line kept", "row 0000" in lines[0])
    ok("...through to the newest", "tail 019" in copied)
    ok("...with no blank padding on either end",
       lines[0].strip() != "" and lines[-1].strip() != "")
    ok("...and far more than one screenful", len(lines) > 4 * view._rows)
    check("...which is the whole document, trimmed",
          copied, view.document_text())

    view.scroll_to_bottom()
    view.clear_selection()

    view.clear_screen()
    pump(0.2)
    check("Clear Screen empties the scrollback too",
          (view.max_scroll(), win.scrollbar.maximum()), (0, 0))

    # The window has to be shrinkable: sizeHint is 100x24 of monospace.
    ok("the terminal can be made small",
       view.minimumSizeHint().width() < view.sizeHint().width())
    win.resize(320, 180)
    pump(0.3)
    check("...and the window honours it", (win.width(), win.height()), (320, 180))
    ok("...with the grid reflowed to fit", view._cols < 100)
finally:
    win.close()
    pump(0.3)
    ok("closing asks the console to quit",
       any(f.get_raw(0x000002) == b"/quit\r" for f in es.sent))
    check("the client forgot the console", client._terminals, [])
    es.stop_demux()

# --- colours: Winbox v3 is black on white, Winbox 4 light on dark -----------
# The router sends the same escapes either way; the scheme is ours to paint.

from x3.ui.widgets.terminal_window import (  # noqa: E402
    DARK, LIGHT, TerminalView, scheme_for,
)

check("light and dark are named, not guessed",
      (scheme_for("light"), scheme_for("dark")), (LIGHT, DARK))
check("auto follows the theme: winbox3 is a light window",
      scheme_for("auto"), LIGHT)
ui_style.apply_theme(app, "dark", scale=1.0, persist=False)
check("...and a dark theme gets the dark console", scheme_for("auto"), DARK)
ui_style.apply_theme(app, "classic", scale=1.0, persist=False)


def darkest(choice: str, data: bytes) -> int:
    v = TerminalView(10, 2)
    v.set_colours(choice)
    v._stream.feed(data)
    v.resize(v.sizeHint())
    img = v.grab().toImage()
    return min(img.pixelColor(x, y).lightness()
               for x in range(img.width()) for y in range(img.height()))


# --- F1 is the console's help, not the app's -------------------------------
# The router's banner says "Press F1 for help"; View ▸ Keyboard Keys sits on F1
# too, and used to win.

from PySide6.QtGui import QAction, QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QLineEdit, QMainWindow, QVBoxLayout, QWidget  # noqa: E402

host = QMainWindow()
fired: list[str] = []
help_act = QAction("Keyboard Keys", host)
help_act.setShortcut(QKeySequence("F1"))
help_act.triggered.connect(lambda: fired.append("app"))
host.addAction(help_act)
holder = QWidget()
lay = QVBoxLayout(holder)
console = TerminalView(20, 4)
other = QLineEdit()
lay.addWidget(console)
lay.addWidget(other)
host.setCentralWidget(holder)
host.show()
sent: list[bytes] = []
console.input_ready.connect(sent.append)
console.setFocus()
pump(0.1)
QTest.keyClick(console, Qt.Key.Key_F1)
pump(0.1)
check("F1 in a console goes to the router", (sent, fired), ([b"\x1bOP"], []))
other.setFocus()
pump(0.1)
QTest.keyClick(other, Qt.Key.Key_F1)
pump(0.1)
check("...and is still the app's everywhere else", fired, ["app"])
host.close()

ok("light paints the paper white",
   darkest("light", b"") == 255)
ok("...and explicit white ink on it in black, not invisibly",
   darkest("light", b"\x1b[37mXXXX\x1b[0m") < 80)

if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all terminal tests passed")
