from __future__ import annotations

import json
import os
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.delivery_contract import MarketingCommand
from app.middleware_client import MAX_TOKEN_BYTES, MiddlewareDeliveryError, MiddlewareMarketingClient


@pytest.fixture
def payload():
    return {
        "operation_id": str(uuid4()), "campaign_id": str(uuid4()),
        "action": "activate", "expected_state": "approved", "expected_version": 1,
        "tenant_id": "tenant-one", "correlation_id": "correlation-one",
    }


@pytest.fixture
def configured(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("unit-test-token\n", encoding="ascii")
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", "https://middleware.example")
    monkeypatch.setenv("MIDDLEWARE_TOKEN_FILE", str(token))
    monkeypatch.delenv("MIDDLEWARE_TIMEOUT_SECONDS", raising=False)
    return token


@pytest.mark.parametrize("field,value", [
    ("provider_url", "https://unapproved.example"), ("access_token", "not-a-real-secret"),
    ("account_id", "unapproved-account"), ("options", {}),
    ("action", "publish"), ("expected_state", "active"),
    ("expected_state", "paused"), ("expected_version", True),
    ("expected_version", "1"), ("expected_version", 1.0),
    ("expected_version", 0), ("expected_version", 2147483648),
    ("operation_id", "not-a-uuid"), ("campaign_id", "not-a-uuid"),
    ("tenant_id", "tenant\nforged"), ("tenant_id", ""),
    ("correlation_id", "short"), ("correlation_id", "correlation\r\nInjected: value"),
])
def test_closed_command_schema_rejects_unsafe_or_coerced_input(payload, field, value):
    payload[field] = value
    with pytest.raises(ValidationError):
        MarketingCommand.model_validate(payload)


@pytest.mark.parametrize("action,state", [
    ("activate", "approved"), ("resume", "approved"),
    ("pause", "paused"), ("pause", "approved"),
])
def test_canonical_commands_round_trip(payload, action, state):
    payload.update(action=action, expected_state=state)
    assert MarketingCommand.model_validate(payload).as_payload() == payload


@pytest.mark.parametrize("url", [
    "https://middleware.example/prefix", "https://middleware.example?route=elsewhere",
    "https://middleware.example#fragment", "https://user:password@middleware.example",
    "https://middleware.example:invalid", "https://middleware.example:99999",
    "https://middleware.example:0", "https://[broken", "https://middleware.example\n",
    "https://middleware.example\\other", "http://localhost.attacker.example",
    "http://localhost@attacker.example", "http://192.0.2.1", "", "ftp://middleware.example",
])
def test_invalid_endpoint_is_a_nonretryable_error(configured, monkeypatch, url):
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", url)
    with pytest.raises(MiddlewareDeliveryError) as caught:
        MiddlewareMarketingClient()._endpoint()
    assert caught.value.code == "middleware_base_url_invalid"
    assert caught.value.retryable is False


@pytest.mark.parametrize("url", [
    "https://middleware.example", "https://middleware.example/",
    "http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000",
])
def test_valid_endpoint_is_rooted_at_the_canonical_route(configured, monkeypatch, url):
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", url)
    client = MiddlewareMarketingClient()
    assert client._endpoint() == url.rstrip("/") + "/api/v1/control/marketing/campaign-activations"
    assert client._endpoint("pause").endswith("/api/v1/control/marketing/campaign-transitions")
    with pytest.raises(MiddlewareDeliveryError, match="marketing_action_invalid"):
        client._endpoint("publish")


@pytest.mark.parametrize("value", ["invalid", "nan", "inf", "-inf", ""])
def test_timeout_validation_is_fail_closed(configured, monkeypatch, value):
    monkeypatch.setenv("MIDDLEWARE_TIMEOUT_SECONDS", value)
    with pytest.raises(MiddlewareDeliveryError, match="middleware_timeout_invalid"):
        MiddlewareMarketingClient()


@pytest.mark.parametrize("raw", [b"", b"a\nb", b"token\xff", b"x" * (MAX_TOKEN_BYTES + 1)])
def test_invalid_tokens_are_rejected(configured, raw):
    configured.write_bytes(raw)
    with pytest.raises(MiddlewareDeliveryError) as caught:
        MiddlewareMarketingClient()._token()
    assert not caught.value.retryable
    assert "token" in caught.value.code


def test_token_file_failure_symlink_and_fifo_are_controlled(configured, monkeypatch):
    client = MiddlewareMarketingClient()
    configured.unlink()
    with pytest.raises(MiddlewareDeliveryError, match="middleware_token_file_invalid"):
        client._token()
    target = configured.with_name("actual-token")
    target.write_text("unit-test-token", encoding="ascii")
    configured.symlink_to(target)
    with pytest.raises(MiddlewareDeliveryError, match="middleware_token_file_invalid"):
        client._token()
    configured.unlink()
    os.mkfifo(configured)
    with pytest.raises(MiddlewareDeliveryError, match="middleware_token_file_invalid"):
        client._token()


@pytest.mark.asyncio
async def test_delivery_preserves_idempotency_correlation_and_reads_rotated_token(configured, payload):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={"operation_id": "middleware-operation-1", "state": "accepted"})

    client = MiddlewareMarketingClient(transport=httpx.MockTransport(handler))
    first = await client.deliver(payload)
    configured.write_text("rotated-unit-test-token", encoding="ascii")
    second = await client.deliver(payload)
    assert first == second == {"operation_id": "middleware-operation-1", "state": "accepted"}
    assert len(calls) == 2
    for request in calls:
        assert request.headers["Idempotency-Key"] == payload["operation_id"]
        assert request.headers["X-Tenant-ID"] == payload["tenant_id"]
        assert request.headers["X-Correlation-ID"] == payload["correlation_id"]
        assert json.loads(request.content) == payload
    assert calls[0].headers["Authorization"] == "Bearer unit-test-token"
    assert calls[1].headers["Authorization"] == "Bearer rotated-unit-test-token"


