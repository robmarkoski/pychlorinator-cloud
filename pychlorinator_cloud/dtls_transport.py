"""Async OpenSSL DTLS wrapper for Halo P2P connectivity."""

from __future__ import annotations

import asyncio
import logging
from asyncio.subprocess import Process

from .const import DTLS_CIPHER_STRING, DTLS_PSK_IDENTITY
from .exceptions import DtlsTransportError

LOGGER = logging.getLogger(__name__)


class OpenSslDtlsTransport:
    """Thin asyncio wrapper around `openssl s_client` with DTLS-PSK."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        psk: bytes,
        local_host: str = "0.0.0.0",
        local_port: int = 0,
        openssl_binary: str = "openssl",
        psk_identity: str = DTLS_PSK_IDENTITY,
        cipher_string: str = DTLS_CIPHER_STRING,
    ) -> None:
        self.host = host
        self.port = port
        self.psk = psk
        self.local_host = local_host
        self.local_port = local_port
        self.openssl_binary = openssl_binary
        self.psk_identity = psk_identity
        self.cipher_string = cipher_string
        self._process: Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        """Start the OpenSSL subprocess and wait briefly for startup errors."""

        if self._process is not None:
            return
        cmd = [
            self.openssl_binary,
            "s_client",
            "-dtls1_2",
            "-quiet",
            "-connect",
            f"{self.host}:{self.port}",
            "-bind",
            f"{self.local_host}:{self.local_port}",
            "-psk",
            self.psk.hex(),
            "-psk_identity",
            self.psk_identity,
            "-cipher",
            self.cipher_string,
        ]
        LOGGER.debug("Starting DTLS transport: %s", " ".join(cmd[:-3]) + " ...")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise DtlsTransportError(f"Failed to start OpenSSL: {exc}") from exc

        self._stderr_task = asyncio.create_task(self._log_stderr(), name="halo-dtls-stderr")
        await asyncio.sleep(0.25)
        if self._process.returncode is not None:
            raise DtlsTransportError(
                f"OpenSSL exited early with code {self._process.returncode}"
            )

    async def send(self, data: bytes) -> None:
        """Send raw DTLS application bytes."""

        process = self._require_process()
        if process.stdin is None:
            raise DtlsTransportError("OpenSSL stdin is not available")
        process.stdin.write(data)
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise DtlsTransportError("DTLS transport write failed") from exc

    async def recv(self, max_bytes: int = 4096) -> bytes:
        """Receive raw bytes from the DTLS session."""

        process = self._require_process()
        if process.stdout is None:
            raise DtlsTransportError("OpenSSL stdout is not available")
        data = await process.stdout.read(max_bytes)
        if not data:
            if process.returncode is not None:
                raise DtlsTransportError(
                    f"OpenSSL exited with code {process.returncode} while reading"
                )
            raise DtlsTransportError("DTLS transport returned EOF")
        return data

    async def close(self) -> None:
        """Close the subprocess cleanly."""

        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
            self._stderr_task = None

    def _require_process(self) -> Process:
        if self._process is None:
            raise DtlsTransportError("DTLS transport is not connected")
        return self._process

    async def _log_stderr(self) -> None:
        process = self._require_process()
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            LOGGER.debug("openssl: %s", line.decode("utf-8", errors="replace").rstrip())
