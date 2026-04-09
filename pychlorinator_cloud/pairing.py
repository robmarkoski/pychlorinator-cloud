"""BLE pairing helper for AstralPool Halo chlorinators.

This module implements the password generation flow that creates the
cloud credentials needed for WebSocket connections.

The flow requires BLE access to the chlorinator. Once complete, the
generated password is static and works for all future cloud sessions.

BLE Pairing Flow
================

1. Connect to chlorinator via BLE (advertises as "HCHLOR")
2. Read session key from UUID 45000001-98b7-4e29-a03f-160174643002
3. Write encrypted MAC key to UUID 45000002-98b7-4e29-a03f-160174643002
4. Send username via command 719 (0x02CF):
   - Payload: [0x00, username_length, username_utf8_bytes...]
   - Write encrypted to UUID 45000004-98b7-4e29-a03f-160174643002
5. Receive password fragments via command 720 (0x02D0):
   - Notified on UUID 45000003-98b7-4e29-a03f-160174643002
   - Each fragment: [last_flag, frag_index, total_length, ascii_bytes(up to 13)]
   - last_flag=0 means this IS the last fragment
   - Concatenate all fragments to get the full password (typically 64 chars)

Response codes from command 719:
   0 = Success (proceed to wait for password)
   1 = User list is full
   2 = Username exists (still proceeds to password)
   3 = Invalid name length
   4 = Password fragment received (unexpected here)
   5 = Password fragment out of range
   6 = User ID out of range
   7 = Waiting for password fragment 0 to start

Cloud Pairing (Theoretical)
===========================

It MAY be possible to send the pairing commands over the cloud WebSocket
using the same wire format as other commands:

   prefix(0x01) + cmd_id(0x02CF LE) + [0x00, len, username...]

And receive password fragments as dataexchange with cmd_id=0x02D0.

This has NOT been tested. Pairing may require BLE-only characteristics
that are not relayed through the cloud WebSocket.

Usage (BLE — requires bleak)
=============================

    from pychlorinator_cloud.pairing import pair_via_ble

    # Requires the chlorinator to be in pairing mode (access code visible)
    password = await pair_via_ble(
        ble_address="AA:BB:CC:DD:EE:FF",
        access_code="1234",
        username="<username>",
    )
    print(f"Cloud password: {password}")
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

# BLE UUIDs
UUID_SERVICE = "45000001-98b7-4e29-a03f-160174643002"
UUID_SESSION_KEY = "45000001-98b7-4e29-a03f-160174643002"
UUID_AUTH = "45000002-98b7-4e29-a03f-160174643002"
UUID_TX = "45000003-98b7-4e29-a03f-160174643002"  # chlorinator → app (notify)
UUID_RX = "45000004-98b7-4e29-a03f-160174643002"  # app → chlorinator (write)

# Encryption key (from pychlorinator)
SECRET_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")

# Command IDs
CMD_REGISTER_USERNAME = 719  # 0x02CF
CMD_PASSWORD_FRAGMENT = 720  # 0x02D0


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR two byte arrays, zero-padding the shorter one."""
    short, long_ = sorted((a, b), key=len)
    short = short.ljust(len(long_), b"\0")
    return bytes(x ^ y for x, y in zip(short, long_))


def _aes_encrypt(data: bytes) -> bytes:
    """AES-128-ECB encrypt a 16-byte block with the shared secret."""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            cipher = Cipher(algorithms.AES(SECRET_KEY), modes.ECB())
            enc = cipher.encryptor()
            return enc.update(data) + enc.finalize()
        except ImportError:
            raise ImportError(
                "Either pycryptodome or cryptography is required for BLE pairing. "
                "Install with: pip install pycryptodome"
            )
    cipher = AES.new(SECRET_KEY, AES.MODE_ECB)
    return cipher.encrypt(data)


def encrypt_mac_key(session_key: bytes, access_code: bytes) -> bytes:
    """Encrypt the MAC authentication key."""
    xored = _xor_bytes(session_key, access_code)
    return _aes_encrypt(xored)


def encrypt_characteristic(data: bytes, session_key: bytes) -> bytes:
    """Encrypt a characteristic write payload (20 bytes)."""
    xored = _xor_bytes(data, session_key)
    array = _aes_encrypt(xored[:16]) + xored[16:]
    array = array[:4] + _aes_encrypt(array[4:])
    return array


def decrypt_characteristic(data: bytes, session_key: bytes) -> bytes:
    """Decrypt a characteristic notification payload."""
    try:
        from Crypto.Cipher import AES
        cipher = AES.new(SECRET_KEY, AES.MODE_ECB)
        decrypt = cipher.decrypt
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(SECRET_KEY), modes.ECB())
        dec = cipher.decryptor()
        decrypt = lambda d: dec.update(d) + dec.finalize()

    array = data[:4] + decrypt(data[4:])
    array = decrypt(array[:16]) + array[16:]
    return _xor_bytes(array, session_key)


