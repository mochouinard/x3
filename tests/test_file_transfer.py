"""File upload and download via mproxy — §10, both directions.

No device and no pytest required:

    .venv/bin/python test_file_transfer.py

Only the read direction existed: fetch_file (cmd 0x7 open-and-read, 0x4 chunks)
is how the plugin index arrives. The write direction was documented from a
capture and never implemented.

Shape, read off a raw session capture (Winbox 4 writing a
37964-byte file and saving two backups):

    cmd=0x1  0x2=<total size>  0x1=<name>       -> 0xFE0001 = write handle
    cmd=0x2  0xFE0001=handle  0x3=<32168 bytes> 0x4=False  -> ack
    cmd=0x2  0xFE0001=handle  0x3=<5796 bytes>  0x4=True   -> ack

    cmd=0x3  0x1=<name>                         -> 0xFE0001 = handle, 0x2 = size
    cmd=0x4  0xFE0001=handle  0x2=32168         -> 0x3=<bytes>  0x4=eof

Every chunk carries the handle. This file used to pin the opposite, read off
the proxy's text log -- which never printed the field -- and a real 7.23.3
answered every chunk with "unknown session id". The transfer ends with the 0x4
bool on the last chunk; there is no close verb.
"""

from x3.proto import message as m
from x3.proto.modern import ModernSession

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


class StubSession(ModernSession):
    """Records requests and answers them the way the device does."""

    def __init__(self, handle=22):
        self.sent: list = []
        self._rid = 0
        self._handle = handle
        self.client_ident = "winbox-4.0"

    def next_request_id(self) -> int:
        self._rid += 1
        return self._rid

    def request(self, msg, *, timeout=None):
        self.sent.append(msg)
        reply = m.Message()
        if msg.get_u32(m.SYS_COMMAND) == 1:
            reply.add_u32(0xFE0001, self._handle)
        reply.add_u32(0xFF0003, 2)
        return reply


DATA = bytes(range(256)) * 200        # 51200 bytes -> two chunks
sess = StubSession()
handle = sess.upload_file("aaa", DATA)

check("the write handle comes back", handle, 22)
check("one open plus two chunks", len(sess.sent), 3)

opened = sess.sent[0]
check("open uses cmd 0x1", opened.get_u32(m.SYS_COMMAND), 1)
check("...carrying the total size", opened.get_u32(0x000002), len(DATA))
check("...and the name", opened.get_string(0x000001), "aaa")
ok("...with a client identity, which v7 firmware requires",
   opened.find(0xFF001C) is not None)

first, last = sess.sent[1], sess.sent[2]
check("chunks use cmd 0x2", (first.get_u32(m.SYS_COMMAND),
                             last.get_u32(m.SYS_COMMAND)), (2, 2))
check("the first chunk is a full 32KB", len(first.get_raw(0x000003)), 0x8000)
check("...and is not the end", first.get_bool(0x000004), False)
check("the last chunk holds the remainder",
      len(last.get_raw(0x000003)), len(DATA) - 0x8000)
check("...and says so", last.get_bool(0x000004), True)
check("every byte was sent, once",
      first.get_raw(0x000003) + last.get_raw(0x000003), DATA)
check("every chunk names the transfer it belongs to",
      (first.get_u32(0xFE0001), last.get_u32(0xFE0001)), (22, 22))

# --- an exact multiple of the chunk size must not send an empty tail ------

sess = StubSession()
sess.upload_file("exact", b"x" * 0x8000)
check("one full chunk is one frame", len(sess.sent), 2)
check("...marked as the end", sess.sent[1].get_bool(0x000004), True)

# --- an empty file still needs a chunk to complete the transfer -----------

sess = StubSession()
sess.upload_file("empty", b"")
check("an empty file still closes the transfer", len(sess.sent), 2)
check("...with an empty final chunk", sess.sent[1].get_raw(0x000003), b"")
check("...flagged as the end", sess.sent[1].get_bool(0x000004), True)
check("...and an open declaring zero bytes", sess.sent[0].get_u32(0x000002), 0)

# --- a refusal is reported, not swallowed --------------------------------


class RefusingSession(StubSession):
    def request(self, msg, *, timeout=None):
        self.sent.append(msg)
        reply = m.Message()
        if msg.get_u32(m.SYS_COMMAND) == 1:
            reply.add_u32(0xFE0001, 22)
            reply.add_u32(0xFF0003, 2)
            return reply
        reply.add_u32(m.SYS_ERROR_CODE, 0xFE0006)
        reply.add_string(m.SYS_ERROR_STRING, "no space left on device")
        return reply


sess = RefusingSession()
failed = None
try:
    sess.upload_file("big", b"y" * 100)
except Exception as e:  # noqa: BLE001
    failed = str(e)
ok("a refused write raises", failed is not None)
ok("...in the device's own words", failed and "no space left" in failed)
ok("...saying how far it got", failed and "100B" in failed)

# --- download: open for read, then read until the device says eof ------


class FileServer(StubSession):
    """Serves one file the way 7.23.3 did: 61440 bytes per read at most."""

    def __init__(self, data: bytes, *, eof_flag=True):
        super().__init__(handle=20)
        self._data, self._pos, self._eof_flag = data, 0, eof_flag

    def request(self, msg, *, timeout=None):
        self.sent.append(msg)
        reply = m.Message()
        cmd = msg.get_u32(m.SYS_COMMAND)
        if cmd == 3:
            reply.add_u32(0xFE0001, self._handle)
            reply.add_u32(0x000002, len(self._data))
        elif cmd == 4:
            n = min(msg.get_u32(0x000002), 61440)
            body = self._data[self._pos:self._pos + n]
            self._pos += len(body)
            if self._eof_flag:
                reply.add_bool(0x000004, self._pos >= len(self._data))
            reply.add_raw(0x000003, body)
        reply.add_u32(0xFF0003, 2)
        return reply


BLOB = bytes(range(256)) * 300        # 76800 bytes -> three reads
srv = FileServer(BLOB)
check("a download returns the file", srv.download_file("a.backup"), BLOB)
check("...opened by name with cmd 0x3",
      (srv.sent[0].get_u32(m.SYS_COMMAND), srv.sent[0].get_string(0x000001)),
      (3, "a.backup"))
reads = srv.sent[1:]
check("...read with cmd 0x4 until the device said eof",
      [r.get_u32(m.SYS_COMMAND) for r in reads], [4, 4, 4])
check("...each read naming the handle the open returned",
      {r.get_u32(0xFE0001) for r in reads}, {20})

srv = FileServer(BLOB, eof_flag=False)
check("a reply without the eof flag still stops at the declared size",
      srv.download_file("a.backup"), BLOB)

srv = FileServer(b"")
check("an empty file downloads as nothing", srv.download_file("e"), b"")

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all file-transfer tests passed")
