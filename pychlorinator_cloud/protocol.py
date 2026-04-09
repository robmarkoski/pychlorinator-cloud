"""Halo JSON protocol session handling over DTLS."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable

from .const import KEEPALIVE_INTERVAL_SECONDS, PROTOCOL_VERSION
from .exceptions import ChlorinatorProtocolError
from .models import CommandFrame, DecodedPayload
from .parsers import parse_command_frame, parse_payload

LOGGER = logging.getLogger(__name__)

PayloadHandler = Callable[[dict[str, object], CommandFrame, DecodedPayload], Awaitable[None]]
MessageHandler = Callable[[dict[str, object]], Awaitable[None]]


class HaloProtocolSession:
    """Manage keepalives, JSON parsing, acking, and payload dispatch."""

    def __init__(
        self,
        transport: object,
        *,
        on_message: MessageHandler | None = None,
        on_payload: PayloadHandler | None = None,
    ) -> None:
        self._transport = transport
        self._on_message = on_message
        self._on_payload = on_payload
        self._msg_id = 0
        self._running = False
        self._keepalive_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._buffer = bytearray()

    async def start(self) -> None:
        """Start keepalive and receive loops."""

        if self._running:
            return
        self._running = True
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="halo-keepalive")
        self._reader_task = asyncio.create_task(self._reader_loop(), name="halo-reader")

    async def stop(self) -> None:
        """Stop background tasks."""

        self._running = False
        tasks = [task for task in (self._keepalive_task, self._reader_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._keepalive_task = None
        self._reader_task = None

    async def send_keepalive(self) -> int:
        """Send a keepalive command."""

        return await self._send_json(
            {"cmd": "keepAlive", "msgId": self._next_msg_id(), "version": PROTOCOL_VERSION, "payload": {}}
        )

    async def send_data_command(self, data: bytes) -> int:
        """Wrap a binary command in a Halo JSON `data` message."""

        return await self._send_json(
            {
                "cmd": "data",
                "msgId": self._next_msg_id(),
                "version": PROTOCOL_VERSION,
                "payload": {"data": base64.b64encode(data).decode("ascii")},
            }
        )

    async def _send_json(self, message: dict[str, object]) -> int:
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
        await self._transport.send(encoded)
        LOGGER.debug("Sent protocol message: %s", message)
        return int(message["msgId"])

    async def _keepalive_loop(self) -> None:
        while self._running:
            await self.send_keepalive()
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)

    async def _reader_loop(self) -> None:
        while self._running:
            chunk = await self._transport.recv()
            self._buffer.extend(chunk)
            for message in self._extract_messages():
                await self._handle_message(message)

    def _extract_messages(self) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []

        for line in self._split_complete_lines():
            parsed = self._parse_json(line)
            if parsed is not None:
                messages.append(parsed)

        if messages:
            return messages

        start = None
        depth = 0
        complete_end = None
        for index, byte in enumerate(self._buffer):
            char = chr(byte)
            if char == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    blob = bytes(self._buffer[start : index + 1])
                    parsed = self._parse_json(blob)
                    if parsed is not None:
                        messages.append(parsed)
                        complete_end = index + 1
        if complete_end is not None:
            del self._buffer[:complete_end]
        return messages

    def _split_complete_lines(self) -> list[bytes]:
        if b"\n" not in self._buffer:
            return []
        parts = bytes(self._buffer).splitlines(keepends=True)
        complete: list[bytes] = []
        consumed = 0
        for part in parts:
            if not part.endswith((b"\n", b"\r")):
                break
            complete.append(part.strip())
            consumed += len(part)
        if consumed:
            del self._buffer[:consumed]
        return complete

    def _parse_json(self, raw: bytes) -> dict[str, object] | None:
        text = raw.decode("utf-8", errors="ignore").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            raise ChlorinatorProtocolError(f"Expected JSON object, got {parsed!r}")
        return parsed

    async def _handle_message(self, message: dict[str, object]) -> None:
        LOGGER.debug("Received protocol message: %s", message)
        if self._on_message is not None:
            await self._on_message(message)

        if message.get("cmd") == "data":
            await self._handle_data_message(message)

    async def _handle_data_message(self, message: dict[str, object]) -> None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ChlorinatorProtocolError(f"Malformed data payload: {message}")
        raw_data = payload.get("data")
        if not isinstance(raw_data, str):
            raise ChlorinatorProtocolError(f"Missing base64 data field: {message}")
        frame_bytes = base64.b64decode(raw_data)
        frame = parse_command_frame(frame_bytes)
        decoded = parse_payload(frame)
        await self._ack_data_message(message)
        if self._on_payload is not None:
            await self._on_payload(message, frame, decoded)

    async def _ack_data_message(self, message: dict[str, object]) -> None:
        received_msg_id = int(message["msgId"])
        await self._send_json(
            {
                "cmd": "dataAck",
                "msgId": self._next_msg_id(),
                "version": PROTOCOL_VERSION,
                "payload": {"ids": received_msg_id},
            }
        )

    def _next_msg_id(self) -> int:
        msg_id = self._msg_id
        self._msg_id += 1
        return msg_id
