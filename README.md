# x3

An independent desktop client and command-line toolkit for routers managed
over the **Winbox protocol** (TCP 8291, and MAC-layer on UDP 20561).

x3 reads each router's own `.jg` schema files, so the menu tree, the list
columns and the property forms come from the device rather than from
hand-written screens. Each session can start read-only, queue changes for
review, or write straight through; you pick which.

x3 is a personal research project and a homage to Winbox 3. It is free for
personal and noncommercial use (see [License](#license)). Python 3.12+ and
PySide6.

## Why x3 exists

I liked the Winbox 3 experience. I didn't like Winbox 4: there was a lot I had
hoped to see improved in it that never was, while Winbox 3 itself stopped
getting any love. So I started building my own client, for my own use.

The first itch was backups. I wanted to automate my router backups over the
Winbox protocol, the one management port I could count on everywhere. Once the
client could drive the router's console, it could take an export of every
router in my address book, every night. Then I kept adding things: the menus
and forms, a keyboard-first workflow, and the safety nets I had always wanted.

With firmware 7.25, Winbox 3 can no longer be used, so x3 is now the client I
use full-time. That is also why I am publishing it: maybe it helps someone else
who misses the way Winbox 3 worked, and they can help tweak x3, through pull
requests, so it works the way they want. x3 isn't a Winbox 3 clone, though: I
also put in a lot from Winbox 4, to please the v4 crowd.

x3 is not meant to compete with the official clients, and it is not a
product. It is a research project and a homage. The best possible outcome for
it is that it stops being useful: if any of it (a keyboard flow, a snapshot
feature, a safety net) nudges the official tools toward doing these things
properly, x3 can quietly retire, and I'll consider that a win.

### The name

x3 is a homage to Winbox v3. It also stands for the three edges of a cube that
meet at a corner: a box, which is where the icon comes from.

## Please read this before you use it

**I do not recommend that you use this client.**

I built it to be as compatible as *I* needed it to be. That is a much lower bar
than "correct". There is almost certainly functionality that does not work for
your devices, your firmware version or your configuration, and there are
failure modes I have never encountered and therefore never handled. Writing a
configuration through an incomplete client can produce results ranging from
"nothing happened" to a device that is misconfigured, unreachable or otherwise
broken.

Use your router vendor's own management tools for as long as they work for you.
Those are supported products. This is not.

If you use this anyway, it is entirely at your own risk. Take a backup first,
try it on a device you can afford to lose, and read the export before you trust
it.

## What it is

- **A desktop client** built from the router's own schema: menus, lists,
  forms, tabs, dropdowns and status lines as the device describes them.
- **Keyboard-first.** A change can be made end to end without the mouse: type to
  filter, `Enter` to open a row, `Tab` through the form, `Enter` to commit.
- **Three edit modes.** *Read-only*, *Queue* (changes are collected locally
  and sent together, in order, once you review them) and *Live*
  (write-through).
- **Safe Mode** with an idle alarm and an input guard, because the moment you
  most need Safe Mode is the moment you forgot you were in it.
- **Automatic config snapshots**: a text export a quiet interval after the last
  change, so you can see what changed and when. Nothing is written to the
  router's flash.
- **A command line, `x3ctl`**: login checks, schema browsing, GET/GETALL
  probes, file transfer, an interactive console, config export, and a
  whole-fleet backup into a git repository.
- **An MCP server** that lets an AI assistant read your saved devices: lists,
  forms, logs, ping and traceroute. It cannot change a router.
- **Audit mode**: every frame a session sends and receives, written verbatim to
  a file you can read back, list and diff.
- **MNDP neighbour discovery** and MAC-layer connections for devices with no IP.

Modern sessions (firmware 6.43 and later: EC-SRP5 login, AES-CBC +
HMAC-SHA1 transport) are the supported path. The pre-6.43 MD5
challenge/response login is implemented for old gear.

## Install

Each release on the [releases page](https://github.com/mochouinard/x3/releases)
has ready-to-run builds:

- **Windows:** `x3-<version>-win64-setup.exe`. It installs for your account
  only (no administrator rights needed), adds x3 to the Start menu, and can be
  removed from *Settings ▸ Apps*. `x3ctl.exe` is installed alongside `x3.exe`
  in `%LOCALAPPDATA%\Programs\x3`. The installer is not code-signed yet, so
  SmartScreen may ask you to confirm the first time, and Windows Firewall
  asks once whether x3 may use the network (it listens for router discovery
  on UDP 5678; allowing private networks is enough).
- **Linux:** `x3-<version>-x86_64.AppImage`. Make it executable and run it:
  `chmod +x x3-*.AppImage && ./x3-*.AppImage`. Run it with `x3ctl` as the
  first argument for the command line (`./x3-*.AppImage x3ctl --help`).

From source, on any platform with Python 3.12 or newer:

```sh
git clone https://github.com/mochouinard/x3 && cd x3
python3 -m venv .venv
.venv/bin/pip install -e ".[gui]"     # desktop client + x3ctl
# or: .venv/bin/pip install -e .      # x3ctl and the protocol library only
.venv/bin/x3                          # the desktop client
.venv/bin/x3ctl --help                # the command line
.venv/bin/x3ctl mcp                   # the MCP server, on stdio
```

`assets/install-desktop.sh` adds a desktop entry and icons for a source
install on Linux (current user, no root needed).

### Updates

The first page of the welcome tour asks whether x3 may look for a newer
release (recommended). If you agree, it asks GitHub at most once a day, also
while x3 stays open, and only shows a button when something new is out.
*Settings ▸ Check for x3 Updates Daily* changes your answer, and
*Help ▸ Check for x3 Updates* asks at any time. Nothing is installed until you
say so. The
Windows build and the AppImage update themselves: x3 downloads the new
version, checks it against the release's `SHA256SUMS`, and restarts into it.
A source or pip install is told the command to run instead.

### Where x3 keeps its files

| | Linux | Windows |
|---|---|---|
| settings, address-book overlay | `~/.config/x3` | `%APPDATA%\x3` |
| session logs, audit files, snapshots | `~/.local/share/x3` | `%LOCALAPPDATA%\x3\data` |
| caches (parsed schemas, plugin sets) | `~/.cache/x3` | `%LOCALAPPDATA%\x3\cache` |
| application log | `~/.local/state/x3/x3.log` | `%LOCALAPPDATA%\x3\state\x3.log` |

The `XDG_*_HOME` variables move them on any platform. On Linux the files that
hold passwords (settings, the address-book overlay, snapshots, audit files)
are created readable by you alone; on Windows they rely on the permissions of
your user profile folder, which other users cannot read by default.

## The desktop client

On first start a **welcome tour** walks through what x3 adds and lets you
switch each part: the look (theme, zoom, icons), keyboard use and the Ctrl
key panel, the mode new sessions start in, the safety nets and Safe Mode's idle
alarm, config snapshots (and whether they keep secrets), the audit record, and
the small differences in lists and forms. Nothing changes until you press
Finish, and the tour can be reopened from **Help ▸ Welcome Tour**.

The first screen after that is the connection page: an address, a login, the devices
MNDP hears on your LAN, and your saved address book. x3 reads an existing
`Addresses.cdb` if it finds one and keeps its own edits in a separate overlay,
so the file itself is never rewritten.

### Modes

The toolbar's **Mode** box decides what an Apply does, for the whole session:

| Mode | What happens |
|---|---|
| **Read-only** | Forms are locked. Nothing can change. |
| **Queue** | Apply, Remove, Enable/Disable and Comment are collected locally. **Pending changes** shows each one as `old → new`; **Apply all** sends them oldest first and stops at the first one the router refuses. |
| **Live** | Write-through: each change reaches the router immediately. Safe Mode is offered when you switch. |

New sessions start in Live. Untick **Settings ▸ Start In Live Mode** to start them read-only.

**Safe Mode** uses the router's own safe-mode handler: changes made while it is
on are undone by the router if the session drops. While it is armed the whole
window says so: a striped toolbar button, a banner and a hazard-striped
backdrop. An armed session left idle starts asking to be dealt with.

### Keyboard

**In a list window**

| key | what it does |
|---|---|
| type anything | narrows the list (the filter box is where a window opens) |
| `Down` / `Enter` (in the filter) | hands the keyboard to the rows |
| `Esc` (in the filter) | empties it; on an empty box, closes the window |
| `↑` `↓` | walk the rows (they survive a poll re-reading the list) |
| `→` / `←` | open / close an interface tree's parent |
| `Enter` | open the current row's properties |
| `Ins` | Add |
| `Del` | Remove |
| `Ctrl+E` / `Ctrl+Shift+E` | Enable / Disable |
| `Ctrl+M` | Comment |
| `Alt+↑` / `Alt+↓` | Move Up / Move Down (ordered lists: firewall, NAT, …) |
| `F5` | Refresh |
| `Ctrl` (hold) / `F1` / `?` | show the keys this window answers to |

**In a property form**

| key | what it does |
|---|---|
| `Tab` / `Shift+Tab` | next / previous field, then the button stack |
| `Ctrl+PageDown` / `Ctrl+PageUp` | next / previous tab |
| `Space` | toggle a checkbox; flip an optional field; open a group fold |
| `Enter` | OK: commit the form and close it |
| `Ctrl+Enter` | Apply: commit and stay open |
| `Esc` | close (asks first when there are unsaved edits) |
| `Ctrl`+wheel | change the dropdown or spin box under the pointer |

A plain mouse wheel never changes a value: it scrolls the form. `Ctrl`+wheel
is the deliberate gesture.

### Looks

**View ▸ Theme** offers five skins over the same windows:

| Theme | |
|---|---|
| **Light** (default) | Dense and flat, black type, hairline window edges. |
| **Dark** | Light's layout in dark slate. |
| **Roomy** | Flat and light with more padding: comfort over density. |
| **Classic** | Win32 gray with bevels, Tahoma and dense tables. |
| **High contrast** | Strong borders and larger type. |

`Ctrl+=` / `Ctrl+-` zoom the whole interface (type, rows, icons, padding) and
`Ctrl+0` goes back to 100%. **View ▸ Font** picks the typeface, and
**View ▸ Icon Set** switches between the colour icons and a monochrome set
inked in the theme's text colour. `x3 --theme dark` picks a skin from the
command line. The client works with screen readers through Qt's accessibility
bridge.

### Languages

x3 speaks English and French (Quebec). By default it follows the desktop's
language when x3 has it, and English otherwise. The **Language** list at the
bottom of the connection page switches at once; **View ▸ Language** in a
session applies when you return to the connection page. `x3 --lang fr` tries
one for a single run.

A language is a table from the English text to its own, and a string is
translated only when the table holds it exactly. That covers x3's own menus,
dialogs and tour, and the router's labels too: menus, window titles, tabs,
columns and field names, taken from the plugin files of a few hundred firmware
versions. Anything the table does not hold stays in English. What the router
stores is never translated: values in fields, drop-downs and lists read
exactly as the router has them, so a translated screen never writes another
language to a router. To add a language, see `CONTRIBUTING.md`.

## The command line

```
x3ctl login           authenticate and exit
x3ctl try-creds       try every password in an address book against one device
x3ctl version         board, firmware, memory, CPU
x3ctl inspect         GET on any handler; prints every field
x3ctl getall          GETALL on a menu handler; prints every object
x3ctl schema          browse the menu/field map from a local plugin set (offline)
x3ctl probe           send an arbitrary request and dump the reply
x3ctl subscribe       like probe, but keeps reading replies
x3ctl download        fetch a file from the device
x3ctl fetch-plugins   download a list of files
x3ctl mirror          mirror the device's .jg plugin set into '<version>-<build>/'
x3ctl terminal        an interactive router console over the Winbox protocol
x3ctl export          capture a text /export (nothing is written to flash)
x3ctl backup          back up every router in an address book into a git repo
x3ctl neighbors       MNDP discovery on the LAN
x3ctl audit           read back a .wba audit file (offline)
x3ctl mcp             serve the address book to an AI assistant (see below)
```

Every device command takes `--host`, `--port`, `--user` and `--password` (or
`$X3_PASSWORD`). `--note` resolves all four from a saved address-book entry.

```sh
export X3_PASSWORD=...
x3ctl version --host 192.0.2.1 --user admin
x3ctl export  --host 192.0.2.1 --user admin > router.rsc
x3ctl backup  --cdb ~/Addresses.cdb --archive ~/router-config-archive
```

`export` and `backup` are pure reads: the configuration comes off the console
into memory, with `show-sensitive` so the result can rebuild the device. That
also means the archive holds secrets in plain text; keep it private.
`contrib/systemd/` has a user timer that runs the backup nightly.

## The MCP server

```sh
claude mcp add x3 -- x3ctl mcp      # use the full path to x3ctl if it is not on PATH
```

It serves your saved address book: `list_devices`, `list_neighbors`,
`device_info`, `list_menus`, `read_menu`, `export_config`, `ping`,
`traceroute`, `read_log` and `interface_traffic`. Every tool is read-only, no
model-supplied text is ever typed at a router's console, and passwords never
appear in results.

## Development

```
x3/proto/   the protocol: framing, EC-SRP5 login, AES transport, the M2
            message codec, the .jg schema, MAC-layer transport, MNDP
x3/ui/      the desktop client (PySide6)
x3/mcp/     the MCP server
x3/cli.py   x3ctl
tests/      one standalone script per behaviour
```

```sh
python tests/run.py           # everything
python tests/run.py fonts     # only files whose name contains "fonts"
```

To build the release packages locally: `pyinstaller --noconfirm
packaging/pyinstaller/x3.spec`, then `packaging/linux/build-appimage.sh`
(Linux) or `iscc /DAppVersion=<version> packaging\windows\x3.iss` (Windows,
Inno Setup 6). A `v<version>` tag builds both and publishes the release
(`.github/workflows/release.yml`).

Many tests read a real `.jg` plugin set. Those files belong to the router
vendor and are not in this repository: mirror one from a device into the
repository root (`x3ctl mirror --host … --out-root .`). The tests were written
against `7.21.1-151146055/`; a test whose plugin set is missing is reported as
skipped.

## Credits

The wire format and the login are public knowledge thanks to other people's
research. x3 is a clean implementation built on it:

- **Margin Research**: the EC-SRP5 handshake, key derivation and encrypted
  framing, documented in
  [mikrotik_authentication](https://github.com/MarginResearch/mikrotik_authentication)
  and [EC-SRP](https://github.com/MarginResearch/EC-SRP). `x3/proto/crypto.py`
  derives from their reference code (Apache-2.0; see below).
- **Tenable**: [routeros](https://github.com/tenable/routeros) documented the
  TCP segment chunking, the M2 type tags, the system field codes and the
  legacy MD5 login.
- **Cisco Talos**: the
  [Winbox protocol dissector](https://github.com/Cisco-Talos/Winbox_Protocol_Dissector)
  is the source for command, error and standard field codes.
- **Kirils Solovjovs**: [mikrotik-tools](https://github.com/0ki/mikrotik-tools)
  confirmed the M2 block encoding on disk.
- **Håkon Nessjøen**: [MAC-Telnet](https://github.com/haakonnessjoen/MAC-Telnet)
  and Wireshark's MAC-Telnet dissector described the MAC-layer session framing.

x3 is a management tool. It contains no exploit code and was not built from
any.

## License

x3 is free for **personal and noncommercial use** under the **PolyForm Strict
License 1.0.0**, with one added permission: you may change x3 to prepare, test
and send those changes back as a pull request to this repository. Beyond that,
the license means what it says: no commercial use, no redistribution, and no
changed copies kept or shared outside a contribution. The full text is in
`LICENSE`, and the contribution terms are in `CONTRIBUTING.md`. If you would
like to use x3 for something the license doesn't cover, at work for example,
ask.

*La licence est aussi offerte en français : `LICENSE.fr.md`.*

x3 comes **as is, with no warranty of any kind**, and its author is not liable
for what it does to your routers or anything else, as far as the law allows
(see "No Liability" in `LICENSE`).

`x3/proto/crypto.py` contains code derived from Margin Research's reference
client, and those portions remain under the **Apache License 2.0**
(`LICENSES/Apache-2.0.txt`).

x3 runs on PySide6/Qt and pyte (LGPL-3.0), PyCryptodome (BSD-2-Clause and
public domain), python-ecdsa, six and wcwidth (MIT), and on Windows and macOS
psutil (BSD-3-Clause). None of them is bundled, and x3's license takes nothing
away from theirs: the LGPL libraries in particular stay yours to replace or
modify. `THIRD-PARTY-NOTICES.md` has the details, and the same list is in the
app under **Help ▸ About**.

x3 is an independent project. MikroTik, RouterOS and Winbox are names used by
Mikrotīkls SIA for its company, its products and its management protocol, and
may be its trademarks. x3 uses "Winbox" only to say which protocol it speaks
and which clients it is compared with, and is not affiliated with, endorsed or
sponsored by Mikrotīkls SIA. Other product names belong to their respective
owners.
