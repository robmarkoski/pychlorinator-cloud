"""Async WebSocket signalling client for Halo cloud sessions."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any

import websockets

from .const import SIGNALLING_AUTH_PASSWORD, SIGNALLING_AUTH_USERNAME, SIGNALLING_FAIL_REASON_MAP, SIGNALLING_WS_URL
from .exceptions import (
    SignallingAuthenticationError,
    SignallingBusyError,
    SignallingDosProtectionError,
    SignallingError,
    SignallingRateLimitedError,
    SignallingUnavailableError,
)
from .models import SignallingAnswer, StunEndpoint

LOGGER = logging.getLogger(__name__)


def _map_signalling_failure(reason_code: int) -> SignallingError:
    reason_name = SIGNALLING_FAIL_REASON_MAP.get(reason_code, "unknown_error")
    message = f"Signalling failed with reason {reason_code} ({reason_name})"
    if reason_code == 1:
        return SignallingUnavailableError(message)
    if reason_code == 2:
        return SignallingAuthenticationError(message)
    if reason_code == 3:
        return SignallingBusyError(message)
    if reason_code == 4:
        return SignallingRateLimitedError(message)
    if reason_code == 5:
        return SignallingDosProtectionError(message)
    return SignallingError(message)


class HaloSignallingClient:
    """Halo signalling client backed by the `websockets` package."""

    def __init__(self, url: str = SIGNALLING_WS_URL) -> None:
        self._url = url
        self._auth_headers = self._build_auth_headers()

    @staticmethod
    def _build_auth_headers() -> dict[str, str]:
        creds = base64.b64encode(
            f"{SIGNALLING_AUTH_USERNAME}:{SIGNALLING_AUTH_PASSWORD}".encode("ascii")
        ).decode("ascii")
        return {"Authorization": f"Basic {creds}"}

    async def query(self, serial_number: str) -> bool:
        """Check whether the target chlorinator is reachable via signalling."""

        request = {"type": "query", "name": serial_number}
        LOGGER.debug("Connecting to signalling server %s", self._url)
        async with websockets.connect(self._url, additional_headers=self._auth_headers) as websocket:
            await websocket.send(json.dumps(request))
            response = await self._receive_json(websocket)
        resp_type = response.get("type")
        if resp_type not in ("query", "queryresp"):
            raise SignallingError(f"Unexpected query response: {response}")
        return bool(response.get("success"))

    async def request_session(
        self,
        *,
        serial_number: str,
        username: str,
        password: str,
        public_endpoint: StunEndpoint,
        nat: int = 0,
    ) -> SignallingAnswer:
        """Send an offer and parse the returned session parameters."""

        request = {
            "type": "offer",
            "name": serial_number,
            "payload": {
                "address": public_endpoint.host,
                "port": public_endpoint.port,
                "nat": nat,
                "userName": username,
                "password": password,
            },
        }
        LOGGER.debug("Connecting to signalling server %s", self._url)
        async with websockets.connect(self._url, additional_headers=self._auth_headers) as websocket:
            await websocket.send(json.dumps(request))
            response = await self._receive_json(websocket)

        if response.get("type") != "answer":
            raise SignallingError(f"Unexpected signalling response: {response}")

        payload = response.get("payload") or {}
        if int(response.get("success", 0)) != 1:
            fail_reason = int(payload.get("failReason", 0))
            raise _map_signalling_failure(fail_reason)

        try:
            return SignallingAnswer(
                address=str(payload["address"]),
                port=int(payload["port"]),
                access_level=int(payload.get("accessLevel", 0)),
                session_key=base64.b64decode(str(payload["sessionKey"])),
                raw_message=dict(response),
            )
        except (KeyError, ValueError) as exc:
            raise SignallingError(f"Malformed signalling answer: {response}") from exc

    async def _receive_json(self, websocket: Any) -> Mapping[str, object]:
        try:
            raw = await websocket.recv()
        except websockets.WebSocketException as exc:
            raise SignallingError(f"Signalling websocket error: {exc}") from exc
        if not isinstance(raw, str):
            raise SignallingError("Expected text signalling message")
        LOGGER.debug("Received signalling message: %s", raw)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SignallingError(f"Invalid signalling JSON: {raw}") from exc
        if not isinstance(parsed, dict):
            raise SignallingError(f"Unexpected signalling payload: {parsed!r}")
        return parsed
