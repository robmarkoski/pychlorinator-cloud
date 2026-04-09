"""Custom exceptions for the Halo connectivity stack."""

from __future__ import annotations


class ChlorinatorError(Exception):
    """Base class for library errors."""


class StunError(ChlorinatorError):
    """Raised for STUN request or parsing failures."""


class SignallingError(ChlorinatorError):
    """Raised for signalling connection or protocol failures."""


class SignallingUnavailableError(SignallingError):
    """Raised when the target chlorinator is unavailable."""


class SignallingAuthenticationError(SignallingError):
    """Raised when supplied credentials are rejected."""


class SignallingBusyError(SignallingError):
    """Raised when the chlorinator is already busy."""


class SignallingRateLimitedError(SignallingError):
    """Raised when signalling rejects the request due to rate limits."""


class SignallingDosProtectionError(SignallingError):
    """Raised when signalling enables DoS protection."""


class DtlsTransportError(ChlorinatorError):
    """Raised for OpenSSL DTLS transport failures."""


class ChlorinatorProtocolError(ChlorinatorError):
    """Raised for invalid or unexpected protocol messages."""
