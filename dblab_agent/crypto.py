"""Symmetric encryption for connection passwords saved to disk.

User-added connections are persisted to data/connections.json; their passwords
are Fernet-encrypted with a key kept in data/.secret (auto-generated, never
committed). This keeps plaintext credentials out of the JSON file.
"""
from cryptography.fernet import Fernet

from .config import SECRET_KEY_PATH


def _load_or_create_key() -> bytes:
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    SECRET_KEY_PATH.write_bytes(key)
    try:
        SECRET_KEY_PATH.chmod(0o600)
    except Exception:
        pass  # best-effort on platforms without POSIX perms
    return key


_fernet = Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt((plaintext or "").encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except Exception:
        return ""