def build_username_command(username: str) -> bytes:
    """Build the BLE command 719 payload for registering a username.
    
    Format: [0x00, username_length, username_utf8_bytes...]
    Padded to 17 bytes (20 total with 3-byte header added by caller).
    
    Max username length: 14 characters.
    """
    if len(username) > 14:
        username = username[:14]
    
    name_bytes = username.encode("utf-8")
    payload = bytes([0x00, len(username)]) + name_bytes
    # Pad to fit in 17-byte body (20 total - 3 byte header)
    return payload.ljust(17, b"\x00")


def parse_password_fragment(body: bytes) -> tuple[bool, int, int, str]:
    """Parse a password fragment from command 720.
    
    Args:
        body: The command body bytes (after the 3-byte frame header).
    
    Returns:
        (is_last, fragment_index, total_length, ascii_fragment)
    """
    if len(body) < 3:
        raise ValueError(f"Password fragment too short: {len(body)} bytes")
    
    is_last = body[0] == 0  # 0 = this IS the last fragment
    fragment_index = body[1]
    total_length = body[2]
    
    # Halo password fragments are up to 13 ASCII bytes, with a trailing
    # packet counter byte in the characteristic payload that must be ignored.
    raw_fragment = body[3:16]

    if is_last:
        # Last fragment: only use the remaining password bytes.
        actual_length = total_length - (fragment_index * 13)
        raw_fragment = raw_fragment[:actual_length]
    
    # Convert to ASCII string
    try:
        fragment_str = raw_fragment.decode("ascii")
    except UnicodeDecodeError:
        fragment_str = raw_fragment.decode("ascii", errors="replace")
    
    return is_last, fragment_index, total_length, fragment_str


