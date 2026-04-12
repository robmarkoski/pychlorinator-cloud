"""Library constants derived from the reverse-engineered Halo app."""

from __future__ import annotations

from typing import Final

PROTOCOL_VERSION: Final[float] = 0.1

STUN_SERVER_HOST: Final[str] = "13.211.222.74"
STUN_SERVER_PORT: Final[int] = 3478

SIGNALLING_WS_URL: Final[str] = (
    "wss://iot.connectmypool.com.au/halo_p2p/signalling/version_0.6/app"
)
SIGNALLING_REST_QUERY_URL: Final[str] = (
    "https://iot.connectmypool.com.au/halo_p2p/signalling/version_0.6/app/query"
)

P2P_LOCAL_PORT_CLOUD: Final[int] = 64176
P2P_LOCAL_PORT_LOCAL: Final[int] = 64177

DTLS_PSK_IDENTITY: Final[str] = "Client_identity"
DTLS_FIXED_LOCAL_KEY: Final[bytes] = bytes.fromhex("0123456789ABCDEF0123456789ABCDEF")
DTLS_CIPHER_SUITES: Final[tuple[str, ...]] = (
    "ECDHE-PSK-AES128-CBC-SHA256",
    "ECDHE-PSK-AES256-CBC-SHA384",
    "ECDHE-PSK-AES256-CBC-SHA",
    "PSK-NULL-SHA256",
    "PSK-AES128-CBC-SHA256",
    "PSK-AES256-CBC-SHA384",
)
DTLS_CIPHER_STRING: Final[str] = ":".join(DTLS_CIPHER_SUITES)

KEEPALIVE_INTERVAL_SECONDS: Final[float] = 3.0
DEFAULT_RECEIVE_TIMEOUT_SECONDS: Final[float] = 15.0

HOLE_PUNCH_REQUEST: Final[bytes] = bytes((9, 1))
HOLE_PUNCH_RESPONSE: Final[bytes] = bytes((9, 2))

HALO_MDNS_SERVICE: Final[str] = "_halop2p._udp.local."
HALO_MDNS_UUID_PREFIX: Final[str] = "f3fa2daa-cc33-42da-9e5b-5933"

# WebSocket signalling requires HTTP Basic Auth (hardcoded in HaloChlorGO app)
SIGNALLING_AUTH_USERNAME: Final[str] = "appUserName_sXQlNZa7"
SIGNALLING_AUTH_PASSWORD: Final[str] = "0Q9V@EQwC322S^K6kAyiefdr98a-dcfW"

SIGNALLING_FAIL_REASON_MAP: Final[dict[int, str]] = {
    0: "unknown_error",
    1: "chlorinator_unavailable",
    2: "wrong_credentials",
    3: "chlorinator_busy",
    4: "rate_limited",
    5: "dos_protected",
    6: "stun_failed",
}

# Reverse engineering is incomplete. The few labels below are intentionally
# conservative and should be treated as pragmatic placeholders rather than
# protocol truth.
COMMAND_ID_MAP: Final[dict[int, str]] = {
    0x0009: "frequent_status_placeholder",
    0x1001: "scan_response_placeholder",
}
