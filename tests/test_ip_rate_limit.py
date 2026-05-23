import httpx
import pytest
from datasette.app import Datasette

from datasette_ip_rate_limit import RateLimitState


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def datasette_with_config(config):
    return Datasette(
        memory=True,
        metadata={"plugins": {"datasette-ip-rate-limit": config}},
    )


def client_for(ds, clock=None, client_addr=("127.0.0.1", 123)):
    app = ds.app()
    if clock is not None:
        ds._datasette_ip_rate_limit_state.clock = clock
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client_addr))


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-ip-rate-limit" in installed_plugins


@pytest.mark.asyncio
async def test_no_config_passes_through():
    ds = Datasette(memory=True)
    async with client_for(ds) as client:
        responses = [
            await client.get("http://localhost/data/table?a=1&b=2") for _ in range(3)
        ]
    assert [response.status_code for response in responses] == [404, 404, 404]


@pytest.mark.asyncio
async def test_blocks_ip_after_limit_using_asgi_client_by_default():
    ds = datasette_with_config(
        {
            "rules": [
                {
                    "paths": ["/data/*"],
                    "query_params_min": 2,
                    "window_seconds": 60,
                    "max_requests": 2,
                    "block_seconds": 300,
                }
            ]
        }
    )
    clock = FakeClock()

    async with (
        client_for(ds, clock, ("203.0.113.10", 123)) as client,
        client_for(ds, clock, ("203.0.113.11", 123)) as other_client,
    ):
        first = await client.get("http://localhost/data/table?a=1&b=2")
        second = await client.get("http://localhost/data/table?a=1&b=2")
        third = await client.get("http://localhost/data/table?a=1&b=2")
        other_ip = await other_client.get("http://localhost/data/table?a=1&b=2")

    assert first.status_code == 404
    assert second.status_code == 404
    assert third.status_code == 429
    assert third.headers["retry-after"] == "300"
    assert "Rate limit exceeded" in third.text
    assert other_ip.status_code == 404


