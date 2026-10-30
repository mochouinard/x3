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

Read-only, on purpose. Every session is opened with ``readonly=True``, so
DeviceClient refuses a write before it reaches the wire, whatever a tool does.
Changes are for the in-window server, where each one waits in x3's queue for a
person to approve it.

Secrets never leave: passwords are looked up here and used here, never put in
a tool result; fields the schema marks secret come back as ``<hidden>``; the
export asks for the non-sensitive form and is redacted on top.
"""
