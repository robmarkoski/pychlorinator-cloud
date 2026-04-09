"""High-level async clients for Halo cloud and local connectivity."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from .const import (
    DTLS_FIXED_LOCAL_KEY,
    HOLE_PUNCH_REQUEST,
    HOLE_PUNCH_RESPONSE,
    P2P_LOCAL_PORT_CLOUD,
    P2P_LOCAL_PORT_LOCAL,
)
from .dtls_transport import OpenSslDtlsTransport
from .exceptions import DtlsTransportError
from .models import ChlorinatorData, CommandFrame, DecodedPayload, SignallingAnswer, utc_now
from .protocol import HaloProtocolSession
from .signalling import HaloSignallingClient
from .stun import stun_binding_request

LOGGER = logging.getLogger(__name__)


def derive_local_session_key(access_code: str) -> bytes:
    """Derive the local DTLS session key from the 4-character access code."""

    code = access_code.encode("ascii")
    if len(code) != 4:
        raise ValueError("Local access code must be exactly 4 ASCII characters")
    key = bytearray(DTLS_FIXED_LOCAL_KEY)
    for index, value in enumerate(code):
        key[index] ^= value
    return bytes(key)


class _BaseHaloClient:
    """Shared protocol and aggregation logic."""

    def __init__(self) -> None:
        self.data = ChlorinatorData()
        self._transport: OpenSslDtlsTransport | None = None
        self._protocol: HaloProtocolSession | None = None

    async def disconnect(self) -> None:
        """Stop the session and close the underlying transport."""

        self.data.connected = False
        if self._protocol is not None:
            await self._protocol.stop()
            self._protocol = None
        if self._transport is not None:
            await self._transport.close()
            self._transport = None

    async def send_binary_command(self, data: bytes) -> int:
        """Send a raw BLE-style command frame wrapped in Halo JSON."""

        if self._protocol is None:
            raise DtlsTransportError("Client is not connected")
        return await self._protocol.send_data_command(data)

    async def wait_forever(self) -> None:
        """Keep the current task alive while the background reader runs."""

        while self.data.connected:
            await asyncio.sleep(3600)

    async def _attach_protocol(self, transport: OpenSslDtlsTransport) -> None:
        self._transport = transport
        self._protocol = HaloProtocolSession(
            transport,
            on_message=self._on_protocol_message,
            on_payload=self._on_payload,
        )
        await self._protocol.start()
        self.data.connected = True

    async def _on_protocol_message(self, message: dict[str, Any]) -> None:
        self.data.record_protocol_message(message)

    async def _on_payload(
        self,
        message: dict[str, Any],
        frame: CommandFrame,
        payload: DecodedPayload,
    ) -> None:
        del message
        self.data.last_message_at = utc_now()
        self.data.merge_payload(frame, payload)


class HaloCloudClient(_BaseHaloClient):
    """Cloud client using STUN, signalling, hole punching, and DTLS."""

    def __init__(
        self,
        *,
        serial_number: str,
        username: str,
        password: str,
        local_port: int = P2P_LOCAL_PORT_CLOUD,
        local_host: str = "0.0.0.0",
        signalling: HaloSignallingClient | None = None,
        openssl_binary: str = "openssl",
    ) -> None:
        super().__init__()
        self.serial_number = serial_number
        self.username = username
        self.password = password
        self.local_port = local_port
        self.local_host = local_host
        self.signalling = signalling or HaloSignallingClient()
        self.openssl_binary = openssl_binary

    async def connect(self) -> SignallingAnswer:
        """Establish a cloud session end to end."""

        self.data.serial_number = self.serial_number
        self.data.transport = "cloud"
        stun_result = await stun_binding_request(
            local_host=self.local_host,
            local_port=self.local_port,
        )
        LOGGER.info(
            "STUN mapped %s:%s to %s:%s",
            stun_result.local_endpoint.host,
            stun_result.local_endpoint.port,
            stun_result.public_endpoint.host,
            stun_result.public_endpoint.port,
        )
        answer = await self.signalling.request_session(
            serial_number=self.serial_number,
            username=self.username,
            password=self.password,
            public_endpoint=stun_result.public_endpoint,
        )
        self.data.access_level = answer.access_level
        await self._hole_punch(answer.address, answer.port)
        transport = OpenSslDtlsTransport(
            host=answer.address,
            port=answer.port,
            psk=answer.session_key,
            local_host=self.local_host,
            local_port=self.local_port,
            openssl_binary=self.openssl_binary,
        )
        await transport.connect()
        await self._attach_protocol(transport)
        return answer

    async def _hole_punch(self, host: str, port: int) -> None:
        """Perform the same simple UDP hole punch observed in the app."""

        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.settimeout(0.1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.local_host, self.local_port))
            sock.connect((host, port))
            for _ in range(150):
                await loop.sock_sendall(sock, HOLE_PUNCH_REQUEST)
                try:
                    data = await asyncio.wait_for(loop.sock_recv(sock, 32), timeout=0.1)
                except TimeoutError:
                    continue
                if data == HOLE_PUNCH_RESPONSE:
                    LOGGER.debug("UDP hole punch acknowledged by %s:%s", host, port)
                    return
        except OSError as exc:
            raise DtlsTransportError(f"UDP hole punch failed: {exc}") from exc
        finally:
            sock.close()
        raise DtlsTransportError("UDP hole punch did not receive the expected acknowledgement")


class HaloLocalClient(_BaseHaloClient):
    """Local/LAN client using the inferred XOR-derived PSK."""

    def __init__(
        self,
        *,
        host: str,
        access_code: str,
        port: int = P2P_LOCAL_PORT_LOCAL,
        local_host: str = "0.0.0.0",
        local_port: int = P2P_LOCAL_PORT_LOCAL,
        openssl_binary: str = "openssl",
    ) -> None:
        super().__init__()
        self.host = host
        self.access_code = access_code
        self.port = port
        self.local_host = local_host
        self.local_port = local_port
        self.openssl_binary = openssl_binary

    async def connect(self) -> bytes:
        """Establish a local DTLS session."""

        self.data.transport = "local"
        session_key = derive_local_session_key(self.access_code)
        transport = OpenSslDtlsTransport(
            host=self.host,
            port=self.port,
            psk=session_key,
            local_host=self.local_host,
            local_port=self.local_port,
            openssl_binary=self.openssl_binary,
        )
        await transport.connect()
        await self._attach_protocol(transport)
        return session_key
