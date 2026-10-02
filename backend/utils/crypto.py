"""Symmetric encryption for at-rest secrets (tenant DB passwords, tool secrets).

The Fernet key comes from config (``security.secrets_fernet_key``) — never from a
DB, never from dispatch metadata. Decryption happens in-process in the API and the
worker; plaintext is never stored, returned over the API, or logged.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from settings import get_settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    # Lazy: build the cipher once on first use (defers reading the key until the
    # env is loaded), then reuse the module-global instance. SecurityConfig has
    # already validated that the key constructs, so this cannot fail here.
    global _fernet
    if _fernet is None:
        _fernet = Fernet(get_settings().security.secrets_fernet_key.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()
