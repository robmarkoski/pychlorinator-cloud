"""Dataclasses used by the Halo connectivity library."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


@dataclass(slots=True)
class StunEndpoint:
    """A resolved UDP endpoint."""

    host: str
    port: int


@dataclass(slots=True)
class StunBindingResult:
    """Result of a STUN binding request."""

    local_endpoint: StunEndpoint
    public_endpoint: StunEndpoint
    source_endpoint: StunEndpoint | None = None
    changed_endpoint: StunEndpoint | None = None
    transaction_id: bytes = b""


@dataclass(slots=True)
class SignallingAnswer:
    """Successful signalling answer payload."""

    address: str
    port: int
    access_level: int
    session_key: bytes
    raw_message: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommandFrame:
    """Decoded binary frame transported inside JSON `data` messages."""

    prefix: int
    command_id: int
    body: bytes
    raw: bytes


@dataclass(slots=True)
class DecodedPayload:
    """Base parsed payload structure."""

    command_id: int
    command_name: str
    raw_body: bytes
    notes: str | None = None


@dataclass(slots=True)
class UnknownPayload(DecodedPayload):
    """Fallback payload when a command mapping is not known."""


@dataclass(slots=True)
class ScanResponsePayload(DecodedPayload):
    """Parser for the reverse-engineered `ScanResponseStruct` layout."""

    manufacturer_id: int = 0
    device_type: int = 0
    device_version: int = 0
    bootloader_major_version: int = 0
    bootloader_minor_version: int = 0
    application_major_version: int = 0
    application_minor_version: int = 0
    hardware_platform_id: int | None = None


@dataclass(slots=True)
class ChlorinatorData:
    """Aggregated application-facing state built from incoming payloads."""

    connected: bool = False
    transport: str = ""
    serial_number: str | None = None
    access_level: int | None = None
    last_keepalive_at: datetime | None = None
    last_message_at: datetime | None = None
    last_protocol_message: dict[str, Any] | None = None
    last_frame: CommandFrame | None = None
    last_decoded_payload: DecodedPayload | None = None
    scan_response: ScanResponsePayload | None = None
    latest_payloads: dict[int, DecodedPayload] = field(default_factory=dict)
    raw_payload_count: int = 0

    def record_protocol_message(self, message: dict[str, Any]) -> None:
        """Update state from a decoded JSON protocol message."""

        self.last_protocol_message = message
        self.last_message_at = utc_now()
        if message.get("cmd") == "keepAlive":
            self.last_keepalive_at = self.last_message_at

    def merge_payload(self, frame: CommandFrame, payload: DecodedPayload) -> None:
        """Merge parsed binary payload into the aggregate state."""

        self.last_frame = frame
        self.last_decoded_payload = payload
        self.latest_payloads[payload.command_id] = payload
        self.raw_payload_count += 1
        if isinstance(payload, ScanResponsePayload):
            self.scan_response = payload
