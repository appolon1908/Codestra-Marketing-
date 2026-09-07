from __future__ import annotations

import pytest

from app.middleware_client import MiddlewareDeliveryError, MiddlewareMarketingClient
from app.outbox_worker import claim_one


@pytest.mark.asyncio
async def test_worker_kill_switch_prevents_claim(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "false")
    assert await claim_one(30) is None


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost.attacker.example",
        "http://localhost@attacker.example",
        "ftp://middleware.example",
        "",
    ],
)
def test_middleware_endpoint_rejects_malformed_or_plaintext_remote_urls(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", value)
    with pytest.raises(MiddlewareDeliveryError, match="middleware_base_url_invalid"):
        MiddlewareMarketingClient()._endpoint()


@pytest.mark.parametrize(
    "value",
    ["https://middleware.example", "http://127.0.0.1:8000", "http://localhost:8000"],
)
def test_middleware_endpoint_accepts_https_or_exact_loopback(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", value)
    assert MiddlewareMarketingClient()._endpoint().endswith(
        "/api/v1/control/marketing/campaign-activations"
    )
