from collections import OrderedDict
from dataclasses import dataclass
from math import ceil
import re
from time import monotonic
from urllib.parse import parse_qsl

from datasette import hookimpl

DEFAULT_HEADER = "Fly-Client-IP"
DEFAULT_MAX_KEYS = 10000
DEFAULT_MAX_REQUESTS = 60
DEFAULT_WINDOW_SECONDS = 60
DEFAULT_BLOCK_SECONDS = 300


@dataclass
class Bucket:
    tokens: float
    updated_at: float
    blocked_until: float = 0.0


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after: int | None = None


class RateLimitState:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.buckets = OrderedDict()

    def check(
        self,
        key,
        max_requests,
        window_seconds,
        block_seconds,
        max_keys=DEFAULT_MAX_KEYS,
    ):
        now = self.clock()
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = Bucket(tokens=float(max_requests), updated_at=now)
            self.buckets[key] = bucket
        self.buckets.move_to_end(key)

        if bucket.blocked_until > now:
            self._evict(max_keys)
            return RateLimitResult(False, max(1, ceil(bucket.blocked_until - now)))

        if bucket.blocked_until:
            bucket.blocked_until = 0.0
            bucket.tokens = float(max_requests)
            bucket.updated_at = now
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            refill_rate = max_requests / window_seconds
            bucket.tokens = min(
                float(max_requests), bucket.tokens + elapsed * refill_rate
            )
            bucket.updated_at = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            self._evict(max_keys)
            return RateLimitResult(True)

        bucket.blocked_until = now + block_seconds
        bucket.updated_at = now
        self._evict(max_keys)
        if block_seconds:
            return RateLimitResult(False, max(1, ceil(block_seconds)))

        refill_rate = max_requests / window_seconds
        retry_after = ceil((1 - bucket.tokens) / refill_rate)
        return RateLimitResult(False, max(1, retry_after))

    def _evict(self, max_keys):
        while len(self.buckets) > max_keys:
            self.buckets.popitem(last=False)


class IpRateLimitMiddleware:
    def __init__(self, app, datasette, state):
        self.app = app
        self.datasette = datasette
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        config = self.datasette.plugin_config("datasette-ip-rate-limit") or {}
        if not config or config.get("enabled") is False:
            await self.app(scope, receive, send)
            return

        rules = config.get("rules") or []
        if not rules:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        query_string = (scope.get("query_string") or b"").decode("latin-1")

        if _matches_any(path, _as_list(config.get("exempt_paths"))):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        query_count = len(parse_qsl(query_string, keep_blank_values=True))
        matching_rule = None
        for index, rule in enumerate(rules):
            if _rule_matches(rule, path, method, query_count):
                matching_rule = (index, rule)
                break

        if matching_rule is None:
            await self.app(scope, receive, send)
            return

        client_ip = _client_ip(scope, config.get("header") or DEFAULT_HEADER)
        if not client_ip:
            await self.app(scope, receive, send)
            return

        index, rule = matching_rule
        max_requests = _positive_int(rule.get("max_requests"), DEFAULT_MAX_REQUESTS)
        window_seconds = _positive_float(
            rule.get("window_seconds"), DEFAULT_WINDOW_SECONDS
        )
        block_seconds = _non_negative_float(
            rule.get("block_seconds"), DEFAULT_BLOCK_SECONDS
        )
        max_keys = _positive_int(config.get("max_keys"), DEFAULT_MAX_KEYS)
        rule_name = rule.get("name") or str(index)

        result = self.state.check(
            (rule_name, client_ip),
            max_requests,
            window_seconds,
            block_seconds,
            max_keys=max_keys,
        )
        if result.allowed:
            await self.app(scope, receive, send)
            return

        await _send_rate_limited(send, result.retry_after)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _pattern_to_regex(pattern):
    escaped = re.escape(pattern)
    return re.compile("^{}$".format(escaped.replace(r"\*", ".*")))


def _matches_any(path, patterns):
    return any(_pattern_to_regex(pattern).match(path) for pattern in patterns)


def _rule_matches(rule, path, method, query_count):
    methods = {item.upper() for item in _as_list(rule.get("methods"))}
    if methods and method not in methods:
        return False

    paths = _as_list(rule.get("paths") or rule.get("path"))
    if not paths or not _matches_any(path, paths):
        return False

    query_params_min = rule.get("query_params_min")
    if query_params_min is not None and query_count < _positive_int(
        query_params_min, 0, minimum=0
    ):
        return False

    return True


def _client_ip(scope, header_name):
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers") or []
    }
    client_ip = headers.get(header_name.lower())
    if not client_ip:
        return None
    # X-Forwarded-For style values are comma-separated; Fly-Client-IP is not.
    return client_ip.split(",", 1)[0].strip()


def _positive_int(value, default, minimum=1):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _positive_float(value, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(0.001, value)


def _non_negative_float(value, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(0.0, value)


async def _send_rate_limited(send, retry_after):
    body = b"Rate limit exceeded. Try again later.\n"
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"cache-control", b"no-store"),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode("ascii")))
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


@hookimpl
def asgi_wrapper(datasette):
    if not hasattr(datasette, "_datasette_ip_rate_limit_state"):
        datasette._datasette_ip_rate_limit_state = RateLimitState()

    def wrap(app):
        return IpRateLimitMiddleware(
            app, datasette, datasette._datasette_ip_rate_limit_state
        )

    return wrap