async def pair_via_ble(
    ble_address: str | Any,
    access_code: str,
    username: str,
    timeout: float = 30.0,
) -> str:
    """Pair with a Halo chlorinator via BLE and retrieve the cloud password.

    This requires:
    - The chlorinator to be discoverable via BLE
    - The 4-digit access code (displayed on the chlorinator during pairing mode)
    - bleak and pycryptodome/cryptography installed

    Args:
        ble_address: BLE MAC address or BLEDevice for the chlorinator
        access_code: 4-character pairing code from the chlorinator
        username: Username to register (max 14 chars)
        timeout: Timeout in seconds for the pairing process

    Returns:
        The generated cloud password (typically 64 characters).

    Raises:
        ImportError: If bleak is not installed.
        RuntimeError: If pairing fails.
        asyncio.TimeoutError: If the process times out.
    """
    try:
        from bleak import BleakClient
    except ImportError:
        raise ImportError(
            "bleak is required for BLE pairing. Install with: pip install bleak"
        )

    try:
        from bleak_retry_connector import (
            BleakClientWithServiceCache,
            establish_connection,
        )
    except ImportError:
        BleakClientWithServiceCache = None
        establish_connection = None

    async def _connect_client(target: str | Any):
        if establish_connection is not None and not isinstance(target, str):
            target_name = getattr(target, "name", None) or getattr(target, "address", None) or "HCHLOR"
            return await establish_connection(
                BleakClientWithServiceCache,
                target,
                target_name,
                max_attempts=4,
            )

        client = BleakClient(target, timeout=10)
        await client.connect()
        return client

    last_error: Exception | None = None

    for attempt in range(1, 4):
        password_fragments: list[str] = []
        password_complete = asyncio.Event()
        pairing_error: Optional[str] = None
        client = None

        try:
            client = await _connect_client(ble_address)
            LOGGER.debug(
                "Connected to %s (attempt %d/3)",
                getattr(client, "address", ble_address),
                attempt,
            )

            # Step 1: Read session key
            session_key = await client.read_gatt_char(UUID_SESSION_KEY)
            LOGGER.debug("Session key read ok (%d bytes)", len(session_key))

            # Step 2: Authenticate with MAC key
            mac = encrypt_mac_key(session_key, access_code.encode("utf-8"))
            await client.write_gatt_char(UUID_AUTH, mac)
            LOGGER.debug("Authentication write ok")

            # Step 3: Set up notification handler for password fragments
            def notification_handler(_, data: bytearray):
                nonlocal pairing_error

                decrypted = decrypt_characteristic(bytes(data), session_key)
                cmd_id = int.from_bytes(decrypted[1:3], byteorder="little")
                body = decrypted[3:]

                LOGGER.debug("Notification received cmd=%d", cmd_id)

                if cmd_id == CMD_REGISTER_USERNAME:
                    # Response to username registration
                    result_code = body[0]
                    LOGGER.debug("Username registration response code=%d", result_code)
                    if result_code == 0:
                        LOGGER.info("Username accepted, waiting for password...")
                    elif result_code == 2:
                        LOGGER.info("Username already exists, waiting for password...")
                    else:
                        error_messages = {
                            1: "User list is full",
                            3: "Invalid name length",
                            4: "Password fragment received (unexpected)",
                            5: "Password fragment out of range",
                            6: "User ID out of range",
                            7: "Waiting for password fragment 0",
                        }
                        pairing_error = error_messages.get(result_code, f"Unknown error ({result_code})")
                        LOGGER.error("Pairing failed: %s", pairing_error)
                        password_complete.set()

                elif cmd_id == CMD_PASSWORD_FRAGMENT:
                    is_last, frag_idx, total_len, fragment = parse_password_fragment(body)
                    LOGGER.debug(
                        "Password fragment %d received (last=%s, total=%d)",
                        frag_idx, is_last, total_len,
                    )
                    password_fragments.append(fragment)

                    if is_last:
                        password_complete.set()

            await client.start_notify(UUID_TX, notification_handler)
            LOGGER.debug("Notifications enabled")

            # Step 4: Send username registration command
            username_payload = build_username_command(username)
            # BLE write format: [prefix=3, cmd_lo, cmd_hi] + payload, padded to 20 bytes
            # cmd 719 = 0x02CF
            write_data = bytes([3]) + struct.pack("<H", CMD_REGISTER_USERNAME) + username_payload
            write_data = write_data[:20].ljust(20, b"\x00")

            encrypted = encrypt_characteristic(write_data, session_key)
            await client.write_gatt_char(UUID_RX, encrypted)
            LOGGER.debug("Sent username registration")

            # Step 5: Wait for password fragments
            try:
                await asyncio.wait_for(password_complete.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                LOGGER.warning(
                    "Timed out waiting for username/password notifications after %.1fs",
                    timeout,
                )
                raise asyncio.TimeoutError(
                    f"Timed out waiting for password after {timeout}s. "
                    "Is the chlorinator in pairing mode?"
                )

            if pairing_error:
                raise RuntimeError(f"Pairing failed: {pairing_error}")

            password = "".join(password_fragments)
            LOGGER.info("Pairing complete, password length: %d chars", len(password))
            return password
        except (RuntimeError, asyncio.TimeoutError):
            raise
        except Exception as err:
            last_error = err
            if attempt >= 3:
                raise
            LOGGER.warning(
                "BLE pairing transport attempt %d/3 failed, retrying: %s",
                attempt,
                err,
            )
            await asyncio.sleep(attempt)
        finally:
            if client is not None and getattr(client, "is_connected", False):
                await client.disconnect()

    if last_error is not None:
        raise RuntimeError(f"BLE pairing failed: {last_error}")

    raise RuntimeError("BLE pairing failed for an unknown reason")


async def pair_via_cloud(
    serial_number: str,
    username: str,
    ws_client=None,
) -> Optional[str]:
    """Attempt to pair via the cloud WebSocket (EXPERIMENTAL).
    
    This sends the username registration command over the cloud WebSocket
    and waits for password fragments. This may or may not work — the
    chlorinator might only accept pairing commands over BLE.
    
    Args:
        serial_number: Chlorinator serial number
        username: Username to register (max 14 chars)
        ws_client: An already-connected HaloWebSocketClient instance
    
    Returns:
        The generated password, or None if cloud pairing is not supported.
    """
    if ws_client is None or not ws_client.data.connected:
        raise RuntimeError("WebSocket client must be connected")
    
    # Build the cloud command: prefix(01) + cmd(0x02CF LE) + payload
    payload = build_username_command(username)
    command = bytes([0x01]) + struct.pack("<H", CMD_REGISTER_USERNAME) + payload
    
    LOGGER.info("Attempting cloud pairing for username '%s' (EXPERIMENTAL)", username)
    
    password_fragments: list[str] = []
    original_callback = ws_client.on_data
    complete = asyncio.Event()
    
    def data_handler(parsed):
        cmd_id = parsed.get("cmd_id", 0)
        
        if cmd_id == CMD_PASSWORD_FRAGMENT:
            body = bytes.fromhex(parsed.get("data_hex", ""))
            if len(body) >= 3:
                is_last, frag_idx, total_len, fragment = parse_password_fragment(body)
                LOGGER.debug("Cloud password fragment %d received", frag_idx)
                password_fragments.append(fragment)
                if is_last:
                    complete.set()
        
        # Chain to original callback
        if original_callback:
            original_callback(parsed)
    
    ws_client.on_data = data_handler
    
    try:
        await ws_client.send_command(command)
        
        try:
            await asyncio.wait_for(complete.wait(), timeout=15.0)
            password = "".join(password_fragments)
            LOGGER.info("Cloud pairing succeeded! Password length: %d", len(password))
            return password
        except asyncio.TimeoutError:
            LOGGER.warning(
                "Cloud pairing timed out — pairing may only work over BLE"
            )
            return None
    finally:
        ws_client.on_data = original_callback
