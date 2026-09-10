from __future__ import annotations

from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.asgi import app
from app.auth import Principal, _tenant_claim, authenticate
from app.delivery_contract import MarketingCommand


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("tenant", ["acme/us", "team@example.com", "tenant-one", "tenant:one", "tenant!one"])
def test_auth_and_delivery_accept_same_http_safe_tenants(tenant):
    assert _tenant_claim({"tenant_id": tenant}) == tenant
    assert MarketingCommand(operation_id=uuid4(), campaign_id=uuid4(), action="activate",
        expected_state="approved", expected_version=1, tenant_id=tenant, correlation_id="corr-tenant").tenant_id == tenant


@pytest.mark.parametrize("tenant", ["a" * 65, "acme us", "tenant\nforged", "tenant\x7f", "\u2603"])
def test_auth_and_delivery_reject_unsafe_tenants_at_the_boundary(tenant):
    assert _tenant_claim({"tenant_id": tenant}) is None
    with pytest.raises(ValidationError):
        MarketingCommand(operation_id=uuid4(), campaign_id=uuid4(), action="activate",
            expected_state="approved", expected_version=1, tenant_id=tenant, correlation_id="corr-tenant")


@pytest.mark.parametrize("path", ["/internal/v1/marketing/status", "/internal/v1/marketing/accounts/demo/connectivity",
                                   f"/internal/v1/marketing/campaigns/{uuid4()}", f"/internal/v1/marketing/operations/{uuid4()}"])
def test_private_reads_require_authentication(client, path):
    assert client.get(path).status_code == 401


def test_only_explicitly_authorized_middleware_clients_reach_private_boundary(client, monkeypatch):
    monkeypatch.setenv("MARKETING_COMMAND_CLIENT_IDS", "middleware")
    app.dependency_overrides[authenticate] = lambda: Principal("user", "tenant", frozenset({"marketing.provider.read"}), "n8n")
    assert client.get("/internal/v1/marketing/status").status_code == 403
    monkeypatch.delenv("MARKETING_COMMAND_CLIENT_IDS")
    assert client.get("/internal/v1/marketing/status").status_code == 503


def test_missing_scope_is_denied_before_database_access(client, monkeypatch):
    monkeypatch.setenv("MARKETING_COMMAND_CLIENT_IDS", "middleware")
    app.dependency_overrides[authenticate] = lambda: Principal("user", "tenant", frozenset(), "middleware")
    response = client.post("/internal/v1/marketing/commands",
        headers={"Idempotency-Key": "test-key", "X-Correlation-ID": "test-correlation"},
        json={"campaign_id": str(uuid4()), "action": "pause", "expected_version": 1})
    assert response.status_code == 403


@pytest.mark.parametrize("action", ["activate", "resume"])
def test_legacy_activation_cannot_bypass_new_guard(client, action, monkeypatch):
    monkeypatch.setenv("LIVE_ADVERTISING", "true")
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    response = client.post(f"/v1/marketing/campaigns/{uuid4()}/{action}")
    assert response.status_code == 423
    assert response.headers["Cache-Control"] == "no-store"


def test_legacy_global_provider_account_read_is_disabled(client):
    assert client.get("/v1/marketing/providers/meta/accounts/123/campaigns").status_code == 403


def test_original_docs_and_no_spend_capabilities_are_preserved(client):
    assert "/v1/marketing/campaigns" in client.get("/openapi.json").json()["paths"]
    capabilities = client.get("/capabilities").json()
    assert capabilities["live_advertising_enabled"] is False
    assert capabilities["publication_enabled"] is False


@pytest.mark.parametrize("canonical", ["false", "FALSE", "", "invalid", "0"])
def test_canonical_advertising_kill_switch_overrides_legacy_alias(monkeypatch, canonical):
    from app.outbox_worker import capability_enabled
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    monkeypatch.setenv("LIVE_ADVERTISING", canonical)
    assert capability_enabled() is False
