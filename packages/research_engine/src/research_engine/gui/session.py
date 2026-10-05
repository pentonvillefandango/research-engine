"""GUI session cookie, CSRF Origin check, login rate limiting and redirect-target checks (B2).

The cookie value is ``TimestampSigner(session_secret, salt=SALT).sign("gui")``.
"""

import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from urllib.parse import unquote, urlsplit

from itsdangerous import BadSignature, TimestampSigner

COOKIE = "re_session"
MAX_AGE_S = 12 * 3600
SALT = "research-engine-gui"
_PAYLOAD = b"gui"
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class SessionCodec:
    def __init__(self, secret: str) -> None:
        self._signer = TimestampSigner(secret, salt=SALT)

    def issue(self) -> str:
        return self._signer.sign(_PAYLOAD).decode()

    def valid(self, value: str | None) -> bool:
        """True only for an untampered cookie signed with our secret and salt, under 12 h old.

        itsdangerous compares signatures in constant time (``hmac.compare_digest``).
        """
        if not value:
            return False
        try:
            return self._signer.unsign(value, max_age=MAX_AGE_S) == _PAYLOAD
        except (BadSignature, UnicodeError):  # SignatureExpired is a BadSignature
            return False


def cookie_values(headers: Iterable[str], name: str) -> list[str]:
    """Every value of cookie ``name`` across raw ``Cookie`` header values.

    Deliberately lenient: malformed pairs are skipped, never raised on, and a bad pair does not
    hide the pairs after it (unlike ``http.cookies.SimpleCookie``). Surrounding double quotes are
    removed. All candidates are returned so a planted junk cookie cannot shadow the real one.
    """
    found: list[str] = []
    for header in headers:
        for pair in header.split(";"):
            key, sep, value = pair.partition("=")
            if not sep or key.strip() != name:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            found.append(value)
    return found


def _source_matches(source: str, host: str, site_host: str) -> bool:
    try:
        parts = urlsplit(source.strip())
        hostname = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not hostname or "@" in parts.netloc:
        return False
    netloc = parts.netloc.lower()
    allowed = {site_host.lower(), host.lower()} - {""}
    return netloc in allowed or hostname in allowed


def same_origin(origins: list[str], referers: list[str], host: str, site_host: str) -> bool:
    """CSRF defence in depth for state-changing requests.

    Uses the ``Origin`` header(s) or, when there is none, the ``Referer``. Every value present
    must name ``site_host`` or the request ``Host``. With neither header the request is refused.
    """
    sources = origins or referers
    return bool(sources) and all(_source_matches(s, host, site_host) for s in sources)


def _bad_path(value: str) -> bool:
    return (
        not value.startswith("/")
        or value.startswith(("//", "/\\"))
        or "\\" in value
        or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value)
    )


def safe_next(value: str | None) -> str:
    """``value`` if it is a same-site relative path (``/...``, not ``//`` or ``/\\``, no scheme or
    host, no whitespace or control characters, also once percent-decoded); otherwise ``/``."""
    if not value or len(value) > 2048 or _bad_path(value) or _bad_path(unquote(value)):
        return "/"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return value


class LoginRateLimiter:
    """Failed-login limiter per client IP: ``max_failures`` within ``window_s`` blocks the client
    until the oldest of those failures leaves the window. Memory is bounded to ``max_clients``
    (least recently failed client evicted first); checking never allocates."""

    def __init__(
        self,
        max_failures: int = 5,
        window_s: float = 60.0,
        *,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max, self._window, self._cap, self._clock = (
            max_failures,
            window_s,
            max_clients,
            clock,
        )
        self._fails: OrderedDict[str, deque[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._fails)

    def _recent(self, client: str) -> deque[float] | None:
        q = self._fails.get(client)
        if q is None:
            return None
        cutoff = self._clock() - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            del self._fails[client]
            return None
        return q

    def blocked(self, client: str) -> bool:
        q = self._recent(client)
        return q is not None and len(q) >= self._max

    def retry_after(self, client: str) -> int:
        """Whole seconds until ``client`` is unblocked (0 if not blocked)."""
        q = self._recent(client)
        if q is None or len(q) < self._max:
            return 0
        return max(1, int(q[-self._max] + self._window - self._clock() + 0.999))

    def fail(self, client: str) -> None:
        q = self._recent(client)
        if q is None:
            q = self._fails[client] = deque(maxlen=self._max)
            while len(self._fails) > self._cap:
                self._fails.popitem(last=False)
        else:
            self._fails.move_to_end(client)
        q.append(self._clock())

    def reset(self, client: str) -> None:
        self._fails.pop(client, None)
