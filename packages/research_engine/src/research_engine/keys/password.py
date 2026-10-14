"""GUI password hashing (V1-23, ADR-0027): scrypt, stored as
``scrypt:<n>:<r>:<p>:<salt b64url>:<hash b64url>`` (no ``$``: Compose would interpolate it)."""

import base64
import hashlib
import hmac
import secrets

N, R, P = 2**14, 8, 1
SALT_BYTES = 16
DKLEN = 32
MIN_LENGTH = 12
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=_MAXMEM)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    return f"scrypt:{N}:{R}:{P}:{_b64(salt)}:{_b64(_derive(password, salt, N, R, P))}"


def check_format(stored: str) -> tuple[int, int, int, bytes, bytes]:
    """Parse ``stored``; ValueError (naming GUI_PASSWORD_HASH, never the value) if malformed."""
    err = ValueError(
        "GUI_PASSWORD_HASH is not a valid hash; set it with: research-engine gui set-password"
    )
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise err
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except ValueError as exc:
        raise err from exc
    if n < 2 or n & (n - 1) or r < 1 or p < 1 or len(salt) < 8 or len(digest) != DKLEN:
        raise err
    return n, r, p, salt, digest


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check; False for an empty password or a malformed hash. CPU-bound:
    callers on the event loop use ``asyncio.to_thread``."""
    if not password:
        return False
    try:
        n, r, p, salt, digest = check_format(stored)
    except ValueError:
        return False
    return hmac.compare_digest(_derive(password, salt, n, r, p), digest)
