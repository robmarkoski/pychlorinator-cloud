"""Minimal async STUN binding client with manual packet parsing."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import struct

from .const import (
    DEFAULT_RECEIVE_TIMEOUT_SECONDS,
    STUN_SERVER_HOST,
    STUN_SERVER_PORT,
)
from .exceptions import StunError
from .models import StunBindingResult, StunEndpoint

LOGGER = logging.getLogger(__name__)

_STUN_BINDING_REQUEST = 0x0001
_STUN_BINDING_RESPONSE = 0x0101
_STUN_MAGIC_COOKIE = 0x2112A442
_ATTR_MAPPED_ADDRESS = 0x0001
_ATTR_SOURCE_ADDRESS = 0x0004
_ATTR_CHANGED_ADDRESS = 0x0005
_ATTR_XOR_MAPPED_ADDRESS = 0x0020


def build_binding_request(transaction_id: bytes | None = None) -> tuple[bytes, bytes]:
    """Build a STUN binding request packet."""

    tx_id = transaction_id or os.urandom(12)
    packet = struct.pack("!HHI12s", _STUN_BINDING_REQUEST, 0, _STUN_MAGIC_COOKIE, tx_id)
    return packet, tx_id


def _parse_address_attribute(attr_type: int, value: bytes) -> StunEndpoint:
    if len(value) < 8:
        raise StunError("STUN address attribute too short")
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0]
    addr_bytes = value[4:]
    if family == 0x01:
        if len(addr_bytes) < 4:
            raise StunError("IPv4 STUN attribute too short")
        raw_ip = addr_bytes[:4]
        if attr_type == _ATTR_XOR_MAPPED_ADDRESS:
            cookie_bytes = struct.pack("!I", _STUN_MAGIC_COOKIE)
            raw_ip = bytes(a ^ b for a, b in zip(raw_ip, cookie_bytes))
            port ^= _STUN_MAGIC_COOKIE >> 16
        host = str(ipaddress.IPv4Address(raw_ip))
    elif family == 0x02:
        if len(addr_bytes) < 16:
            raise StunError("IPv6 STUN attribute too short")
        raw_ip = addr_bytes[:16]
        host = str(ipaddress.IPv6Address(raw_ip))
    else:
        raise StunError(f"Unsupported STUN address family: {family}")
    return StunEndpoint(host=host, port=port)


def parse_binding_response(data: bytes, expected_transaction_id: bytes) -> dict[int, StunEndpoint]:
    """Parse a STUN binding response and return extracted endpoints."""

    if len(data) < 20:
        raise StunError("STUN response too short")
    msg_type, msg_len, cookie, tx_id = struct.unpack("!HHI12s", data[:20])
    if msg_type != _STUN_BINDING_RESPONSE:
        raise StunError(f"Unexpected STUN message type: 0x{msg_type:04x}")
    if cookie != _STUN_MAGIC_COOKIE:
        raise StunError("Unexpected STUN magic cookie")
    if tx_id != expected_transaction_id:
        raise StunError("STUN transaction id mismatch")
    if len(data) < 20 + msg_len:
        raise StunError("Truncated STUN response")

    offset = 20
    attributes: dict[int, StunEndpoint] = {}
    while offset + 4 <= 20 + msg_len:
        attr_type, attr_len = struct.unpack("!HH", data[offset : offset + 4])
        offset += 4
        value = data[offset : offset + attr_len]
        offset += attr_len
        offset += (-attr_len) % 4
        if attr_type in {
            _ATTR_MAPPED_ADDRESS,
            _ATTR_SOURCE_ADDRESS,
            _ATTR_CHANGED_ADDRESS,
            _ATTR_XOR_MAPPED_ADDRESS,
        }:
            attributes[attr_type] = _parse_address_attribute(attr_type, value)
    return attributes


async def stun_binding_request(
    host: str = STUN_SERVER_HOST,
    port: int = STUN_SERVER_PORT,
    *,
    local_host: str = "0.0.0.0",
    local_port: int = 0,
    timeout: float = DEFAULT_RECEIVE_TIMEOUT_SECONDS,
) -> StunBindingResult:
    """Perform a STUN binding request over UDP using asyncio sockets."""

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.bind((local_host, local_port))
        request, transaction_id = build_binding_request()
        local_endpoint = StunEndpoint(*sock.getsockname()[:2])
        LOGGER.debug(
            "Sending STUN binding request from %s:%s to %s:%s",
            local_endpoint.host,
            local_endpoint.port,
            host,
            port,
        )
        await loop.sock_sendto(sock, request, (host, port))
        data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), timeout=timeout)
        parsed = parse_binding_response(data, transaction_id)
        public_endpoint = parsed.get(_ATTR_XOR_MAPPED_ADDRESS) or parsed.get(_ATTR_MAPPED_ADDRESS)
        if public_endpoint is None:
            raise StunError("STUN response did not include a mapped address")
        return StunBindingResult(
            local_endpoint=local_endpoint,
            public_endpoint=public_endpoint,
            source_endpoint=parsed.get(_ATTR_SOURCE_ADDRESS),
            changed_endpoint=parsed.get(_ATTR_CHANGED_ADDRESS),
            transaction_id=transaction_id,
        )
    except TimeoutError as exc:
        raise StunError("Timed out waiting for STUN response") from exc
    except OSError as exc:
        raise StunError(f"STUN socket error: {exc}") from exc
    finally:
        sock.close()