@pytest.mark.asyncio
async def test_configured_header_overrides_asgi_client_ip():
    ds = datasette_with_config(
        {
            "header": "X-Forwarded-For",
            "rules": [
                {
                    "paths": ["/data/*"],
                    "window_seconds": 60,
                    "max_requests": 1,
                    "block_seconds": 300,
                }
            ],
        }
    )

    async with client_for(ds, FakeClock(), ("203.0.113.10", 123)) as client:
        first = await client.get(
            "http://localhost/data/table",
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        second = await client.get(
            "http://localhost/data/table",
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
        blocked = await client.get(
            "http://localhost/data/table",
            headers={"X-Forwarded-For": "198.51.100.1"},
        )

    assert first.status_code == 404
    assert second.status_code == 404
    assert blocked.status_code == 429


@pytest.mark.asyncio
async def test_ip_prefix_length_groups_multiple_ips_into_one_bucket():
    ds = datasette_with_config(
        {
            "rules": [
                {
                    "paths": ["/data/*"],
                    "window_seconds": 60,
                    "max_requests": 2,
                    "block_seconds": 300,
                    "ip_prefix_length": 24,
                }
            ]
        }
    )

    async with (
        client_for(ds, FakeClock(), ("203.0.113.10", 123)) as client,
        client_for(ds, FakeClock(), ("203.0.113.11", 123)) as other_client,
    ):
        first = await client.get("http://localhost/data/table")
        second = await other_client.get("http://localhost/data/table")
        third = await client.get("http://localhost/data/table")

    assert first.status_code == 404
    assert second.status_code == 404
    assert third.status_code == 429


@pytest.mark.asyncio
async def test_debug_route_shows_grouped_subnet_key():
    ds = datasette_with_config(
        {
            "debug": True,
            "rules": [
                {
                    "paths": ["/data/*"],
                    "window_seconds": 60,
                    "max_requests": 1,
                    "block_seconds": 300,
                    "ip_prefix_length": 24,
                }
            ],
        }
    )

    async with client_for(ds, FakeClock(), ("203.0.113.10", 123)) as client:
        first = await client.get("http://localhost/data/table")
        response = await client.get("http://localhost/-/ip-rate-limit-debug")

    assert first.status_code == 404
    assert response.status_code == 200
    assert response.json()["buckets"][0]["client_ip"] == "203.0.113.0/24"


@pytest.mark.asyncio
async def test_debug_route_is_not_available_by_default():
    ds = datasette_with_config(
        {
            "rules": [
                {
                    "paths": ["/data/*"],
                    "window_seconds": 60,
                    "max_requests": 1,
                    "block_seconds": 300,
                }
            ],
        }
    )

    async with client_for(ds) as client:
        response = await client.get("http://localhost/-/ip-rate-limit-debug")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_debug_route_returns_pretty_printed_json_state():
    ds = datasette_with_config(
        {
            "debug": True,
            "rules": [
                {
                    "paths": ["/data/*"],
                    "window_seconds": 60,
                    "max_requests": 1,
                    "block_seconds": 300,
                }
            ],
        }
    )
    clock = FakeClock()

    async with client_for(ds, clock, ("203.0.113.10", 123)) as client:
        first = await client.get("http://localhost/data/table")
        clock.advance(5)
        response = await client.get("http://localhost/-/ip-rate-limit-debug")

    assert first.status_code == 404
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.text.startswith("{\n  ")
    assert response.text.endswith("\n")

    data = response.json()
    assert data["config"]["debug"] is True
    assert data["bucket_count"] == 1
    assert data["now"] == 5.0
    assert data["buckets"] == [
        {
            "rule": "0",
            "client_ip": "203.0.113.10",
            "tokens": 0.0,
            "updated_at": 0.0,
            "seconds_since_updated": 5.0,
            "blocked_until": 0.0,
            "blocked_for_seconds": 0.0,
        }
    ]


@pytest.mark.asyncio
async def test_debug_route_is_not_rate_limited_or_tracked():
    ds = datasette_with_config(
        {
            "debug": True,
            "rules": [
                {
                    "paths": ["/*"],
                    "window_seconds": 60,
                    "max_requests": 1,
                    "block_seconds": 300,
                }
            ],
        }
    )

    async with client_for(ds, FakeClock()) as client:
        first = await client.get("http://localhost/-/ip-rate-limit-debug")
        second = await client.get("http://localhost/-/ip-rate-limit-debug")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["bucket_count"] == 0
    assert second.json()["bucket_count"] == 0


@pytest.mark.asyncio
async def test_block_expires_after_configured_period():
    ds = datasette_with_config(
        {
            "rules": [
                {
                    "paths": ["/data/*"],
                    "window_seconds": 60,
                    "max_requests": 1,
                    "block_seconds": 10,
                }
            ]
        }
    )
    clock = FakeClock()

    async with client_for(ds, clock) as client:
        first = await client.get("http://localhost/data/table")
        blocked = await client.get("http://localhost/data/table")
        clock.advance(11)
        allowed = await client.get("http://localhost/data/table")

    assert first.status_code == 404
    assert blocked.status_code == 429
    assert allowed.status_code == 404


@pytest.mark.asyncio
async def test_query_params_min_only_counts_matching_requests():
    ds = datasette_with_config(
        {
            "rules": [
                {
                    "paths": ["/data/*"],
                    "query_params_min": 2,
                    "window_seconds": 60,
                    "max_requests": 1,
                    "block_seconds": 300,
                }
            ]
        }
    )

    async with client_for(ds, FakeClock()) as client:
        one_param = await client.get("http://localhost/data/table?a=1")
        first_two_params = await client.get("http://localhost/data/table?a=1&b=2")
        second_two_params = await client.get("http://localhost/data/table?a=1&b=2")

    assert one_param.status_code == 404
    assert first_two_params.status_code == 404
    assert second_two_params.status_code == 429


@pytest.mark.asyncio
async def test_exempt_paths_are_not_rate_limited():
    ds = datasette_with_config(
        {
            "exempt_paths": ["/static/*"],
            "rules": [
                {
                    "paths": ["/*"],
                    "window_seconds": 60,
                    "max_requests": 1,
                    "block_seconds": 300,
                }
            ],
        }
    )

    async with client_for(ds, FakeClock()) as client:
        static_one = await client.get("http://localhost/static/site.css")
        static_two = await client.get("http://localhost/static/site.css")
        dynamic_one = await client.get("http://localhost/data/table")
        dynamic_two = await client.get("http://localhost/data/table")

    assert static_one.status_code == 404
    assert static_two.status_code == 404
    assert dynamic_one.status_code == 404
    assert dynamic_two.status_code == 429


def test_state_evicts_oldest_keys_to_cap_memory():
    state = RateLimitState(clock=FakeClock())

    state.check(("rule", "203.0.113.1"), 10, 60, 300, max_keys=2)
    state.check(("rule", "203.0.113.2"), 10, 60, 300, max_keys=2)
    state.check(("rule", "203.0.113.1"), 10, 60, 300, max_keys=2)
    state.check(("rule", "203.0.113.3"), 10, 60, 300, max_keys=2)

    assert list(state.buckets.keys()) == [
        ("rule", "203.0.113.1"),
        ("rule", "203.0.113.3"),
    ]
