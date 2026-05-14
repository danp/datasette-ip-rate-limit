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


def client_for(ds, clock=None):
    app = ds.app()
    if clock is not None:
        ds._datasette_ip_rate_limit_state.clock = clock
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))


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
async def test_blocks_ip_after_limit_using_fly_client_ip_header():
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

    async with client_for(ds, clock) as client:
        headers = {"Fly-Client-IP": "203.0.113.10"}
        first = await client.get("http://localhost/data/table?a=1&b=2", headers=headers)
        second = await client.get(
            "http://localhost/data/table?a=1&b=2", headers=headers
        )
        third = await client.get("http://localhost/data/table?a=1&b=2", headers=headers)
        other_ip = await client.get(
            "http://localhost/data/table?a=1&b=2",
            headers={"Fly-Client-IP": "203.0.113.11"},
        )

    assert first.status_code == 404
    assert second.status_code == 404
    assert third.status_code == 429
    assert third.headers["retry-after"] == "300"
    assert "Rate limit exceeded" in third.text
    assert other_ip.status_code == 404


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
        headers = {"Fly-Client-IP": "203.0.113.10"}
        first = await client.get("http://localhost/data/table", headers=headers)
        blocked = await client.get("http://localhost/data/table", headers=headers)
        clock.advance(11)
        allowed = await client.get("http://localhost/data/table", headers=headers)

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
        headers = {"Fly-Client-IP": "203.0.113.10"}
        one_param = await client.get("http://localhost/data/table?a=1", headers=headers)
        first_two_params = await client.get(
            "http://localhost/data/table?a=1&b=2", headers=headers
        )
        second_two_params = await client.get(
            "http://localhost/data/table?a=1&b=2", headers=headers
        )

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
        headers = {"Fly-Client-IP": "203.0.113.10"}
        static_one = await client.get(
            "http://localhost/static/site.css", headers=headers
        )
        static_two = await client.get(
            "http://localhost/static/site.css", headers=headers
        )
        dynamic_one = await client.get("http://localhost/data/table", headers=headers)
        dynamic_two = await client.get("http://localhost/data/table", headers=headers)

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
