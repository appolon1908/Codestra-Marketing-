from __future__ import annotations

import json
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from app.marketing_policy import AccountPolicy
from app.providers.guarded_drafts import MetaDrafts, ProviderError, SandboxDrafts


@pytest.fixture
def configured(tmp_path, monkeypatch):
    path = tmp_path / "token"
    path.write_text("not-a-real-provider-token")
    monkeypatch.setenv("MARKETING_DRAFT_PROVIDER_WRITES_ENABLED", "true")
    return AccountPolicy(tenant_id="tenant", alias="draft", provider="meta", provider_account_id="123",
        mode="draft_only", currency="USD", max_daily_budget_minor=1000, max_total_budget_minor=10000,
        max_duration_days=30, max_start_delay_days=60, audiences=[uuid4()], creatives=[uuid4()],
        countries=["DO"], objectives=["OUTCOME_LEADS"], token_file=str(path), graph_version="v25.0")


@pytest.mark.asyncio
async def test_create_only_paused_shell_with_server_bound_account(configured):
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"account_id": "123", "currency": "USD", "account_status": 1})
        data = parse_qs(request.content.decode())
        assert data == {"name": ["codestra-draft-test"], "objective": ["OUTCOME_LEADS"],
                        "status": ["PAUSED"], "special_ad_categories": ["[]"]}
        return httpx.Response(200, json={"id": "999"})
    adapter = MetaDrafts(configured, transport=httpx.MockTransport(handler))
    assert await adapter.create("codestra-draft-test", {"objective": "OUTCOME_LEADS"}) == "999"
    for request in calls:
        assert request.url.host == "graph.facebook.com"
        assert "access_token" not in str(request.url)
        assert request.headers["Authorization"] == "Bearer not-a-real-provider-token"
    assert calls[-1].url.path == "/v25.0/act_123/campaigns"


@pytest.mark.asyncio
async def test_disabled_draft_writes_never_touch_network(configured, monkeypatch):
    monkeypatch.setenv("MARKETING_DRAFT_PROVIDER_WRITES_ENABLED", "false")
    def handler(request):
        pytest.fail("unexpected provider request")
    with pytest.raises(ProviderError, match="provider_draft_writes_disabled"):
        await MetaDrafts(configured, transport=httpx.MockTransport(handler)).create("marker", {"objective": "OUTCOME_LEADS"})


@pytest.mark.parametrize("action", ["activate", "resume", "publish", "delete", "arbitrary"])
@pytest.mark.asyncio
async def test_no_activation_or_publication_method(configured, action):
    with pytest.raises(ProviderError, match="provider_action_forbidden"):
        await MetaDrafts(configured).change("999", action)


@pytest.mark.parametrize("action,status", [("pause", "PAUSED"), ("archive", "ARCHIVED")])
@pytest.mark.asyncio
async def test_only_reducing_state_writes(configured, action, status):
    def handler(request):
        assert parse_qs(request.content.decode()) == {"status": [status]}
        return httpx.Response(200, json={"success": True})
    await MetaDrafts(configured, transport=httpx.MockTransport(handler)).change("999", action)


@pytest.mark.parametrize("spend,expected", [("0", 0), ("1.25", 125), (None, None)])
@pytest.mark.asyncio
async def test_spend_is_observed_or_explicitly_unknown(configured, spend, expected):
    def handler(request):
        if request.url.path.endswith("insights"):
            return httpx.Response(200, json={"data": [{"spend": spend}] if spend is not None else []})
        return httpx.Response(200, json={"id": "999", "account_id": "123", "configured_status": "PAUSED"})
    snapshot = await MetaDrafts(configured, transport=httpx.MockTransport(handler)).read("999", expected="PAUSED")
    assert snapshot.spend_minor == expected
    assert snapshot.evidence_kind == "provider_readback"


@pytest.mark.parametrize("spend", ["-1", "NaN", "Infinity", "0.001", 0, {}, "1e100"])
@pytest.mark.asyncio
async def test_invalid_spend_is_not_a_zero(configured, spend):
    def handler(request):
        document = {"data": [{"spend": spend}]} if request.url.path.endswith("insights") else {
            "id": "999", "account_id": "123", "configured_status": "PAUSED"}
        return httpx.Response(200, json=document)
    with pytest.raises(ProviderError, match="provider_spend_readback_invalid"):
        await MetaDrafts(configured, transport=httpx.MockTransport(handler)).read("999", expected="PAUSED")


@pytest.mark.parametrize("patch", [{"account_id": "other"}, {"id": "998"}, {"configured_status": "ACTIVE"}])
@pytest.mark.asyncio
async def test_readback_requires_bound_safe_campaign(configured, patch):
    document = {"id": "999", "account_id": "123", "configured_status": "PAUSED", **patch}
    with pytest.raises(ProviderError):
        await MetaDrafts(configured, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=document))).read("999", expected="PAUSED")


@pytest.mark.asyncio
async def test_ownership_check_does_not_prevent_emergency_pause_of_active_campaign(configured):
    document = {"id": "999", "account_id": "123", "configured_status": "ACTIVE"}
    await MetaDrafts(configured, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=document))).assert_ownership("999")


@pytest.mark.asyncio
async def test_reconciliation_never_follows_provider_next_url(configured):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == "graph.facebook.com"
        if len(calls) == 1:
            return httpx.Response(200, json={"data": [], "paging": {"next": "https://evil.example/", "cursors": {"after": "cursor"}}})
        assert request.url.params["after"] == "cursor"
        return httpx.Response(200, json={"data": [{"id": "999", "name": "marker"}]})
    assert await MetaDrafts(configured, transport=httpx.MockTransport(handler)).find("marker") == "999"


@pytest.mark.parametrize("document", [{"data": []}, {"data": [{"id": "998", "name": "m"}, {"id": "999", "name": "m"}]}])
@pytest.mark.asyncio
async def test_ambiguous_reconciliation_is_not_success(configured, document):
    with pytest.raises(ProviderError, match="provider_reconciliation_not_unique"):
        await MetaDrafts(configured, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=document))).find("m")


@pytest.mark.asyncio
async def test_redirect_does_not_forward_token(configured):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.example"})
    with pytest.raises(ProviderError, match="provider_http_307"):
        await MetaDrafts(configured, transport=httpx.MockTransport(handler)).connectivity()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_local_sandbox_can_never_be_mistaken_for_provider_evidence(configured):
    sandbox = configured.model_copy(update={"provider": "sandbox", "mode": "sandbox", "token_file": None, "graph_version": None})
    adapter = SandboxDrafts(sandbox)
    identifier = await adapter.create("codestra-draft-abc", {})
    snapshot = await adapter.read(identifier, expected="PAUSED")
    assert snapshot.evidence_kind == "local_simulation"
    assert (await adapter.connectivity())["status"] == "simulation_only"
