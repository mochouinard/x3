"""Compact Winbox-style connect dialog with live MNDP + Addresses.cdb.

Neighbors table: click the **Address** column to connect by IP (TCP 8291);
click the **MAC Address** column to connect by MAC (MAC-Winbox UDP 20561).

Addresses table: Addresses.cdb merged with our own local store (see
:mod:`x3.ui.connstore` — the cdb is read, never written). The first column
carries the origin: blank for a plain cdb entry, ``L`` where we have edited
one, ``N`` for a connection that exists only here. Stored passwords sit in
their own column, masked until the **Show** toggle (shared with
Settings ▸ Show Passwords) reveals them.

Click a header to sort, ascending → descending → back to file order. Ctrl+C
copies the cell under the cursor in either table; right-click for Copy
Address / Login / Password.
"""

from __future__ import annotations

import html
import re
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, QSize, QStringListModel, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from x3.proto import audit as audit_mod

from .. import a11y, applog, connstore as cs
from .. import i18n, qthreads, style
from ..i18n import tr, trf, verbatim
from .group_fold import GroupFold
from .wheel_guard import guard_wheel
from ..session import SessionInfo
from ..services import (
    DEFAULT_PORT,
    AddressRow,
    NeighborRow,
    discover_neighbors,
    find_addresses_cdb,
    load_address_book,
    split_host_port,
)
from ..sortkey import sort_key
from ..settings import (
    LIST_REFRESH_CHOICES,
    LastConnection,
    load_audit,
    load_audit_level,
    load_connect_fold,
    load_language,
    load_list_refresh_secs,
    load_last_connection,
    load_show_passwords,
    save_addresses_cdb,
    save_audit,
    save_audit_level,
    save_connect_fold,
    save_language,
    save_list_refresh_secs,
    save_last_connection,
    save_show_passwords,
)

# Neighbor table columns
COL_IDENTITY = 0
COL_ADDRESS = 1
COL_MAC = 2
COL_BOARD = 3
COL_VERSION = 4
COL_UPTIME = 5

# Addresses table columns. Column 0 is the Winbox-style flag letter saying
# where the row came from: blank = straight from Addresses.cdb, L = edited
# locally, N = ours alone. See x3.ui.connstore.
COL_ADDR_FLAG = 0
COL_ADDR_HOST = 1
COL_ADDR_USER = 2
COL_ADDR_PASS = 3
COL_ADDR_NOTE = 4
COL_ADDR_GROUP = 5
#: When this machine last logged in to the entry -- from the device record a
#: successful connect leaves, so blank for anything never connected from here.
COL_ADDR_LAST = 6

#: Sort key per address column, as an attribute of connstore.Connection.
ADDR_SORT_FIELD = {
    COL_ADDR_FLAG: "flag",
    COL_ADDR_HOST: "host",
    COL_ADDR_USER: "login",
    COL_ADDR_NOTE: "note",
    COL_ADDR_GROUP: "group",
    COL_ADDR_LAST: "last_used",
}

#: The Group filter's first choice.
ALL_GROUPS = "All groups"


def dot_icon(colour: str | None, px: int) -> QIcon:
    """A round dot ``px`` across, or a blank of the same size for None.

    The blank is what keeps every address starting at the same x whether or
    not its row carries a dot. Drawn at 2x so it stays round on HiDPI.
    """
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    if colour:
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(colour))
        d = px * 0.7
        p.drawEllipse(QRectF((px - d) / 2, (px - d) / 2, d, d))
        p.end()
    return QIcon(pm)


