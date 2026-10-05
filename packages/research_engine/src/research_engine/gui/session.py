"""GUI session cookie, CSRF Origin check, login rate limiting and redirect-target checks (B2).

The cookie value is ``TimestampSigner(session_secret, salt=SALT).sign("gui")``.
"""

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from urllib.parse import unquote, urlsplit

from itsdangerous import BadSignature, TimestampSigner

COOKIE = "re_session"
MAX_AGE_S = 12 * 3600
SALT = "research-engine-gui"
_PAYLOAD = b"gui"
# Allowlist: every other method (POST, PUT, PATCH, DELETE, and anything unknown) is treated as
# state-changing and Origin-checked, so new routes and MCP verbs fail closed.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
type Origin = tuple[str, str, int]  # (scheme, lowercase host, effective port)


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


def _origin_of(scheme: str, netloc: str) -> Origin | None:
    """Normalise ``scheme`` + ``netloc`` to ``(scheme, host, port)``; ``None`` if unusable.

    Rejects userinfo, empty or trailing-dot hosts, bad ports and non-http(s) schemes. The
    default port of the scheme is filled in, so ``http://h`` and ``http://h:80`` are equal.
    """
    scheme = scheme.lower()
    if scheme not in _DEFAULT_PORTS or not netloc or "@" in netloc:
        return None
    try:
        parts = urlsplit(f"//{netloc}")
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or host.endswith(".") or parts.path or parts.query or parts.fragment:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def _source_origin(source: str) -> Origin | None:
    try:
        parts = urlsplit(source.strip())
    except ValueError:
        return None
    return _origin_of(parts.scheme, parts.netloc)


def same_origin(
    origins: list[str], referers: list[str], scheme: str, host: str, site_host: str
) -> bool:
    """CSRF defence in depth for non-safe requests.

    The source is the ``Origin`` header(s) or, when there is none, the ``Referer``. Each value
    must normalise to exactly ``(scheme, host, port)`` of the request itself (its scheme plus
    ``Host``) or of ``site_host`` (with the request's scheme, default port unless ``site_host``
    names one). With neither header the request is refused.
    """
    allowed = {o for o in (_origin_of(scheme, host), _origin_of(scheme, site_host)) if o}
    sources = origins or referers
    return bool(sources and allowed) and all(_source_origin(s) in allowed for s in sources)


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
    """Failed-login limiter per client IP with a fixed lockout: the ``max_failures``-th failure
    within ``window_s`` blocks the client until ``window_s`` after that failure, whatever
    happens meanwhile. Memory is bounded to ``max_clients`` (least recently failed client
    evicted first); checking never allocates."""

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
        self._blocked_until: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._fails) + len(self._blocked_until)

    def _until(self, client: str) -> float | None:
        until = self._blocked_until.get(client)
        if until is not None and self._clock() >= until:
            del self._blocked_until[client]
            return None
        return until

    def blocked(self, client: str) -> bool:
        return self._until(client) is not None

    def retry_after(self, client: str) -> int:
        """Whole seconds until ``client`` is unblocked (0 if not blocked)."""
        until = self._until(client)
        return 0 if until is None else max(1, math.ceil(until - self._clock()))

    def fail(self, client: str) -> None:
        if self.blocked(client):
            return  # attempts during a lockout neither count nor extend it
        now = self._clock()
        q = self._fails.get(client)
        if q is None:
            q = self._fails[client] = deque(maxlen=self._max)
        else:
            self._fails.move_to_end(client)
        while q and q[0] <= now - self._window:
            q.popleft()
        q.append(now)
        if len(q) >= self._max:
            del self._fails[client]
            self._blocked_until[client] = now + self._window
        while len(self._fails) > self._cap:
            self._fails.popitem(last=False)
        self._evict_expired_blocks(now)

    def _evict_expired_blocks(self, now: float) -> None:
        if len(self._blocked_until) > self._cap:
            for c in [c for c, until in self._blocked_until.items() if until <= now]:
                del self._blocked_until[c]
            while len(self._blocked_until) > self._cap:  # oldest lockouts end first
                del self._blocked_until[next(iter(self._blocked_until))]

    def reset(self, client: str) -> None:
        self._fails.pop(client, None)
        self._blocked_until.pop(client, None)