@pytest.mark.asyncio
async def test_payload_validation_precedes_token_read_and_network(configured, payload):
    configured.unlink()
    calls = []
    client = MiddlewareMarketingClient(transport=httpx.MockTransport(lambda request: calls.append(request)))
    payload["access_token"] = "must-not-be-disclosed"
    with pytest.raises(MiddlewareDeliveryError, match="marketing_command_invalid") as caught:
        await client.deliver(payload)
    assert "must-not-be-disclosed" not in str(caught.value)
    assert not calls


@pytest.mark.parametrize("status,retryable", [
    (301, False), (307, False), (400, False), (401, False), (403, False),
    (404, False), (408, True), (409, False), (422, False), (429, True), (500, True), (503, True),
])
@pytest.mark.asyncio
async def test_http_errors_and_redirects_do_not_become_success(configured, payload, status, retryable):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://unapproved.example/"})

    client = MiddlewareMarketingClient(transport=httpx.MockTransport(handler))
    with pytest.raises(MiddlewareDeliveryError) as caught:
        await client.deliver(payload)
    assert caught.value.code == f"middleware_rejected_{status}"
    assert caught.value.retryable is retryable
    assert len(calls) == 1


@pytest.mark.parametrize("document", [
    None, [], "accepted", {}, {"operation_id": None, "state": "accepted"},
    {"operation_id": "", "state": "accepted"},
    {"operation_id": {}, "state": "accepted"},
    {"operation_id": "op", "state": []},
    {"operation_id": "op", "state": "failed"},
    {"operation_id": "op", "state": "denied"},
    {"operation_id": "op", "state": "active"},
    {"operation_id": "op", "state": "arbitrary"},
])
@pytest.mark.asyncio
async def test_invalid_receipts_are_unknown_outcomes_not_acceptance(configured, payload, document):
    transport = httpx.MockTransport(lambda request: httpx.Response(202, json=document))
    with pytest.raises(MiddlewareDeliveryError) as caught:
        await MiddlewareMarketingClient(transport=transport).deliver(payload)
    assert caught.value.code == "middleware_response_invalid"
    assert caught.value.retryable is True


@pytest.mark.parametrize("field", ["tenant_id", "campaign_id", "correlation_id"])
@pytest.mark.asyncio
async def test_receipt_echo_cannot_cross_request_boundaries(configured, payload, field):
    document = {"operation_id": "remote-op", "state": "accepted", field: "wrong-request"}
    transport = httpx.MockTransport(lambda request: httpx.Response(202, json=document))
    with pytest.raises(MiddlewareDeliveryError, match="middleware_response_mismatch"):
        await MiddlewareMarketingClient(transport=transport).deliver(payload)


@pytest.mark.asyncio
async def test_transport_and_non_json_failures_are_retryable_unknown_outcomes(configured, payload):
    def timeout(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with pytest.raises(MiddlewareDeliveryError, match="middleware_outcome_unknown") as caught:
        await MiddlewareMarketingClient(transport=httpx.MockTransport(timeout)).deliver(payload)
    assert caught.value.retryable
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="not JSON"))
    with pytest.raises(MiddlewareDeliveryError, match="middleware_response_invalid") as caught:
        await MiddlewareMarketingClient(transport=transport).deliver(payload)
    assert caught.value.retryable