def ago(ts: float, now: float | None = None) -> str:
    """``5m ago`` / ``3h ago`` / ``2d ago``, then the date; "" for never."""
    if not ts:
        return ""
    secs = max(0, int((now if now is not None else time.time()) - ts))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return trf("{n}m ago", n=secs // 60)
    if secs < 86400:
        return trf("{n}h ago", n=secs // 3600)
    if secs < 30 * 86400:
        return trf("{n}d ago", n=secs // 86400)
    return time.strftime("%Y-%m-%d", time.localtime(ts))

#: Sort key per neighbour column, as an attribute of services.NeighborRow.
NEIGH_SORT_FIELD = {
    COL_IDENTITY: "identity",
    COL_ADDRESS: "address",
    COL_MAC: "mac",
    COL_BOARD: "board",
    COL_VERSION: "version",
    COL_UPTIME: "uptime",
}

# Stand-in for a stored password while Show Passwords is off. Fixed width, so
# the column doesn't jump when the real value is revealed.
PASSWORD_MASK = "••••••••"

#: How often the Neighbors list asks the LAN again while the page is up, and
#: how long a device may stay silent before its row goes. The router answers
#: every request, so a box missing from three scans in a row has left.
NEIGHBOR_RESCAN_MS = 10_000
NEIGHBOR_MAX_AGE_S = 35.0


#: The phases DeviceClient.connect() reports, by the start of its progress
#: text, so the status line can say how far along a connect is.
CONNECT_STAGES = ("Connecting", "Authenticating", "Loading schema", "Reading device info")


def explain_connect_error(
    err: str, *, transport: str = "tcp", port: int = DEFAULT_PORT, has_mac: bool = False
) -> tuple[str, str]:
    """A connect failure as (what happened, what to try next).

    DeviceClient wraps every transport and auth failure in one DeviceError,
    so the message is all there is to go on. The raw text still reaches the
    status line and the tooltip; this is the sentence a person can act on.
    """
    low = err.lower()
    if "rejected login" in low or "invalid user name or password" in low:
        return ("Login rejected",
                "The device refused this login and password. Check both, or "
                "tick Try all saved to look for a reused one.")
    if "hash mismatch" in low:
        return ("Handshake mismatch",
                "The device answered, but its login proof did not match ours — "
                "a client fault, not a wrong password. Audit this connection "
                "to capture it.")
    if "refused" in low:
        return ("Connection refused",
                trf("Something is there, but nothing accepts Winbox on port {port}: "
                    "the winbox service may be off, on another port, or limited "
                    "to other addresses (/ip service).", port=port))
    if any(k in low for k in ("name or service not known", "nodename", "getaddrinfo")):
        return ("Unknown host",
                "That name does not resolve. Check the spelling, or use the address.")
    if "no route" in low or "unreachable" in low:
        return ("Unreachable", "This machine has no route to that address.")
    if "timed out" in low or "timeout" in low:
        if transport == "mac":
            return ("No answer",
                    "Nothing replied over MAC-Winbox. The device has to share a "
                    "layer-2 segment with this machine, and its MAC server has "
                    "to allow that interface.")
        hint = ("Nothing answered: a wrong address, a firewall dropping the "
                "port, or the device is down.")
        if has_mac:
            hint += " It is on this LAN — try its MAC Address instead."
        return ("No answer", hint)
    return ("Connect failed", "")


class _NeighborWorker(QThread):
    finished_ok = Signal(list)  # list[NeighborRow]
    finished_err = Signal(str)

    def __init__(self, timeout: float = 2.5, parent=None) -> None:
        super().__init__(parent)
        self.timeout = timeout

    def run(self) -> None:  # noqa: N802
        try:
            rows = discover_neighbors(timeout=self.timeout)
            self.finished_ok.emit(rows)
        except PermissionError as e:
            self.finished_err.emit(f"permission denied (UDP 5678): {e}")
        except OSError as e:
            self.finished_err.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.finished_err.emit(f"{type(e).__name__}: {e}")


class _ConnectWorker(QThread):
    """Run DeviceClient.connect() off the UI thread (auth + schema + info).

    Doing this on the GUI thread freezes the whole app for the full socket
    timeout (often 12–30s) when the host is wrong or slow.
    """

    finished_ok = Signal(object)  # DeviceClient
    finished_err = Signal(str)
    status = Signal(str)

    def __init__(
        self,
        *,
        host: str,
        user: str,
        password: str,
        port: int,
        transport: str,
        schema_dir: str | None,
        audit: bool = False,
        audit_level: str = "config",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._user = user
        self._password = password
        self._port = port
        self._transport = transport
        self._schema_dir = schema_dir
        self._audit = audit
        self._audit_level = audit_level
        # Cancel handshake with the GUI thread; see cancel().
        self._lock = threading.Lock()
        self._client = None
        self._cancelled = False

    def cancel(self) -> None:
        """Abort an in-flight connect. Called from the GUI thread.

        Closing the client stops the demux reader, which fails every pending
        waiter at once (demux._fail_all). That is what lets run() return in
        milliseconds instead of sitting out the 12s per-request timeout inside
        refresh_info -- and a worker that returns is a worker Qt can destroy
        without aborting the process.
        """
        with self._lock:
            self._cancelled = True
            client = self._client
        if client is None:
            return
        # abort() first: until connect() returns there is no session to close,
        # and the transport can still be walking local interfaces.
        for step in (getattr(client, "abort", None), client.close):
            if step is None:
                continue
            try:
                step()
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> None:  # noqa: N802
        try:
            from x3.proto.device import DeviceClient

            self.status.emit(
                f"Connecting to {self._host}:{self._port} "
                f"({self._transport})…"
            )
            client = DeviceClient(
                self._host,
                self._user,
                self._password,
                port=self._port,
                timeout=12.0,
                transport=self._transport,
                schema_dir=self._schema_dir,
                # Read-only until the session is switched to Live mode, which
                # is what clears this flag (MainWindow._apply_mode).
                readonly=True,
                audit=self._audit,
                audit_level=self._audit_level,
            )
            with self._lock:
                if self._cancelled:
                    return
                # Published before connect() so a cancel arriving mid-request
                # has something to close.
                self._client = client
            # connect() reports its own phases: auth, then schema (may fetch
            # the plugin index), then device info. Reporting them as one
            # "Authenticating…" hid where the slow part actually was.
            client.connect(progress=self.status.emit)
            if self._cancelled:
                client.close()
                return
            self.finished_ok.emit(client)
        except Exception as e:  # noqa: BLE001
            # A cancel tears the socket down under us; that exception is the
            # mechanism working, not a failure worth reporting.
            if self._cancelled:
                return
            self.finished_err.emit(f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._client = None


class _TryCredsWorker(QThread):
    """Find which saved password opens a device, off the UI thread.

    Two hard safety rules, both enforced here rather than trusted to the caller:

    * **EC-SRP5 only.** Before any password is tried, one stage-1/stage-2
      handshake reads the device's SRP salt (:meth:`ModernSession.peek_salt`).
      That exchange sends only the username and a random public key -- no
      password-derived material. If the device does not answer with a valid
      EC-SRP5 stage 2 (legacy pre-6.43 gear, or anything that is not a modern
      the router), we emit :attr:`not_secure` and stop *without trying a single
      password*. So a reused password never reaches a box that would take it
      over a weaker handshake.
    * **The password never crosses the wire regardless.** Even on the boxes we
      do spray, EC-SRP5 keeps the password off the wire; we only learn *which*
      stored password authenticates, never expose one.
    """

    status = Signal(str)
    found = Signal(str)  # the working password
    exhausted = Signal(int)  # tried N, none worked
    not_secure = Signal(str)  # device did not present the EC-SRP5 handshake
    error = Signal(str)

    def __init__(
        self,
        *,
        host: str,
        user: str,
        port: int,
        transport: str,
        candidates: list[str],
        delay: float = 0.3,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._user = user
        self._port = port
        self._transport = transport
        self._candidates = candidates
        self._delay = delay
        self._lock = threading.Lock()
        self._sess = None
        self._cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            sess = self._sess
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _publish(self, sess) -> bool:
        """Hand the live session to cancel(); False if already cancelled."""
        with self._lock:
            if self._cancelled:
                return False
            self._sess = sess
            return True

    def _retire(self, sess) -> None:
        with self._lock:
            self._sess = None
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:  # noqa: N802
        from x3.proto.modern import (
            ModernAuthError,
            ModernProtocolError,
            ModernSession,
        )

        def _open():
            return ModernSession(
                self._host, self._port, 12.0, transport=self._transport
            )

        try:
            # --- EC-SRP5 gate: peek the stage-2 salt, no password material ---
            self.status.emit("Verifying secure (EC-SRP5) handshake…")
            probe = _open()
            if not self._publish(probe):
                return
            try:
                probe.connect()
                probe.peek_salt(self._user)
            except (ModernAuthError, ModernProtocolError) as e:
                self._retire(probe)
                if not self._is_cancelled():
                    self.not_secure.emit(str(e))
                return
            except OSError as e:
                self._retire(probe)
                if not self._is_cancelled():
                    self.error.emit(f"{type(e).__name__}: {e}")
                return
            self._retire(probe)
            if self._is_cancelled():
                return

            # --- spray the distinct saved passwords ---
            n = len(self._candidates)
            for i, pw in enumerate(self._candidates, 1):
                if self._is_cancelled():
                    return
                self.status.emit(trf("Trying saved password {i} of {n}…", i=i, n=n))
                sess = _open()
                if not self._publish(sess):
                    return
                ok = False
                try:
                    sess.connect()
                    sess.authenticate(self._user, pw)
                    ok = True
                except (ModernAuthError, ModernProtocolError):
                    # Wrong credential (or the device dropped the handshake on a
                    # bad login) -- the gate already proved it is reachable and
                    # EC-SRP5, so this is a rejection, not a fault.
                    ok = False
                except OSError as e:
                    self._retire(sess)
                    if not self._is_cancelled():
                        self.error.emit(f"{type(e).__name__}: {e}")
                    return
                finally:
                    self._retire(sess)
                if ok:
                    self.found.emit(pw)
                    return
                if self._delay:
                    time.sleep(self._delay)
            self.exhausted.emit(n)
        except Exception as e:  # noqa: BLE001
            if not self._is_cancelled():
                self.error.emit(f"{type(e).__name__}: {e}")


class ConnectDialog(QDialog):
    #: done() code asking app.py to build this page again in the language just
    #: picked; :meth:`carried` is what the new page restores.
    LANGUAGE_CHANGED = 2

    def __init__(
        self, parent=None, *, cdb_path: str | Path | None = None, start_page: bool = False
    ) -> None:
        super().__init__(parent)
        # The page app.py opens with nothing else on screen, as opposed to New
        # x3 from inside a session: only this one can change language at once.
        self._start_page = start_page
        self.setWindowTitle("x3")
        self.setModal(True)
        # Scaled by the active zoom: the connection page is the first window up,
        # before any MainWindow, and its size is set here rather than left to a
        # hint -- so at a saved zoom it has to open proportionally larger, the
        # way the session window's own initial resize does. scale_px reads the
        # zoom applied at startup (app.py, before this dialog is built).
        self.resize(style.scale_px(580), style.scale_px(480))
        self.setMinimumSize(style.scale_px(480), style.scale_px(400))
        self.session: SessionInfo | None = None
        self.client = None  # x3.proto.device.DeviceClient when live

        self._neighbor_rows: list[NeighborRow] = []
        # Addresses.cdb exactly as read; never written back (see connstore).
        self._address_rows: list[AddressRow] = []
        # Local overlay, and the cdb-plus-overlay view it produces.
        self._store: cs.Store = cs.load()
        self._merged: list[cs.Connection] = []
        self._filtered_view: list[cs.Connection] = []
        # Header sort: (column, descending) or None for file order.
        self._addr_sort: tuple[int, bool] | None = None
        self._neigh_sort: tuple[int, bool] | None = None
        self._cdb_path: Path | None = Path(cdb_path).expanduser() if cdb_path else None
        self._pending_identity = ""
        self._pending_board = ""
        self._pending_version = ""
        self._transport = "tcp"  # tcp | mac
        self._mac = ""
        self._worker: _NeighborWorker | None = None
        # Every device heard from, by MAC, with when it last answered. Scans
        # add to this rather than replace it, so a reply lost to one scan does
        # not blink the row out; NEIGHBOR_MAX_AGE_S is what removes one.
        self._neigh_seen: dict[str, tuple[NeighborRow, float]] = {}
        # Whether the next scan result is reported on the status line: yes for
        # the first scan and for Refresh, no for the timer's.
        self._scan_announce = True
        # Started and stopped by showEvent / hideEvent.
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(NEIGHBOR_RESCAN_MS)
        self._scan_timer.timeout.connect(self._rescan_neighbors)
        self._connect_worker: _ConnectWorker | None = None
        self._tryall_worker: _TryCredsWorker | None = None
        self._schema_dir: str | None = None
        # Stash connect form fields for the async completion path
        self._pending_connect: dict | None = None
        self._busy_cursor = False
        # How far the connect in flight has got, for the Connect button's
        # running time and the stage on the status line.
        self._connect_started = 0.0
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._paint_connect_elapsed)
        # Shared with MainWindow's Settings ▸ Show Passwords
        self._show_passwords = load_show_passwords()

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Winbox's reading order, and the reason its dialog feels calmer than a
        # stack of equals: the connection *is* the subject, stated up top in a
        # fixed-height block, and the device list is the content underneath it
        # that grows to fill whatever room is left. Putting the list first and
        # the form under it made the form look like a footnote and left the
        # list unable to reach the bottom of the window.
        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(3)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.host = QLineEdit("192.168.88.1")
        self.host.textChanged.connect(self._on_host_edited)
        # Saved addresses complete as they are typed (anywhere in the text);
        # picking one that a single saved entry owns loads that entry.
        self._host_model = QStringListModel(self)
        completer = QCompleter(self._host_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated.connect(self._on_host_completed)
        self.host.setCompleter(completer)
        self.user = QLineEdit("admin")
        # Address and login decide whether Save updates or adds, so the hint
        # has to follow them as they are typed.
        self.user.textChanged.connect(lambda _t: self._refresh_save_hint())
        # ...and which saved device the mode line looks up for its link.
        self.user.textChanged.connect(lambda _t: self._update_mode_label())
        self.password = QLineEdit()
        # tr() by hand: one lowercase word is taken for a router's value otherwise.
        self.password.setPlaceholderText(tr("password"))

        # Qt refuses clipboard copy out of a Password-echo QLineEdit, so the
        # same toggle that reveals the text is what makes select-and-copy work.
        self.show_pw = QCheckBox("Show")
        self.show_pw.setChecked(self._show_passwords)
        self.show_pw.setToolTip(
            "Reveal stored passwords here and in the Addresses list, and allow "
            "selecting/copying them. Shares Settings ▸ Show Passwords."
        )
        self.show_pw.toggled.connect(self._on_show_passwords_toggled)

        pw_row = QWidget()
        pw_lay = QHBoxLayout(pw_row)
        pw_lay.setContentsMargins(0, 0, 0, 0)
        pw_lay.setSpacing(4)
        # The row's label lands on the wrapper, not on the box inside it, so
        # without this the one field nobody can afford to mistake -- a password
        # -- is the one field with no name.
        a11y.name(self.password, "Password")
        pw_lay.addWidget(self.password, stretch=1)
        pw_lay.addWidget(self.show_pw)

        # Recover access when a reused password has been lost track of: on
        # Connect, try every distinct saved password until one authenticates.
        self.try_all = QCheckBox("Try all saved")
        self.try_all.setToolTip(
            "On Connect, try every distinct password in your address book "
            "against this device until one works.\n\n"
            "Runs ONLY over the secure firmware 6.43+ (EC-SRP5) handshake: the "
            "password is never sent in cleartext, and if the device does not "
            "offer that handshake the attempt is refused before any password is "
            "tried.\n\n"
            "Applies to one Connect, then un-ticks itself."
        )
        a11y.name(self.try_all, "Try all saved passwords")
        pw_lay.addWidget(self.try_all)

        self.mode_label = QLabel("Mode: TCP (IP)")
        self.mode_label.setStyleSheet(f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};")
        # When the device is on the LAN, the line ends in a link to reach it
        # the other way -- by MAC from an IP, by IP from a MAC. Tab reaches
        # the link and Enter follows it, same as a click.
        self.mode_label.setTextFormat(Qt.TextFormat.RichText)
        self.mode_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        self.mode_label.linkActivated.connect(lambda _href: self._swap_transport())
        a11y.name(self.mode_label, "Connection mode")

        # Winbox carries Note and Group on the connect form, and without them
        # the columns of the same name could be read but never set.
        self.note = QLineEdit()
        self.note.setPlaceholderText("what this device is (shown in the list)")
        self.group = QLineEdit()
        self.group.setPlaceholderText("optional grouping label")

        form.addRow("Connect To:", self.host)
        form.addRow("", self.mode_label)
        form.addRow("Login:", self.user)
        form.addRow("Password:", pw_row)

        # Two folds keep what is asked on every connect -- where, who, which
        # password -- above the device list, and the rest a click away. Open
        # all the time they pushed the list down to a few rows at the
        # dialog's minimum height. Whether each is open is remembered.
        self.entry_fold = GroupFold("Saved Entry", expanded=load_connect_fold("entry"))
        form.addRow(self.entry_fold)
        form.addRow("Note:", self.note)
        form.addRow("Group:", self.group)

        # Winbox's Add/Set sits under the form for the same reason: this saves
        # what is typed above, so it belongs above the list rather than inside
        # one tab of it. Writes our own store, never Addresses.cdb — Winbox
        # rewrites that file wholesale with no locking, so a second writer
        # loses entries silently. connstore's docstring has the detail.
        self.btn_save = QPushButton("Save Connection")
        self.btn_save.setToolTip(
            "Save these fields as a connection.\n"
            "Updates the selected entry when the address and login still match "
            "it, otherwise adds a new one.\n"
            "Stored in connections.json — Addresses.cdb is only ever read."
        )
        self.btn_save.clicked.connect(self._save_entry)
        save_row = QWidget()
        sr = QHBoxLayout(save_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(4)
        sr.addWidget(self.btn_save)
        self._save_hint = QLabel("")
        self._save_hint.setStyleSheet(f"color: {style.TEXT_DIM};")
        sr.addWidget(self._save_hint)
        sr.addStretch(1)
        form.addRow("", save_row)

        # Audit is per-connection and decided here, because that is the moment
        # it can be decided: the recorder attaches to the session as it opens.
        # Unchecked by default -- an audit file holds every configuration value
        # that crosses the wire, so it is the user's call to make.
        self.audit_box = QCheckBox("Audit this connection")
        self.audit_box.setToolTip(
            "Record every message sent to and received from the device to an "
            "audit file, for later review or replay.\n"
            "Read one back with: x3ctl audit <file>"
        )
        self.audit_box.setChecked(load_audit())
        self.audit_box.toggled.connect(save_audit)

        # One ordered picker rather than a checkbox per thing to keep: the
        # levels are supersets of each other (see x3.proto.audit.LEVELS), so
        # there is no combination to get wrong. Statistics polling is the step
        # above the default, which is how you turn it on.
        self.audit_level = QComboBox()
        for name in audit_mod.LEVELS:
            self.audit_level.addItem(tr(audit_mod.LEVEL_LABEL[name]), name)
            self.audit_level.setItemData(
                self.audit_level.count() - 1, tr(audit_mod.LEVEL_HELP[name]),
                Qt.ItemDataRole.ToolTipRole,
            )
        current = load_audit_level()
        self.audit_level.setCurrentIndex(max(0, self.audit_level.findData(current)))
        self.audit_level.setToolTip(
            "How much of the traffic to keep.\n"
            "'stats' adds the 1 Hz Traffic/statistics polling — every counter "
            "sample lands in the file, so it grows much faster than 'config'.\n"
            "Identical consecutive replies still collapse below 'all', which "
            "costs nothing: a live counter changes every second anyway."
        )
        self.audit_level.currentIndexChanged.connect(
            lambda _i: save_audit_level(self.audit_level.currentData())
        )

        # How often a list re-reads once we are in. Asked here as well as in
        # Settings because the answer usually depends on the link you are about
        # to use: a 1 Hz Interface list is nothing across a LAN and real
        # traffic down a slow tunnel. Same setting, same choices, either side
        # of the connection.
        self.refresh_rate = QComboBox()
        for secs, label in LIST_REFRESH_CHOICES:
            self.refresh_rate.addItem(tr(label), secs)
        saved_rate = load_list_refresh_secs()
        self.refresh_rate.setCurrentIndex(next(
            (i for i in range(self.refresh_rate.count())
             if self.refresh_rate.itemData(i) == saved_rate),
            0,
        ))
        self.refresh_rate.setToolTip(
            "How often an open list re-reads the device.\n"
            "The menu default is the rate the .jg declares for each list -- 1 "
            "second on Interfaces, which is what makes its Tx/Rx counters "
            "live.\n"
            "Off leaves a list showing what it read when it opened, until you "
            "press Refresh.\n"
            "Shares Settings > List Auto Refresh, and reaches windows already "
            "open."
        )
        self.refresh_rate.currentIndexChanged.connect(self._on_refresh_rate_changed)

        # Opens by itself while auditing is on: the recording is the user's
        # call, so the box that says it is happening is never folded away.
        self.session_fold = GroupFold(
            "Session",
            expanded=load_connect_fold("session") or self.audit_box.isChecked(),
        )
        form.addRow(self.session_fold)
        form.addRow("Auto Refresh:", self.refresh_rate)

        audit_row = QWidget()
        ar = QHBoxLayout(audit_row)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(6)
        ar.addWidget(self.audit_box)
        self._audit_level_label = QLabel("Record:")
        ar.addWidget(self._audit_level_label)
        ar.addWidget(self.audit_level)
        ar.addStretch(1)
        form.addRow("", audit_row)

        # The picker only means anything while auditing is on, so it follows the
        # checkbox rather than sitting there inviting a change with no effect.
        def _sync_audit_level(on: bool) -> None:
            self._audit_level_label.setEnabled(on)
            self.audit_level.setEnabled(on)

        self.audit_box.toggled.connect(_sync_audit_level)
        _sync_audit_level(self.audit_box.isChecked())

        self._form = form
        self._fold_rows = {
            self.entry_fold: (self.note, self.group, save_row),
            self.session_fold: (self.refresh_rate, audit_row),
        }
        for fold, key in ((self.entry_fold, "entry"), (self.session_fold, "session")):
            fold.folded.connect(
                lambda expanded, f=fold, k=key: self._on_fold(f, k, expanded)
            )
            self._show_fold_rows(fold)

        self._apply_password_echo()
        root.addWidget(form_host)

        # documentMode draws a stray full-width base line *above* the tab strip
        # under our stylesheets; the framed pane is also what the property
        # windows use, so keep tabs consistent across the app.
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, stretch=1)

        self._build_neighbors_tab()
        self._build_addresses_tab()

        # Why the last connect failed and what to try, in place of a modal box
        # that had to be dismissed before the form could be fixed. A plain
        # label: the spoken status line below already says it out loud.
        self.error_banner = QLabel("")
        self.error_banner.setWordWrap(True)
        self.error_banner.setTextFormat(Qt.TextFormat.RichText)
        self.error_banner.setStyleSheet(f"color: {style.INFO_RED};")
        self.error_banner.hide()
        root.addWidget(self.error_banner)

        # Spoken, not just shown: every stage of a connect, every cdb error and
        # every save lands here, and none of it is anywhere else.
        self.status = a11y.SpokenLabel(
            "Neighbors: click Address to connect by IP, MAC Address by MAC-Winbox"
        )
        self.status.setStyleSheet(f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};")
        self.status.setWordWrap(True)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.addWidget(self.status, 1)
        # Shown only while a newer x3 is known (see update_dialog.notifier).
        # On this line rather than the button row: there, beside Theme and
        # Language, the dialog's minimum width clipped it to "0.1.1 availabl".
        self.update_button = QPushButton()
        self.update_button.setAutoDefault(False)
        self.update_button.hide()
        self.update_button.clicked.connect(self._open_update)
        status_row.addWidget(self.update_button, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(status_row)

        # Theme is an app preference, not part of the connection, so it sits on
        # the button row instead of pretending to be a fourth connection field.
        self.theme_combo = QComboBox()
        for th in style.list_themes():
            self.theme_combo.addItem(tr(th.name), th.id)
            idx = self.theme_combo.count() - 1
            self.theme_combo.setItemData(idx, tr(th.description), Qt.ItemDataRole.ToolTipRole)
        cur = style.active_theme().id
        for i in range(self.theme_combo.count()):
            if self.theme_combo.itemData(i) == cur:
                self.theme_combo.setCurrentIndex(i)
                break
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self.theme_combo.setMaximumWidth(220)

        # Language too, for the same reason, and because this is the one page
        # reached before any session: View ▸ Language needs a router first.
        self.language_combo = QComboBox()
        for label, code in i18n.choices():
            self.language_combo.addItem(label, code)
        self.language_combo.setCurrentIndex(
            max(0, self.language_combo.findData(load_language()))
        )
        self.language_combo.setToolTip(
            "Menus, windows and the router's labels. What the router stores "
            "is never translated."
        )
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        self.language_combo.setMaximumWidth(220)
        for combo in (self.theme_combo, self.language_combo):
            # At the dialog's minimum width these give way, not Connect and Cancel.
            combo.setMinimumWidth(style.scale_px(60))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_connect = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._btn_connect.setText("Connect")
        # Connect is the only default: otherwise the style can make Cancel the
        # default (or focus it), so Enter / Space quit the dialog instead of
        # connecting.
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            # Named here, not by Qt, so it is in the user's language too.
            cancel_btn.setText("Cancel")
            cancel_btn.setAutoDefault(False)
            cancel_btn.setDefault(False)
        self._btn_connect.setAutoDefault(True)
        buttons.accepted.connect(self._connect)
        buttons.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(6)
        self._bottom_labels = (QLabel("Theme:"), QLabel("Language:"))
        for lab, combo in zip(self._bottom_labels, (self.theme_combo, self.language_combo)):
            lab.setStyleSheet(f"color: {style.TEXT_DIM};")
            bottom.addWidget(lab)
            bottom.addWidget(combo)
        bottom.addStretch(1)
        bottom.addWidget(buttons)
        root.addLayout(bottom)
        from .update_dialog import notifier

        notifier.available.connect(self._show_update)
        if notifier.release is not None:
            self._show_update(notifier.release)
        # Only now, with the button inside the dialog: setDefault() registers
        # the dialog's own default only if it can find the dialog, and returns
        # early when the button is already default -- so set before this line
        # it never registered at all. On show the dialog then promoted the
        # first auto-default button it met, and once focus had passed through
        # Save or Refresh, Enter pressed nothing.
        self._btn_connect.setDefault(True)

        self.host.returnPressed.connect(self._connect)
        self.password.returnPressed.connect(self._connect)
        # Changing what failed is the fix under way; the banner has said its piece.
        for field in (self.host, self.user, self.password):
            field.textEdited.connect(lambda _t: self.error_banner.hide())

        # Zoom, same chords as the session window (MainWindow builds its own;
        # this dialog is up before one exists). QShortcut rather than a menu
        # action -- a QDialog has no menu bar to hang one off. Ctrl++ and
        # Ctrl+Shift+= join Ctrl+= because "Ctrl and plus" is not one key.
        for seq in ("Ctrl+=", "Ctrl++", "Ctrl+Shift+="):
            QShortcut(QKeySequence(seq), self, activated=lambda: self._zoom_step(1))
        for seq in ("Ctrl+-", "Ctrl+_"):
            QShortcut(QKeySequence(seq), self, activated=lambda: self._zoom_step(-1))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self._zoom_reset)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self._focus_filter)

        # Audit level, Refresh rate, Theme: three dropdowns a wheel roll
        # aimed at the device list used to change on the way past.
        guard_wheel(self)
        self._setup_tab_order()
        self._restore_last_connection()
        self._load_cdb()
        self._start_neighbor_scan()
        # Ready to connect: focus Connect when we already have credentials
        if self.password.text():
            self._btn_connect.setFocus()
        elif self.host.text():
            self.password.setFocus()
        else:
            self.user.setFocus()

    def _show_fold_rows(self, fold: GroupFold) -> None:
        for field in self._fold_rows[fold]:
            self._form.setRowVisible(field, fold.expanded)

    def _on_fold(self, fold: GroupFold, key: str, expanded: bool) -> None:
        self._show_fold_rows(fold)
        save_connect_fold(key, expanded)

    def _on_host_completed(self, host: str) -> None:
        want = host.strip().lower()
        owners = [c for c in self._merged if c.host.strip().lower() == want]
        if len(owners) == 1:
            self._load_connection(owners[0])
            self._select_key(owners[0].key)

    def _focus_filter(self) -> None:
        """Ctrl+F: to the Addresses filter, from anywhere on the page."""
        self.tabs.setCurrentIndex(1)
        self.cdb_filter.setFocus()
        self.cdb_filter.selectAll()

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Type.KeyPress:
            mods = event.modifiers() & ~Qt.KeyboardModifier.ShiftModifier
            # Enter on a row connects to it. Taken here rather than left to
            # the default button, which a table does not reliably reach.
            if obj in (self.addresses, self.neighbors) and event.key() in (
                Qt.Key.Key_Return, Qt.Key.Key_Enter
            ):
                if obj.currentRow() >= 0:
                    self._connect()
                return True
            # Enter in a filter does nothing. Left alone it reached the
            # default button and connected to whatever the form last held,
            # which is not what someone typing a search meant; Down walks
            # into what the filter found.
            if obj in (self.cdb_filter, self.group_filter) and event.key() in (
                Qt.Key.Key_Return, Qt.Key.Key_Enter
            ):
                return True
            if obj is self.addresses:
                text = event.text()
                if text and text.isprintable() and not text.isspace() and mods == Qt.KeyboardModifier.NoModifier:
                    self.cdb_filter.setFocus()
                    self.cdb_filter.insert(text)
                    return True
            elif obj is self.cdb_filter and event.key() == Qt.Key.Key_Down:
                if self.addresses.rowCount():
                    self.addresses.setFocus()
                    if self.addresses.currentRow() < 0:
                        self.addresses.setCurrentCell(0, COL_ADDR_HOST)
                    else:
                        self._pick_address(self.addresses.currentRow(), 0)
                return True
        return super().eventFilter(obj, event)

    def _setup_tab_order(self) -> None:
        """Pick a device, then Tab straight into Connect To / Login / Password.

        One chain covers both pages: focus traversal skips the widgets of
        whichever tab page is hidden, so Tab from either table reaches the
        host field. Rows inside a closed fold are hidden, so the chain steps
        over them the same way and lands on the next fold. A fold is chained
        by its triangle: handed the fold itself, setTabOrder moved the whole
        container and dragged Cancel in ahead of it.
        """
        chain = (
            self.btn_refresh,
            self.neighbors,
            self.btn_open_cdb,
            self.group_filter,
            self.cdb_filter,
            self.btn_remove,
            self.btn_revert,
            self.addresses,
            self.host,
            self.user,
            self.password,
            self.show_pw,
            self.try_all,
            self.entry_fold.focusProxy(),
            self.note,
            self.group,
            self.btn_save,
            self.session_fold.focusProxy(),
            self.refresh_rate,
            self.audit_box,
            self.audit_level,
            self.update_button,
            self.theme_combo,
            self.language_combo,
            self._btn_connect,
        )
        for first, second in zip(chain, chain[1:]):
            self.setTabOrder(first, second)

    # --- Neighbors tab --------------------------------------------------

    def _build_neighbors_tab(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 4, 2, 2)
        lay.setSpacing(2)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.setContentsMargins(0, 0, 0, 0)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setMinimumWidth(72)
        self.btn_refresh.clicked.connect(self._start_neighbor_scan)
        bar.addWidget(self.btn_refresh)
        self.neigh_status = a11y.SpokenLabel("Scanning…")
        self.neigh_status.setStyleSheet(f"color: {style.TEXT_DIM};")
        bar.addWidget(self.neigh_status, stretch=1)
        hint = QLabel("click IP or MAC cell")
        hint.setStyleSheet(f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};")
        bar.addWidget(hint)
        lay.addLayout(bar)

        cols = ("Identity", "Address", "MAC Address", "Board", "Version", "Uptime")
        self.neighbors = QTableWidget(0, len(cols))
        self.neighbors.setHorizontalHeaderLabels([tr(c) for c in cols])
        self._style_table(self.neighbors)
        self.neighbors.cellClicked.connect(self._pick_neighbor_cell)
        self.neighbors.cellDoubleClicked.connect(self._double_neighbor)
        self.neighbors.customContextMenuRequested.connect(self._neighbors_menu)
        self.neighbors.horizontalHeader().sectionClicked.connect(
            self._sort_neighbors_by
        )
        self.neighbors.currentCellChanged.connect(
            lambda row, col, _r, _c: self.neighbors.hasFocus()
            and self._pick_neighbor_cell(row, col)
        )
        self.neighbors.installEventFilter(self)  # Enter connects
        lay.addWidget(self.neighbors)
        self.tabs.addTab(page, "Neighbors")

    def _start_neighbor_scan(self) -> None:
        """Scan now, and report the result on the status line (Refresh)."""
        self._scan_announce = True
        self._scan_neighbors()

    def _rescan_neighbors(self) -> None:
        """The timer's scan: only the Neighbors tab hears about it."""
        if self._busy_cursor:  # a connect is running; the list is disabled
            return
        self._scan_neighbors()

    def _scan_neighbors(self) -> None:
        # Refresh stays enabled through a scan: with one every few seconds,
        # greying it for each made it flicker. Pressed mid-scan, it just has
        # that scan announce its result.
        if self._worker is not None and self._worker.isRunning():
            return
        if not self._neigh_seen:
            self.neigh_status.setText("Scanning MNDP (UDP 5678)…")
        self._worker = _NeighborWorker(timeout=2.5, parent=self)
        self._worker.finished_ok.connect(self._on_neighbors)
        self._worker.finished_err.connect(self._on_neighbors_err)
        self._worker.start()

    def _merge_neighbors(self, rows: list) -> None:
        """Fold one scan's replies into what has been heard, and age it."""
        now = time.monotonic()
        for n in rows:
            self._neigh_seen[n.mac] = (n, now)
        self._neigh_seen = {
            mac: seen
            for mac, seen in self._neigh_seen.items()
            if now - seen[1] <= NEIGHBOR_MAX_AGE_S
        }
        # Unsorted means the order a single scan gave: by identity.
        self._neighbor_rows = sorted(
            (n for n, _at in self._neigh_seen.values()),
            key=lambda n: (n.identity.lower(), n.address),
        )
        self._fill_neighbor_table()

    def _on_neighbors(self, rows: list) -> None:
        self._merge_neighbors(rows)
        n = len(self._neighbor_rows)
        self.neigh_status.setText(trf("{n} neighbor(s)", n=n) if n else "no neighbors found")
        if self._scan_announce:
            self._scan_announce = False
            self.status.setText(trf(
                "MNDP: {n} device(s) — click Address (IP) or MAC Address to select", n=n
            ))

    def _fill_neighbor_table(self) -> None:
        """Repaint the neighbour table in the current sort order.

        Sorts _neighbor_rows in place, because _pick_neighbor_cell indexes
        straight into it by view row.
        """
        if self._neigh_sort is not None:
            col, desc = self._neigh_sort
            field = NEIGH_SORT_FIELD.get(col)
            if field is not None:
                self._neighbor_rows.sort(
                    key=lambda n: sort_key(getattr(n, field, "")), reverse=desc
                )
        rows = self._neighbor_rows
        # A rescan repaints every few seconds, so whatever the user had --
        # the cell picked, the scroll position, column widths dragged by
        # hand -- has to come through it. Rows are matched by MAC.
        table = self.neighbors
        cur = table.currentItem()
        cur_mac = table.item(cur.row(), COL_MAC) if cur is not None else None
        keep = (cur_mac.text(), cur.column()) if cur_mac is not None else None
        before = {
            table.item(r, COL_MAC).text()
            for r in range(table.rowCount())
            if table.item(r, COL_MAC) is not None
        }
        scroll = table.verticalScrollBar().value()
        self.neighbors.setRowCount(len(rows))
        link_brush = QBrush(QColor(style.LINK))
        saved_brush = QBrush(QColor(46, 160, 67, 40))  # subtle green, both themes
        for r, n in enumerate(rows):
            # "We have creds for this" — match the discovered MAC to a stored
            # device fingerprint (survives the device's IP changing).
            dev = self._store.device_by_mac(n.mac) if getattr(n, "mac", "") else None
            ident = f"★ {n.identity}" if dev else n.identity
            saved_tip = ""
            if dev:
                who = dev.get("login") or "?"
                whost = dev.get("host") or ""
                saved_tip = trf("Saved credentials for this device (as {who})",
                                who=f"{who}@{whost}")
            values = (ident, n.address, n.mac, n.board, n.version, n.uptime)
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                # Highlight clickable connect columns
                if c in (COL_ADDRESS, COL_MAC) and val:
                    item.setForeground(link_brush)
                    tip = (
                        "Click to connect via TCP (IP)"
                        if c == COL_ADDRESS
                        else "Click to connect via MAC-Winbox (UDP 20561)"
                    )
                    item.setToolTip(tip)
                if dev:
                    item.setBackground(saved_brush)
                    if not item.toolTip():
                        item.setToolTip(saved_tip)
                self.neighbors.setItem(r, c, item)
        if keep is not None:
            at = next((r for r, n in enumerate(rows) if n.mac == keep[0]), None)
            # Quietly: a rescan putting the cursor back is not the user
            # picking that device again over whatever they typed since.
            table.blockSignals(True)
            if at is None:
                table.setCurrentItem(None)
            else:
                table.setCurrentCell(at, keep[1])
            table.blockSignals(False)
        table.verticalScrollBar().setValue(scroll)
        if {n.mac for n in rows} != before:
            table.resizeColumnsToContents()
        # Who is on the LAN changed, so which saved entries are live did too,
        # and whether the mode line can offer the other way in.
        self._paint_liveness()
        self._update_mode_label()

    def _on_neighbors_err(self, msg: str) -> None:
        # One failed scan is not every device leaving: keep the rows, and
        # let them age out if the failures go on.
        self._merge_neighbors([])
        self.neigh_status.setText(trf("scan failed: {msg}", msg=msg))
        if self._scan_announce:
            self._scan_announce = False
            self.status.setText(trf("MNDP error: {msg}", msg=msg))

    def _pick_neighbor_cell(self, row: int, col: int) -> None:
        if row < 0 or row >= len(self._neighbor_rows):
            return
        n = self._neighbor_rows[row]
        self._pending_identity = n.identity or "router"
        self._pending_board = n.board or ""
        self._pending_version = n.version or ""
        self._mac = n.mac or ""

        # Picking a device means "I mean this one", so the descriptive fields
        # have to follow it — otherwise a Note and Group left over from a row
        # picked earlier on the Addresses tab get saved onto this device. The
        # identity is the obvious default note; MNDP knows nothing of groups.
        self.note.setText(n.identity or "")
        self.group.setText("")

        # A ★ row is a box logged in to before; the login it was saved under
        # is what the star promises. The password comes only from a saved
        # entry -- without one the field is left as typed, as Winbox does.
        dev = self._store.device_by_mac(n.mac) if n.mac else None
        if dev and dev.get("login"):
            self.user.setText(dev["login"])
            saved = self._saved_connection(dev.get("host", ""), dev["login"])
            if saved is not None:
                self.password.setText(saved.password)
                self.note.setText(saved.note or n.identity or "")
                self.group.setText(saved.group)

        # Column decides transport; Identity/other → prefer IP then MAC
        if col == COL_MAC and n.mac:
            self._set_mac_target(n.mac)
        elif col == COL_ADDRESS and n.address:
            self._set_ip_target(n.address, mac=n.mac)
        elif n.address:
            self._set_ip_target(n.address, mac=n.mac)
        elif n.mac:
            self._set_mac_target(n.mac)

    def _saved_connection(self, host: str, login: str) -> cs.Connection | None:
        want = cs.entry_key(host, login)
        return next((c for c in self._merged if c.key == want), None)

    def _set_ip_target(self, address: str, *, mac: str = "") -> None:
        self._transport = "tcp"
        self._mac = mac
        self.host.blockSignals(True)
        self.host.setText(address)
        self.host.blockSignals(False)
        self._update_mode_label()

    def _set_mac_target(self, mac: str) -> None:
        self._transport = "mac"
        self._mac = mac
        self.host.blockSignals(True)
        self.host.setText(mac)
        self.host.blockSignals(False)
        self._update_mode_label()

    def _mode_alternative(self) -> tuple[str, str, NeighborRow] | None:
        """The other way to reach what Connect To names: (transport, target, neighbour).

        Only for a device MNDP can hear right now. MAC-Winbox needs it on this
        LAN anyway, and going by what is heard -- not by the MAC remembered
        from the last pick -- means a retyped address never offers another
        box's MAC.
        """
        text = self.host.text().strip()
        if not text:
            return None
        if self._transport == "mac":
            want = text.lower()
            n = next((n for n in self._neighbor_rows if n.mac.lower() == want), None)
            return ("tcp", n.address, n) if n is not None and n.address else None
        ip, _port = split_host_port(text)
        n = next((n for n in self._neighbor_rows if n.address == ip), None)
        if n is None:
            # A saved entry for the box at its old address still knows its MACs.
            dev = self._store.device_for(text, self.user.text())
            macs = {m.lower() for m in (dev.get("macs") or [])} if dev else set()
            n = next((n for n in self._neighbor_rows if n.mac.lower() in macs), None)
        return ("mac", n.mac, n) if n is not None and n.mac else None

    def _update_mode_label(self) -> None:
        if self._transport == "mac":
            line = trf("Mode: MAC-Winbox (UDP 20561)  ·  {target}",
                       target=self._mac or self.host.text())
            css = f"color: {style.LINK}; font-size: {style.small_css(9)}; font-weight: bold;"
        else:
            _host, port = split_host_port(self.host.text())
            line = trf("Mode: TCP / IP (port {port})", port=port)
            css = f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};"
        alt = self._mode_alternative()
        link = ""
        if alt is not None:
            how = "MAC" if alt[0] == "mac" else "IP"
            link = (
                f'  ·  <a href="swap" style="color: {style.LINK};">'
                + trf("use {how} {target}", how=how, target=html.escape(alt[1]))
                + "</a>"
            )
        self.mode_label.setText(verbatim(html.escape(line) + link))
        self.mode_label.setStyleSheet(css)
        self.mode_label.setToolTip(
            trf("Connect to {name} by {way} instead",
                name=alt[2].identity or tr("this device"),
                way="MAC-Winbox" if alt[0] == "mac" else "TCP/IP")
            if alt is not None else ""
        )
        # Reachable by Tab only while there is a link to follow.
        self.mode_label.setFocusPolicy(
            Qt.FocusPolicy.TabFocus if alt is not None else Qt.FocusPolicy.NoFocus
        )

    def _swap_transport(self) -> None:
        """Follow the mode line's link: the same device, the other way."""
        alt = self._mode_alternative()
        if alt is None or self._busy_cursor:
            return
        transport, target, n = alt
        if transport == "mac":
            self._set_mac_target(target)
        else:
            self._set_ip_target(target, mac=n.mac)
        self.error_banner.hide()
        self._refresh_save_hint()
        self.status.setText(trf(
            "{name}: by {way} now", name=n.identity or target,
            way="MAC-Winbox" if transport == "mac" else "TCP/IP",
        ))

    def _on_host_edited(self, text: str) -> None:
        """If user types a MAC, switch mode automatically."""
        from x3.proto.macwinbox import looks_like_mac

        t = text.strip()
        if looks_like_mac(t):
            self._transport = "mac"
            self._mac = t
        else:
            self._transport = "tcp"
        self._update_mode_label()
        self._refresh_save_hint()

    def _double_neighbor(self, row: int, col: int) -> None:
        self._pick_neighbor_cell(row, col)
        self._connect()

    # --- Addresses (cdb) tab --------------------------------------------

    def _build_addresses_tab(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 4, 2, 2)
        lay.setSpacing(2)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.setContentsMargins(0, 0, 0, 0)
        self.btn_open_cdb = QPushButton("Open…")
        self.btn_open_cdb.setMinimumWidth(72)
        self.btn_open_cdb.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.btn_open_cdb.clicked.connect(self._browse_cdb)
        bar.addWidget(self.btn_open_cdb)
        self.cdb_filter = QLineEdit()
        self.cdb_filter.setPlaceholderText("filter address / user / note…")
        a11y.name(self.cdb_filter, "Filter addresses")
        self.cdb_filter.setClearButtonEnabled(True)
        self.cdb_filter.textChanged.connect(self._apply_cdb_filter)
        # Down from the filter walks into what it found.
        self.cdb_filter.installEventFilter(self)

        # A fleet book is mostly read by group, and the column only sorted
        # them together; this narrows the list to one.
        self.group_filter = QComboBox()
        self.group_filter.addItem(tr(ALL_GROUPS))
        a11y.name(self.group_filter, "Filter by group")
        self.group_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.group_filter.currentIndexChanged.connect(
            lambda _i: self._apply_cdb_filter(self.cdb_filter.text())
        )
        self.group_filter.installEventFilter(self)  # Enter does nothing
        bar.addWidget(self.group_filter)
        bar.addWidget(self.cdb_filter, stretch=1)

        # Save lives with the form, not here: it reads the fields above, and
        # putting it on this tab meant a device found under Neighbors could not
        # be saved without switching tabs first. Remove and Revert do belong
        # here — they act on the row selected in this list.
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setToolTip(
            "Drop the selected connection from this list.\n"
            "A Addresses.cdb entry is only hidden locally; the file is untouched."
        )
        self.btn_remove.clicked.connect(self._remove_entry)
        bar.addWidget(self.btn_remove)

        self.btn_revert = QPushButton("Revert")
        self.btn_revert.setToolTip(
            "Discard local edits to the selected entry and show what "
            "Addresses.cdb says again."
        )
        self.btn_revert.clicked.connect(self._revert_entry)
        bar.addWidget(self.btn_revert)
        lay.addLayout(bar)

        self.cdb_path_label = QLabel("")
        self.cdb_path_label.setStyleSheet(f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};")
        self.cdb_path_label.setWordWrap(True)
        lay.addWidget(self.cdb_path_label)

        cols = ("", "Address", "User", "Password", "Note", "Group", "Last Used")
        self.addresses = QTableWidget(0, len(cols))
        self.addresses.setHorizontalHeaderLabels([tr(c) for c in cols])
        # Every cell of a saved connection picks the same entry, so highlight
        # the whole row (unlike Neighbors, where the column chooses transport).
        self._style_table(self.addresses, select_rows=True)
        # Address is the long, elidable column: let it absorb the slack so the
        # short trailing ones always stay on screen.
        ahdr = self.addresses.horizontalHeader()
        ahdr.setStretchLastSection(False)
        ahdr.setSectionResizeMode(COL_ADDR_HOST, QHeaderView.ResizeMode.Stretch)
        ahdr.resizeSection(COL_ADDR_FLAG, 22)
        ahdr.setSectionResizeMode(COL_ADDR_FLAG, QHeaderView.ResizeMode.Fixed)
        ahdr.sectionClicked.connect(self._sort_addresses_by)
        self.addresses.cellClicked.connect(self._pick_address)
        self.addresses.cellDoubleClicked.connect(self._double_address)
        self.addresses.customContextMenuRequested.connect(self._addresses_menu)
        self.addresses.itemSelectionChanged.connect(self._refresh_entry_buttons)
        # Arrowing through the list loads each entry, as a click does --
        # which is also what lets Enter connect to the row it stands on.
        self.addresses.currentCellChanged.connect(
            lambda row, col, _r, _c: self.addresses.hasFocus() and self._pick_address(row, col)
        )
        # Typing into the list types into the filter; Enter connects.
        self.addresses.installEventFilter(self)
        lay.addWidget(self.addresses)
        self.tabs.addTab(page, "Addresses")

    def _load_cdb(self, path: Path | None = None) -> bool:
        """Load the address book; False when it could not be read."""
        use = path if path is not None else self._cdb_path
        resolved, rows, err = load_address_book(use, include_passwords=True)
        self._cdb_path = resolved
        self._address_rows = rows
        if err:
            self._merged = self._store.merge([])
            self._filtered_view = list(self._merged)
            self.cdb_path_label.setText(err)
            self._fill_address_table(self._merged)
            if use is None and find_addresses_cdb() is None:
                self.status.setText(err)
            else:
                self.status.setText(f"cdb: {err}")
            return False
        assert resolved is not None
        self.cdb_path_label.setText(str(resolved))
        self._remerge()
        if self._merged:
            self.tabs.setCurrentIndex(1)
        return True

    def _remerge(self) -> None:
        """Re-read the overlay onto the cdb rows and repaint the table.

        The cdb is the fresh copy from disk every time, so anything Winbox
        changed shows through for every field we are not overriding.
        """
        self._merged = self._store.merge(self._address_rows)
        self._refill_groups()
        self._host_model.setStringList(
            list(dict.fromkeys(c.host for c in self._merged if c.host))
        )
        self._apply_cdb_filter(self.cdb_filter.text())

    def _refill_groups(self) -> None:
        """The Group filter's choices, keeping the one picked if it survives."""
        current = self.group_filter.currentText()
        groups = sorted({c.group for c in self._merged if c.group}, key=sort_key)
        self.group_filter.blockSignals(True)
        self.group_filter.clear()
        self.group_filter.addItem(tr(ALL_GROUPS))
        self.group_filter.addItems(groups)
        self.group_filter.setCurrentIndex(max(0, self.group_filter.findText(current)))
        self.group_filter.blockSignals(False)
        # Nothing to choose between: leave the space to the text filter.
        self.group_filter.setVisible(bool(groups))

    def _address_count_text(self) -> str:
        """Winbox's '3 items out of 342' — says how much the filter is hiding."""
        shown, total = len(self._filtered_view), len(self._merged)
        local = sum(1 for c in self._merged if c.origin != "cdb")
        extra = " " + trf("· {n} local", n=local) if local else ""
        if not total:
            return tr("no saved connections")
        if shown == total:
            return verbatim(trf("{total} saved connection(s)", total=total) + extra)
        return verbatim(
            trf("{shown} of {total} saved connections", shown=shown, total=total) + extra
        )

    def _fill_address_table(self, rows: list[cs.Connection]) -> None:
        self._filtered_view = list(rows)
        self.addresses.setRowCount(len(rows))
        for r, a in enumerate(rows):
            last = self._last_used(a)
            values = (
                a.flag, a.host, a.login, self._password_cell(a), a.note, a.group,
                ago(last),
            )
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                if c == COL_ADDR_LAST and last:
                    item.setToolTip(time.strftime("%Y-%m-%d %H:%M", time.localtime(last)))
                if c == COL_ADDR_FLAG and a.flag:
                    item.setToolTip(a.origin_help)
                if c == COL_ADDR_PASS and a.password:
                    item.setToolTip(
                        a.password
                        if self._show_passwords
                        else "Show is off — tick it, or right-click ▸ Copy Password"
                    )
                self.addresses.setItem(r, c, item)
        self._paint_liveness()
        hdr = self.addresses.horizontalHeader()
        for col in (COL_ADDR_USER, COL_ADDR_PASS, COL_ADDR_NOTE, COL_ADDR_GROUP,
                    COL_ADDR_LAST):
            self.addresses.resizeColumnToContents(col)
        if hdr.sectionSize(COL_ADDR_NOTE) > 180:
            hdr.resizeSection(COL_ADDR_NOTE, 180)
        if hdr.sectionSize(COL_ADDR_PASS) > 180:
            hdr.resizeSection(COL_ADDR_PASS, 180)
        self._refresh_entry_buttons()

    def _last_used(self, a: cs.Connection) -> int:
        dev = self._store.device_for(a.host, a.login)
        return int(dev.get("seen_at") or 0) if dev else 0

    def _live_neighbor(self, a: cs.Connection) -> NeighborRow | None:
        """The neighbour this entry is, if it is on the LAN right now.

        By address or MAC, and by the MACs its device record holds -- so an
        entry still matches after DHCP moved the box to another IP.
        """
        if not self._neighbor_rows:
            return None
        host = a.host.strip().lower()
        dev = self._store.device_for(a.host, a.login)
        macs = {m.lower() for m in (dev.get("macs") or [])} if dev else set()
        for n in self._neighbor_rows:
            mac = (n.mac or "").lower()
            if host and host in (n.address.lower(), mac):
                return n
            if mac and mac in macs:
                return n
        return None

    def _paint_liveness(self) -> None:
        """Mark the Address cell of each entry that MNDP can hear.

        An icon rather than a character, so Ctrl+C on the cell still copies
        the address alone. Repainted in place when a rescan lands, which
        leaves widths, selection and scroll where the user put them.
        """
        px = style.scale_px(10)
        self.addresses.setIconSize(QSize(px, px))
        live, blank = dot_icon(style.MARK_UP, px), dot_icon(None, px)
        for r, a in enumerate(self._filtered_view):
            item = self.addresses.item(r, COL_ADDR_HOST)
            if item is None:
                continue
            n = self._live_neighbor(a)
            item.setIcon(live if n is not None else blank)
            item.setToolTip(
                trf("On the LAN now: {name} ({address})",
                    name=n.identity or n.mac, address=n.address or n.mac)
                if n is not None else ""
            )

    def _password_cell(self, a) -> str:
        """What the Password column shows for this entry."""
        if not a.password:
            return ""
        return a.password if self._show_passwords else PASSWORD_MASK

    def _apply_cdb_filter(self, text: str) -> None:
        needle = text.strip().lower()
        group = self.group_filter.currentText()
        # By position, not text: the first entry is "All groups" in any language.
        rows = (
            self._merged if self.group_filter.currentIndex() <= 0
            else [a for a in self._merged if a.group == group]
        )
        if needle:
            rows = [
                a
                for a in rows
                if needle in a.host.lower()
                or needle in a.login.lower()
                or needle in a.note.lower()
                or needle in a.group.lower()
            ]
        self._fill_address_table(self._sorted_addresses(list(rows)))
        self.status.setText(self._address_count_text())

    # --- sorting ---------------------------------------------------------

    def _sorted_addresses(self, rows: list[cs.Connection]) -> list[cs.Connection]:
        """Apply the header sort. Unsorted means Addresses.cdb's own order.

        Sorting is done on our list rather than through QTableWidget's own
        setSortingEnabled: that sorts the *items* lexically and would leave
        _filtered_view — which every row lookup indexes into — pointing at the
        wrong entries.
        """
        if self._addr_sort is None:
            return rows
        col, desc = self._addr_sort
        field = ADDR_SORT_FIELD.get(col)
        if field is None:
            return rows
        if col == COL_ADDR_LAST:
            return sorted(rows, key=self._last_used, reverse=desc)
        return sorted(rows, key=lambda a: sort_key(getattr(a, field, "")), reverse=desc)

    def _sort_addresses_by(self, col: int) -> None:
        self._addr_sort = self._next_sort(self._addr_sort, col, ADDR_SORT_FIELD)
        self._show_sort_indicator(self.addresses, self._addr_sort)
        self._apply_cdb_filter(self.cdb_filter.text())

    def _sort_neighbors_by(self, col: int) -> None:
        self._neigh_sort = self._next_sort(self._neigh_sort, col, NEIGH_SORT_FIELD)
        self._show_sort_indicator(self.neighbors, self._neigh_sort)
        self._fill_neighbor_table()

    @staticmethod
    def _next_sort(
        current: tuple[int, bool] | None, col: int, fields: dict
    ) -> tuple[int, bool] | None:
        """Ascending → descending → back to file order, same as the lists."""
        if col not in fields:
            return current
        if current is None or current[0] != col:
            return (col, False)
        if not current[1]:
            return (col, True)
        return None

    @staticmethod
    def _show_sort_indicator(table: QTableWidget, state: tuple[int, bool] | None) -> None:
        hdr = table.horizontalHeader()
        hdr.setSortIndicatorShown(True)
        if state is None:
            hdr.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        else:
            col, desc = state
            hdr.setSortIndicator(
                col,
                Qt.SortOrder.DescendingOrder if desc else Qt.SortOrder.AscendingOrder,
            )

    # --- saving (our store, never the cdb) --------------------------------

    def _selected_connection(self) -> cs.Connection | None:
        rows = {i.row() for i in self.addresses.selectedIndexes()}
        if len(rows) != 1:
            return None
        return self._visible_address(next(iter(rows)))

    def _refresh_entry_buttons(self) -> None:
        conn = self._selected_connection()
        self.btn_remove.setEnabled(conn is not None)
        self.btn_revert.setEnabled(conn is not None and conn.origin == "local")
        self._refresh_save_hint()

    def _save_target(self) -> cs.Connection | None:
        """The entry Save would update, or None if it would add a new one.

        Mirrors the decision in :meth:`_save_entry`: the selected row counts
        only while the form still names it. Retype the address and you mean a
        different device, not a rename of this one.
        """
        conn = self._selected_connection()
        if conn is None:
            return None
        form = cs.entry_key(self.host.text(), self.user.text())
        return conn if cs.entry_key(conn.host, conn.login) == form else None

    def _refresh_save_hint(self) -> None:
        """Say which of the two things Save is about to do, before it does it."""
        if not self.host.text().strip() or not self.user.text().strip():
            self._save_hint.setText("")
            return
        target = self._save_target()
        if target is None:
            self._save_hint.setText("→ adds a new entry")
        else:
            self._save_hint.setText(
                trf("→ updates {host} ({login})", host=target.host, login=target.login)
            )

    def _form_values(self) -> dict[str, str]:
        return {
            "host": self.host.text().strip(),
            "login": self.user.text().strip(),
            "password": self.password.text(),
            "note": self.note.text().strip(),
            "group": self.group.text().strip(),
            "session": "",
        }

    def _save_entry(self) -> None:
        values = self._form_values()
        if not values["host"] or not values["login"]:
            QMessageBox.warning(
                self,
                "Save",
                "A saved connection needs both an address and a login — "
                "together they are what identifies it.",
            )
            return
        # Editing the selected row only when the form still refers to it;
        # otherwise the user retyped the address and means a new entry.
        original = self._save_target()
        if original is not None:
            values["session"] = original.session
        saved = self._store.set_entry(values, original=original)
        cs.save(self._store)
        self._remerge()
        self._select_key(saved.key)
        if saved.origin == "local":
            said = trf("Updated {host} as {login} — in connections.json, "
                       "Addresses.cdb untouched", host=saved.host, login=saved.login)
        else:
            said = trf("Saved {host} as {login} — in connections.json, "
                       "Addresses.cdb untouched", host=saved.host, login=saved.login)
        self.status.setText(said)

    def _remove_entry(self) -> None:
        conn = self._selected_connection()
        if conn is None:
            return
        if conn.origin == "new":
            question = trf("Delete {host} ({login})?\n\nThis entry is ours alone.",
                           host=conn.host, login=conn.login)
        else:
            question = trf(
                "Hide {host} ({login}) from this list?\n\n"
                "It stays in Addresses.cdb — other clients will still show it, and "
                "right-click ▸ Restore Hidden brings it back here.",
                host=conn.host, login=conn.login,
            )
        if QMessageBox.question(self, "Remove", question) != QMessageBox.StandardButton.Yes:
            return
        self._store.remove(conn)
        cs.save(self._store)
        self._remerge()
        self.status.setText(trf("Removed {host} ({login}) from this list",
                                host=conn.host, login=conn.login))

    def _revert_entry(self) -> None:
        conn = self._selected_connection()
        if conn is None:
            return
        if not self._store.revert(conn):
            return
        cs.save(self._store)
        self._remerge()
        self.status.setText(trf("Reverted {host} to what Addresses.cdb says",
                                host=conn.host))

    def _restore_hidden(self) -> None:
        n = self._store.unhide_all()
        if not n:
            return
        cs.save(self._store)
        self._remerge()
        self.status.setText(
            trf("Restored 1 hidden Addresses.cdb entry") if n == 1
            else trf("Restored {n} hidden Addresses.cdb entries", n=n)
        )

    def _select_key(self, key) -> None:
        for row, conn in enumerate(self._filtered_view):
            if conn.key == key:
                self.addresses.selectRow(row)
                return

    def _show_update(self, release) -> None:
        self.update_button.setText(trf("x3 {version} available…", version=release.version))
        self.update_button.setToolTip("A newer x3 is out: see what changed, and update")
        self.update_button.show()

    def _open_update(self) -> None:
        from .update_dialog import UpdateDialog, notifier

        if notifier.release is not None:
            UpdateDialog(notifier.release, self).exec()

    def _browse_cdb(self) -> None:
        start = str(self._cdb_path.parent) if self._cdb_path else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Addresses.cdb",
            start,
            "Address book (*.cdb);;All files (*)",
        )
        # Remembered only once it loaded: a file that failed to parse must not
        # become where every later start looks first.
        if path and self._load_cdb(Path(path)):
            save_addresses_cdb(path)

    def _visible_address(self, row: int) -> cs.Connection | None:
        view = self._filtered_view
        if row < 0 or row >= len(view):
            return None
        return view[row]

    def _pick_address(self, row: int, _col: int) -> None:
        a = self._visible_address(row)
        if a is None:
            return
        self._load_connection(a)

    def _load_connection(self, a: cs.Connection) -> None:
        """Fill the form from a saved entry."""
        if a.host:
            self.host.setText(a.host)
        # Set unconditionally, password included: a field left over from the
        # row picked before would be sent to this device on Connect, or saved
        # onto this entry by Save. "********" is a cdb read without secrets.
        self.user.setText(a.login)
        self.password.setText("" if a.password == "********" else a.password)
        self.note.setText(a.note)
        self.group.setText(a.group)
        self._pending_identity = a.note or a.host or "router"
        self._pending_board = ""
        self._pending_version = ""
        # Host field may be MAC or IP:hostport — _on_host_edited handles mode
        self._on_host_edited(self.host.text())
        self._refresh_entry_buttons()

    def _double_address(self, row: int, col: int) -> None:
        self._pick_address(row, col)
        self._connect()

    # --- shared ---------------------------------------------------------

    def _style_table(self, table: QTableWidget, *, select_rows: bool = False) -> None:
        behavior = (
            QTableWidget.SelectionBehavior.SelectRows
            if select_rows
            else QTableWidget.SelectionBehavior.SelectItems
        )
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(style.ROW_HEIGHT)
        table.setSelectionBehavior(behavior)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(True)
        table.setSortingEnabled(False)
        # Tab must leave the table for the connect form, not walk cell to cell.
        table.setTabKeyNavigation(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setWordWrap(False)
        style.style_data_table(table)

        # Ctrl+C on the cell under the cursor. SelectRows tables highlight the
        # whole row, but currentItem() still tracks the cell that was clicked.
        sc = QShortcut(QKeySequence.StandardKey.Copy, table)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(lambda t=table: self._copy_current_cell(t))
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    # --- copy helpers ----------------------------------------------------

    def _to_clipboard(self, text: str, what: str) -> None:
        QGuiApplication.clipboard().setText(text)
        # `what` is a word of the table's ("password"), or a column header the
        # table already turned: the table holds the capitalised form.
        word = what[:1].upper() + what[1:]
        shown = tr(word)
        if shown != word:
            what = shown[:1].lower() + shown[1:]
        self.status.setText(trf("Copied {what} to clipboard", what=what))

    def _copy_current_cell(self, table: QTableWidget) -> None:
        item = table.currentItem()
        if item is None or not item.text():
            return
        header = table.horizontalHeaderItem(item.column())
        self._to_clipboard(item.text(), (header.text() if header else "cell").lower())

    def _neighbors_menu(self, pos) -> None:
        item = self.neighbors.itemAt(pos)
        if item is None:
            return
        self.neighbors.setCurrentItem(item)
        n = self._neighbor_rows[item.row()] if item.row() < len(self._neighbor_rows) else None
        menu = QMenu(self.neighbors)
        menu.addAction("Copy Cell", lambda: self._copy_current_cell(self.neighbors))
        if n is not None:
            menu.addSeparator()
            if n.address:
                menu.addAction("Copy Address", lambda: self._to_clipboard(n.address, "address"))
            if n.mac:
                menu.addAction("Copy MAC", lambda: self._to_clipboard(n.mac, "MAC"))
        menu.exec(self.neighbors.viewport().mapToGlobal(pos))

    def _addresses_menu(self, pos) -> None:
        item = self.addresses.itemAt(pos)
        a = self._visible_address(item.row()) if item is not None else None
        menu = QMenu(self.addresses)
        # Row actions only on a row; the rest work from the empty area too,
        # which matters once Remove has hidden the last row there was.
        if a is not None:
            self.addresses.setCurrentItem(item)
            menu.addAction("Copy Cell", lambda: self._copy_current_cell(self.addresses))
            menu.addSeparator()
            if a.host:
                menu.addAction("Copy Address", lambda: self._to_clipboard(a.host, "address"))
            if a.login:
                menu.addAction("Copy Login", lambda: self._to_clipboard(a.login, "login"))
            # Copies the real secret whether or not the column is masked — the
            # point of the entry is to hand you the password.
            act_pw = menu.addAction(
                "Copy Password", lambda: self._to_clipboard(a.password, "password")
            )
            act_pw.setEnabled(bool(a.password))
            menu.addSeparator()
        # The only way back for a hidden cdb row: it is not in the list, so
        # neither selecting it nor Revert can reach it.
        hidden = len(self._store.hidden)
        act_restore = menu.addAction(
            f"Restore Hidden ({hidden})" if hidden else "Restore Hidden",
            self._restore_hidden,
        )
        act_restore.setEnabled(bool(hidden))
        act_show = menu.addAction("Show Passwords")
        act_show.setCheckable(True)
        act_show.setChecked(self._show_passwords)
        act_show.toggled.connect(self.show_pw.setChecked)
        menu.exec(self.addresses.viewport().mapToGlobal(pos))

    # --- show passwords ---------------------------------------------------

    def _apply_password_echo(self) -> None:
        self.password.setEchoMode(
            QLineEdit.EchoMode.Normal
            if self._show_passwords
            else QLineEdit.EchoMode.Password
        )

    def _on_refresh_rate_changed(self, _index: int) -> None:
        """Save the cadence, and hand it to any list already on screen.

        The dialog opens before a session but can also be reopened from a live
        one (New x3 in the menu tree), so this is not only a pre-connection choice.
        """
        secs = self.refresh_rate.currentData()
        save_list_refresh_secs(secs)
        from .list_pane import ListPane

        app = QApplication.instance()
        for widget in (app.allWidgets() if app is not None else ()):
            if isinstance(widget, ListPane):
                try:
                    widget.apply_refresh_setting()
                except RuntimeError:  # a pane already on its way out
                    pass

    def _on_show_passwords_toggled(self, checked: bool) -> None:
        self._show_passwords = bool(checked)
        save_show_passwords(self._show_passwords)
        if self.show_pw.isChecked() != self._show_passwords:
            self.show_pw.setChecked(self._show_passwords)
        self._apply_password_echo()
        # Re-render the Password column against the current filter view.
        self._fill_address_table(self._filtered_view)

    def set_schema_dir(self, path: str | None) -> None:
        self._schema_dir = path

    def _on_theme_changed(self, _index: int) -> None:
        tid = self.theme_combo.currentData()
        if not tid:
            return
        theme = style.apply_theme(QApplication.instance(), str(tid), persist=True)
        self._refresh_inline_styles()
        self._refresh_metrics()
        self.status.setText(f"{tr('Theme:')} {tr(theme.name)} — {tr(theme.description)}")

    def _on_language_changed(self, _index: int) -> None:
        """Save the language; on the start page, rebuild the page in it.

        Nothing else is on screen yet, so app.py can build the page again in
        the new language and the session window after it comes up in it too.
        Opened from a session (New x3), the windows already open keep theirs,
        so it applies on the next connection page, as View ▸ Language does.
        """
        code = str(self.language_combo.currentData() or "auto")
        save_language(code)
        i18n.picked = code
        target = i18n.resolve(code)
        if target == i18n.active():
            self.status.setText(tr("Language saved."))
        elif not self._start_page:
            self.status.setText(
                f"{i18n.available().get(target, target)}: "
                + tr("x3 switches to this language when you return to the "
                     "connection page (File ▸ Disconnect).")
            )
        else:
            self._carried = self._carry()
            # A scan in flight runs out its 2.5 s on its own; joining it here
            # would freeze the page for that long, so it is let go instead.
            # No connect is running: the combo is disabled while one is.
            self._scan_timer.stop()
            qthreads.retire(self._worker, wait_ms=0)
            self._worker = None
            self._shutdown_workers()
            self.done(self.LANGUAGE_CHANGED)

    def _carry(self) -> dict:
        """What this page holds that is saved nowhere, for the page that
        replaces it: the form as typed, the list as filtered, where it sat."""
        selected = self._selected_connection()
        return {
            "geometry": self.saveGeometry(),
            "fields": {name: getattr(self, name).text()
                       for name in ("host", "user", "password", "note", "group")},
            "target": (self._transport, self._mac),
            "tab": self.tabs.currentIndex(),
            "filters": (self.group_filter.currentText(), self.cdb_filter.text()),
            "selected": selected.key if selected is not None else None,
            "neighbors": dict(self._neigh_seen),
        }

    def carried(self) -> dict | None:
        """The page's state, when it closed to be rebuilt in another language."""
        return getattr(self, "_carried", None)

    def restore(self, state: dict) -> None:
        """Put back what :meth:`carried` took from the page this one replaces."""
        self.restoreGeometry(state["geometry"])
        self.tabs.setCurrentIndex(state["tab"])
        group, text = state["filters"]
        self.group_filter.setCurrentIndex(max(0, self.group_filter.findText(group)))
        self.cdb_filter.setText(text)
        if state["selected"] is not None:
            self._select_key(state["selected"])
        # After the selection: picking a row fills the form, and the form as
        # typed is what is being restored.
        for name, text in state["fields"].items():
            field = getattr(self, name)
            field.blockSignals(True)
            field.setText(text)
            field.blockSignals(False)
        self._transport, self._mac = state["target"]
        self._update_mode_label()
        self._refresh_entry_buttons()
        self._neigh_seen.update(state["neighbors"])
        self._merge_neighbors([])
        self.status.setText(
            f"{tr('Language:')} {i18n.available().get(i18n.active(), i18n.active())}"
        )

    def _refresh_inline_styles(self) -> None:
        """Re-apply the styles the dialog set on individual widgets by hand.

        The app stylesheet repaints itself when the theme or the zoom changes,
        but these few labels baked ``TEXT_DIM`` and a ``small_css`` size in at
        build time, so they keep the old skin's colour and the old zoom's size
        until they are set again. Shared by the Theme combo and the zoom keys.
        """
        dim = f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};"
        self._update_mode_label()
        self.status.setStyleSheet(dim)
        self.neigh_status.setStyleSheet(f"color: {style.TEXT_DIM};")
        self._save_hint.setStyleSheet(f"color: {style.TEXT_DIM};")
        self.error_banner.setStyleSheet(f"color: {style.INFO_RED};")
        self.cdb_path_label.setStyleSheet(dim)
        for lab in self._bottom_labels:
            lab.setStyleSheet(f"color: {style.TEXT_DIM};")
        # The clickable cells carry the link colour per item, and the new
        # type wants new widths.
        self._fill_neighbor_table()
        self.neighbors.resizeColumnsToContents()

    def _refresh_metrics(self) -> None:
        """Re-measure the two tables after the theme's *sizes* moved.

        A table's row height is set once, from ``style.ROW_HEIGHT``, at build
        time (see :meth:`_style_table`) -- the stylesheet does not reach it. So
        a live zoom or skin change grew the font while the rows stayed the size
        they were built at: text ran over the row above at a big zoom, and huge
        gaps opened on the way back down as the stale font-derived minimum held
        rows taller than the new default. This is the connection page's small
        echo of ``MainWindow._refresh_open_metrics``.

        The minimum comes down to 1 first for the reason ListPane does it:
        QHeaderView derives a minimum from the font and clamps every section to
        it, so without this a row could not follow ``ROW_HEIGHT`` back down.
        """
        for table in (getattr(self, "neighbors", None), getattr(self, "addresses", None)):
            if table is None:
                continue
            style.style_data_table(table)
            vh = table.verticalHeader()
            vh.setMinimumSectionSize(1)
            vh.setDefaultSectionSize(style.ROW_HEIGHT)

    def _zoom_step(self, delta: int) -> None:
        """Ctrl+= / Ctrl+- on the connection page.

        The zoom keys live on MainWindow, which does not exist yet here -- so
        the connection page, the first thing anyone sees, would be the one
        screen the zoom did not reach. It re-applies the *current* skin at the
        new factor and re-measures the two tables (their row height is set on
        the widget, not by the sheet) plus the hand-styled labels.
        """
        old = style.ui_scale()
        scale = style.step_scale(delta)
        if abs(scale - old) < 1e-6:
            return
        theme = style.apply_theme(
            QApplication.instance(), style.base_theme().id, scale=scale
        )
        self._refresh_inline_styles()
        self._refresh_metrics()
        style.scale_window(self, scale / old)
        self.status.setText(
            trf("Zoom: {pct}% — {theme}", pct=round(scale * 100), theme=tr(theme.name))
        )

    def _zoom_reset(self) -> None:
        old = style.ui_scale()
        if abs(old - 1.0) < 1e-6:
            return
        theme = style.apply_theme(
            QApplication.instance(), style.base_theme().id, scale=1.0
        )
        self._refresh_inline_styles()
        self._refresh_metrics()
        style.scale_window(self, 1.0 / old)
        self.status.setText(trf("Zoom: {pct}% — {theme}", pct=100, theme=tr(theme.name)))

    def _restore_last_connection(self) -> None:
        last = load_last_connection()
        if last.host:
            self.host.blockSignals(True)
            self.host.setText(last.host)
            self.host.blockSignals(False)
        if last.user:
            self.user.setText(last.user)
        if last.password:
            self.password.setText(last.password)
        self._transport = last.transport if last.transport in ("tcp", "mac") else "tcp"
        self._mac = last.mac or ""
        if self._transport == "mac" and not self._mac and last.host:
            self._mac = last.host
        self._on_host_edited(self.host.text())
        if last.host or last.user:
            self.status.setText(trf(
                "Restored last connection: {who} — click Connect",
                who=f"{last.user}@{last.host}",
            ))

    def _remember_connection(
        self, host: str, user: str, password: str, transport: str, mac: str
    ) -> None:
        try:
            save_last_connection(
                LastConnection(
                    host=host,
                    user=user,
                    password=password,
                    transport=transport,
                    mac=mac,
                )
            )
        except OSError:
            pass

    def _connect(self) -> None:
        host = self.host.text().strip()
        user = self.user.text().strip()
        if not host:
            QMessageBox.warning(
                self, "Connect", "Enter a host/MAC or select a neighbor / address."
            )
            return
        if not user:
            QMessageBox.warning(self, "Connect", "Enter a login name.")
            return

        self._on_host_edited(host)
        transport = self._transport
        password = self.password.text()
        mac = self._mac if transport == "mac" else ""

        tcp_host, tcp_port = (
            split_host_port(host) if transport == "tcp" else (host, DEFAULT_PORT)
        )

        if (self._connect_worker is not None and self._connect_worker.isRunning()) or (
            self._tryall_worker is not None and self._tryall_worker.isRunning()
        ):
            self.status.setText("Already connecting…")
            return

        target = tcp_host if transport == "tcp" else host
        self.error_banner.hide()
        self._pending_connect = {
            "host": host,
            "user": user,
            "password": password,
            "transport": transport,
            "mac": mac,
            "tcp_host": target,
            "tcp_port": tcp_port,
        }
        self._set_connect_ui_busy(True)

        if self.try_all.isChecked():
            # One sweep per tick. Left on, it turned every later Connect into
            # a sweep too -- every saved password offered to whichever box
            # was picked next, which nobody asked for twice.
            self.try_all.setChecked(False)
            self._start_tryall_worker(target, user, tcp_port, transport, password)
            return

        self.status.setText(trf(
            "Connecting to {target}…",
            target=f"{target}:{tcp_port}" if transport == "tcp" else f"{target} (MAC-Winbox)",
        ))
        self._start_connect_worker()

    def _start_connect_worker(self) -> None:
        """Launch the real (schema + info) connect from ``_pending_connect``."""
        pending = self._pending_connect or {}
        worker = _ConnectWorker(
            host=pending["tcp_host"],
            user=pending["user"],
            password=pending["password"],
            port=pending["tcp_port"],
            transport=pending["transport"],
            schema_dir=self._schema_dir,
            audit=self.audit_box.isChecked(),
            audit_level=self.audit_level.currentData() or load_audit_level(),
            parent=self,
        )
        self._connect_worker = worker
        worker.status.connect(self._on_connect_status)
        worker.finished_ok.connect(self._on_connect_ok)
        worker.finished_err.connect(self._on_connect_err)
        worker.start()

    def _candidate_passwords(self, first: str) -> list[str]:
        """Distinct, order-preserving saved passwords; the typed one leads."""
        seen: set[str] = set()
        out: list[str] = []
        for pw in [first] + [c.password for c in getattr(self, "_merged", [])]:
            pw = pw or ""
            if not pw or pw in seen:
                continue
            seen.add(pw)
            out.append(pw)
        return out

    def _start_tryall_worker(
        self, target: str, user: str, tcp_port: int, transport: str, typed_pw: str
    ) -> None:
        candidates = self._candidate_passwords(typed_pw)
        if not candidates:
            self._set_connect_ui_busy(False)
            QMessageBox.information(
                self,
                "Try all saved",
                "No saved passwords to try. Enter one, or save some connections "
                "first.",
            )
            return
        self.status.setText(trf(
            "Trying {n} saved password(s) against {target}…",
            n=len(candidates), target=target,
        ))
        worker = _TryCredsWorker(
            host=target,
            user=user,
            port=tcp_port,
            transport=transport,
            candidates=candidates,
            parent=self,
        )
        self._tryall_worker = worker
        worker.status.connect(self._on_connect_status)
        worker.found.connect(self._on_tryall_found)
        worker.exhausted.connect(self._on_tryall_exhausted)
        worker.not_secure.connect(self._on_tryall_not_secure)
        worker.error.connect(self._on_connect_err)
        worker.start()

    def _on_tryall_found(self, password: str) -> None:
        """A saved password authenticated: adopt it and connect for real."""
        self._tryall_worker = None
        if self._pending_connect is None:
            self._set_connect_ui_busy(False)
            return
        self._pending_connect["password"] = password
        self.password.setText(password)
        self.status.setText("Password found — connecting…")
        self._start_connect_worker()

    def _on_tryall_exhausted(self, tried: int) -> None:
        self._tryall_worker = None
        self._set_connect_ui_busy(False)
        self.status.setText(trf("No saved password worked ({n} tried).", n=tried))

    def _on_tryall_not_secure(self, detail: str) -> None:
        self._tryall_worker = None
        self._set_connect_ui_busy(False)
        self.status.setText("Not tried — device does not offer the secure handshake.")
        QMessageBox.warning(
            self,
            "Try all saved",
            trf("This device did not present the secure firmware 6.43+ (EC-SRP5) "
                "handshake, so no saved password was tried against it — none of your "
                "passwords were sent.\n\n"
                "Handshake result: {detail}", detail=detail),
        )

    def _set_connect_ui_busy(self, busy: bool) -> None:
        """Disable form controls while connect runs, but keep the dialog alive."""
        self._btn_connect.setEnabled(not busy)
        self.host.setEnabled(not busy)
        self.user.setEnabled(not busy)
        self.password.setEnabled(not busy)
        self.try_all.setEnabled(not busy)
        self.mode_label.setEnabled(not busy)
        self.tabs.setEnabled(not busy)
        # Changing it rebuilds this page, which a connect in flight would not survive.
        self.language_combo.setEnabled(not busy)
        if busy:
            self._connect_started = time.monotonic()
            self._elapsed_timer.start()
            self._btn_connect.setText("Connecting…")
            if not self._busy_cursor:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                self._busy_cursor = True
        else:
            self._elapsed_timer.stop()
            self._btn_connect.setText("Connect")
            if self._busy_cursor:
                QApplication.restoreOverrideCursor()
                self._busy_cursor = False

    def _paint_connect_elapsed(self) -> None:
        # On the button, not the status line: that one is spoken, and a
        # label a timer writes to would talk over itself once a second.
        secs = int(time.monotonic() - self._connect_started)
        self._btn_connect.setText(trf("Connecting… {secs}s", secs=secs))

    def _on_connect_status(self, text: str) -> None:
        # The client reports in English. The stage is found in that, and only
        # then is the line put in the user's language.
        stage = next(
            (i for i, name in enumerate(CONNECT_STAGES) if text.startswith(name)), None
        )
        shown = tr(text)
        dialing = re.match(r"Connecting to (.+)…$", text)
        if dialing:
            shown = trf("Connecting to {target}…", target=dialing.group(1))
        if stage is not None:
            shown = trf("{status}  (step {n} of {total})", status=shown,
                        n=stage + 1, total=len(CONNECT_STAGES))
        self.status.setText(verbatim(shown))

    def _on_connect_ok(self, client) -> None:
        self._set_connect_ui_busy(False)
        t_ok = time.perf_counter()
        pending = self._pending_connect or {}
        host = pending.get("host") or self.host.text().strip()
        user = pending.get("user") or self.user.text().strip()
        password = pending.get("password") or self.password.text()
        transport = pending.get("transport") or self._transport
        mac = pending.get("mac") or self._mac

        self._remember_connection(host, user, password, transport, mac)
        info = client.info
        self.session = SessionInfo(
            host=host,
            user=user,
            password=password,
            identity=info.identity or self._pending_identity or "router",
            board=info.board or self._pending_board or "?",
            ros_version=info.version or self._pending_version or "?",
            platform=info.platform or info.architecture or "?",
            cpu_load=info.cpu_load or "?",
            free_memory=info.free_memory or "?",
            total_memory=info.total_memory or "?",
            uptime=info.uptime or "?",
            transport=transport,
            mac=mac,
        )
        self.client = client
        # Trust-on-first-use device-identity check + fingerprint record.
        # Best-effort: a checker error must never block a real connection.
        try:
            proceed = self._verify_and_record_device(client, host, user)
        except Exception:  # noqa: BLE001
            proceed = True
        phases = getattr(client, "connect_ms", None) or {}
        applog.note(
            f"{host}: connected in "
            f"{sum(phases.values()) + 1000 * (time.perf_counter() - t_ok):.0f} ms"
            f" (auth {phases.get('auth', 0):.0f}"
            f" + schema {phases.get('schema', 0):.0f}"
            f" + info {phases.get('info', 0):.0f}"
            f" + identity {1000 * (time.perf_counter() - t_ok):.0f} ms)"
        )
        if not proceed:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            self.client = None
            self.session = None
            self.status.setText("connect cancelled — device identity mismatch")
            return
        self.status.setText(trf(
            "Connected · {identity} · {version}",
            identity=self.session.identity, version=self.session.ros_version,
        ))
        self.accept()

    def _on_connect_err(self, err: str) -> None:
        self._set_connect_ui_busy(False)
        self.status.setText(trf("connect failed: {err}", err=err))
        # No offline shell to fall back to: fix the target and try again.
        pending = self._pending_connect or {}
        title, hint = explain_connect_error(
            err,
            transport=pending.get("transport", self._transport),
            port=pending.get("tcp_port", DEFAULT_PORT),
            has_mac=self._mode_alternative() is not None and self._transport == "tcp",
        )
        self.error_banner.setText(verbatim(
            f"<b>{html.escape(tr(title))}</b> — {html.escape(tr(hint) if hint else err)}"
        ))
        self.error_banner.setToolTip(err)
        self.error_banner.show()

    # --- device identity (TOFU) ------------------------------------------

    def _verify_and_record_device(self, client, host: str, user: str) -> bool:
        """Compare the device's fingerprint to what we saved for this
        (host, login), prompt on a mismatch, then record it. Returns False only
        when the user cancels on a mismatch."""
        fp = client.read_fingerprint()
        if not (fp.key or fp.macs):
            return True  # nothing identifiable to check or store
        rec = {
            "key": fp.key, "serial": fp.serial, "system_id": fp.system_id,
            "model": fp.model, "identity": fp.identity,
            "macs": fp.macs, "ips": fp.ips, "salt": fp.salt,
            "host": host, "login": user,
        }
        stored = self._store.device_for(host, user)
        if stored is None or not stored.get("verify", True):
            self._store.record_device(rec)
            cs.save(self._store)
            return True
        diffs = self._fingerprint_mismatch(stored, fp)
        if not diffs:
            self._store.record_device(rec)  # refresh MACs/IPs/seen_at
            cs.save(self._store)
            return True
        choice, dont_verify = self._ask_identity_mismatch(host, stored, fp, diffs)
        if dont_verify:
            stored["verify"] = False
        if choice == "update":
            self._store.record_device(rec)
        if dont_verify or choice == "update":
            cs.save(self._store)
        return choice != "cancel"

    @staticmethod
    def _fingerprint_mismatch(stored: dict, fp) -> list[str]:
        diffs: list[str] = []

        def cmp(label: str, old: str, new: str) -> None:
            if old and new and old != new:
                diffs.append(trf("{label}: saved {old}, now {new}",
                                 label=label, old=old, new=new))

        cmp("serial", stored.get("serial", ""), fp.serial)
        cmp("system-id", stored.get("system_id", ""), fp.system_id)
        cmp("model", stored.get("model", ""), fp.model)
        cmp("salt", stored.get("salt", ""), fp.salt)
        return diffs

    def _ask_identity_mismatch(self, host: str, stored: dict, fp, diffs: list[str]):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Different device?")
        box.setText(trf("The device at {host} does not match the one saved here.",
                        host=host))
        box.setInformativeText(trf(
            "Saved:  {saved}\nNow:    {now}\n\n{diffs}\n\n"
            "Usually a re-cabled port, a hardware swap, or this IP now serving a "
            "different box. Continue?",
            saved=f"{stored.get('model', '?')}  ·  {stored.get('key', '?')}",
            now=f"{fp.model or '?'}  ·  {fp.key or '?'}",
            diffs="\n".join(diffs),
        ))
        cont = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
        upd = box.addButton("Update saved", QMessageBox.ButtonRole.ApplyRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        cb = QCheckBox("Don't verify this connection again (e.g. VRRP / shared IP)")
        box.setCheckBox(cb)
        box.exec()
        clicked = box.clickedButton()
        choice = (
            "continue" if clicked is cont
            else "update" if clicked is upd
            else "cancel"
        )
        return choice, cb.isChecked()

    def reject(self) -> None:  # noqa: N802
        # Cancel during a connect attempt cancels *the attempt* and stays on
        # the form. Dismissing the dialog quits the application -- app.py has
        # nothing else to show -- which is not what "stop trying to reach this
        # router" should mean.
        worker = self._connect_worker
        tryall = self._tryall_worker
        if (worker is not None and worker.isRunning()) or (
            tryall is not None and tryall.isRunning()
        ):
            self._cancel_connect()
            return
        # Cancel/Esc reach us through QDialog.done(), which hides the dialog
        # without ever emitting a close event -- so the teardown below was
        # being skipped entirely and Qt aborted on the still-running connect
        # worker as the dialog went away.
        self._shutdown_workers()
        super().reject()

    def _cancel_connect(self) -> None:
        """Abandon the connect in flight and hand the form back."""
        self._shutdown_workers()
        self._set_connect_ui_busy(False)
        self._pending_connect = None
        self.status.setText("Connect cancelled.")
        self._btn_connect.setFocus()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._scan_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        # accept() hides the page behind the session window, and nothing
        # should go on broadcasting to the LAN from a page nobody can see.
        self._scan_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._shutdown_workers()
        super().closeEvent(event)

    def _shutdown_workers(self) -> None:
        """Join both background threads before this dialog can be destroyed."""
        # First, or the timer could start a fresh scan behind the join.
        self._scan_timer.stop()
        self._elapsed_timer.stop()
        # The neighbour scan is hard-bounded by the MNDP timeout, so it always
        # joins on its own; it just must not be destroyed before it does.
        qthreads.retire(self._worker)
        self._worker = None
        worker = self._connect_worker
        if worker is not None:
            # cancel() is what makes this fast: it kills the socket so the
            # in-flight request fails at once. Waiting without it just burned
            # 200ms against a 12s timeout and returned with the thread still
            # running -- which is the fatal case.
            qthreads.retire(worker, cancel=worker.cancel)
        self._connect_worker = None
        tryall = self._tryall_worker
        if tryall is not None:
            # Same reasoning: cancel() closes the live handshake socket so the
            # spray thread returns at once instead of sitting out a timeout.
            qthreads.retire(tryall, cancel=tryall.cancel)
        self._tryall_worker = None
        if self._busy_cursor:
            QApplication.restoreOverrideCursor()
            self._busy_cursor = False
