"""Metadata for the connected device session."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionInfo:
    """Session metadata shown in the title bar / toolbar.

    Credentials come from Addresses.cdb or the connect form; the device fields
    are filled from the router's own info reply once the login succeeds.
    """

    host: str = ""
    user: str = ""
    password: str = ""
    identity: str = "router"
    ros_version: str = "?"
    board: str = "?"
    platform: str = "?"
    cpu_load: str = "?"
    free_memory: str = "?"
    total_memory: str = "?"
    uptime: str = "?"
    # Connection path: "tcp" (IP:8291) or "mac" (MAC-Winbox UDP 20561)
    transport: str = "tcp"
    mac: str = ""  # set when transport is mac (or when both known)

    def title(self) -> str:
        return (
            f"{self.user}@{self.host} ( {self.identity} ) - "
            f"x3 [{self.ros_version}] on {self.board} ({self.platform})"
        )

    def connect_label(self) -> str:
        if self.transport == "mac":
            return f"mac:{self.mac or self.host}"
        return self.host
