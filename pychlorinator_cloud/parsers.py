"""Binary payload parsing helpers for Halo data frames."""

from __future__ import annotations

import logging
import struct

from .const import COMMAND_ID_MAP
from .exceptions import ChlorinatorProtocolError
from .models import CommandFrame, DecodedPayload, ScanResponsePayload, UnknownPayload

LOGGER = logging.getLogger(__name__)

_SCAN_RESPONSE_STRUCT = struct.Struct("<BBBBBBBBH")


def parse_command_frame(data: bytes) -> CommandFrame:
    """Parse the common Halo binary frame wrapper.

    The wrapper is inferred from the app:
    - byte 0: unknown prefix
    - bytes 1-2: little-endian command id
    - bytes 3+: command-specific payload
    """

    if len(data) < 3:
        raise ChlorinatorProtocolError("Binary frame is too short")
    prefix = data[0]
    command_id = int.from_bytes(data[1:3], byteorder="little", signed=False)
    return CommandFrame(prefix=prefix, command_id=command_id, body=data[3:], raw=data)


def parse_scan_response_payload(body: bytes, command_id: int = 0x1001) -> ScanResponsePayload:
    """Parse the reverse-engineered `ScanResponseStruct`.

    This layout comes directly from the extracted C# sources. It is primarily a
    discovery advertisement structure, so it may not appear on every P2P
    session. The parser is still included because it is one of the few
    completely specified binary layouts available in this repository.
    """

    if len(body) < _SCAN_RESPONSE_STRUCT.size:
        raise ChlorinatorProtocolError(
            f"Scan response payload too short: {len(body)} bytes"
        )
    (
        manufacturer_id_lo,
        manufacturer_id_hi,
        device_type,
        device_version,
        boot_major,
        boot_minor,
        app_major,
        app_minor,
        hardware_platform_id,
    ) = _SCAN_RESPONSE_STRUCT.unpack_from(body[: _SCAN_RESPONSE_STRUCT.size])
    hardware_platform = hardware_platform_id or None
    return ScanResponsePayload(
        command_id=command_id,
        command_name=COMMAND_ID_MAP.get(command_id, "scan_response_placeholder"),
        raw_body=body,
        notes=(
            "Parsed from the documented ScanResponseStruct layout. "
            "Use cautiously when observed on P2P data because command mapping "
            "is still incomplete."
        ),
        manufacturer_id=(manufacturer_id_hi << 8) | manufacturer_id_lo,
        device_type=device_type,
        device_version=device_version,
        bootloader_major_version=boot_major,
        bootloader_minor_version=boot_minor,
        application_major_version=app_major,
        application_minor_version=app_minor,
        hardware_platform_id=hardware_platform,
    )


def parse_payload(frame: CommandFrame) -> DecodedPayload:
    """Parse a command frame into a pragmatic higher-level payload.

    Reverse-engineering is incomplete. The strategy here is conservative:
    recognize only layouts that are explicitly documented, and return a typed
    unknown payload everywhere else.
    """

    command_name = COMMAND_ID_MAP.get(frame.command_id, f"unknown_0x{frame.command_id:04x}")

    if len(frame.body) >= _SCAN_RESPONSE_STRUCT.size:
        try:
            parsed = parse_scan_response_payload(frame.body, command_id=frame.command_id)
            if parsed.manufacturer_id == 1095:
                LOGGER.debug(
                    "Interpreted command 0x%04x as scan-response-shaped payload",
                    frame.command_id,
                )
                return parsed
        except ChlorinatorProtocolError:
            pass

    return UnknownPayload(
        command_id=frame.command_id,
        command_name=command_name,
        raw_body=frame.body,
        notes=(
            "No authoritative command mapping is available for this payload yet. "
            "The raw body is preserved for downstream analysis."
        ),
    )
