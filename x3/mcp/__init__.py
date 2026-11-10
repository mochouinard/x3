"""x3 over MCP: the address book and the devices in it, for an AI assistant.

Run it as an MCP stdio server::

    claude mcp add x3 -- /path/to/.venv/bin/python -m x3.mcp

Two layers, so the same tools can later be served from inside a running x3
window instead of from a process of their own:

:mod:`.tools`
    What can be asked -- list devices, read a menu, export the config -- over
    a :class:`~.tools.DeviceSource` that knows where devices and credentials
    come from. :class:`~.tools.SavedDevices` is the headless one: the address
    book plus x3's own overlay, the same list the connection page shows.
:mod:`.protocol`
    MCP itself: newline-delimited JSON-RPC on stdin/stdout. Small enough to
    keep here rather than pull in the SDK and its web stack for one transport.

Read-only, on purpose. Every session this server opens is ``readonly=True``,
so DeviceClient refuses a write before it reaches the wire, whatever a tool
does. What it can do is *propose* a change (:mod:`.change`): that writes a job
file and opens the x3 change window (:mod:`.approve`), a process of its own
with a person in front of it. Approved there, the change runs under Safe Mode
with a revert timer on the router and a checklist, and is kept only if every
check passes.

Secrets never leave: passwords are looked up here and used here, never put in
a tool result; fields the schema marks secret come back as ``<hidden>``; the
export asks for the non-sensitive form and is redacted on top.
"""
