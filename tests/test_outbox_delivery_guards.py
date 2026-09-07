"""Unit regressions for dispatch decisions; database locking is covered by PostgreSQL CI."""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app import outbox_worker as worker
from app.delivery_contract import MarketingCommand, OPERATION_CONTRACTS
from app.middleware_client import MiddlewareDeliveryError
from app.models import CampaignModel, OperationModel, OutboxModel


@pytest.fixture
def rows(monkeypatch):
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    operation_id, campaign_id = uuid4(), uuid4()
    payload = {
        "operation_id": str(operation_id), "campaign_id": str(campaign_id),
        "action": "activate", "expected_state": "approved", "expected_version": 1,
        "tenant_id": "tenant-one", "correlation_id": "correlation-one",
    }
    operation = OperationModel(
        id=operation_id, aggregate_id=campaign_id, tenant_id="tenant-one",
        kind="campaign.activate", state="pending", correlation_id="correlation-one",
        attempts=0, error_code=None,
    )
    campaign = CampaignModel(id=campaign_id, tenant_id="tenant-one", state="approved", resource_version=1)
    outbox = OutboxModel(
        id=uuid4(), operation_id=operation_id, tenant_id="tenant-one", destination="middleware",
        event_type="marketing.campaign.activation_requested", payload_json=json.dumps(payload),
        state="pending", attempts=0,
    )
    return payload, operation, campaign, outbox


def sessions_for(rows, *, after_claim=True):
    _, operation, campaign, outbox = rows
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[outbox, operation, campaign, outbox, operation] if after_claim else [outbox, operation])
    session.get = AsyncMock(return_value=operation)
    session.scalars = AsyncMock(return_value=[])
    session.commit = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=context), session


def client_stub():
    client = MagicMock()
    client.deliver = AsyncMock(return_value={"operation_id": "middleware-operation", "state": "accepted"})
    return client


def claimed(rows):
    payload, operation, _, outbox = rows
    return worker.Claim(
        outbox.id, operation.id, payload, 1,
        outbox.tenant_id, outbox.destination, outbox.event_type,
    )


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "other-tenant"), ("operation_id", str(uuid4())),
    ("campaign_id", str(uuid4())), ("correlation_id", "other-correlation"),
    ("action", "resume"),
])
@pytest.mark.asyncio
async def test_payload_must_match_the_durable_operation(rows, field, value):
    payload, operation, _, outbox = rows
    payload[field] = value
    outbox.payload_json = json.dumps(payload)
    factory, _ = sessions_for(rows)
    client = client_stub()
    assert await worker.run_once(client, lease_seconds=30, max_attempts=3, session_factory=factory)
    client.deliver.assert_not_awaited()
    assert operation.state == "failed"
    assert outbox.state == "dead_letter"
    assert outbox.last_error_code == "marketing_command_binding_invalid"


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "other-tenant"), ("destination", "provider"), ("event_type", "unrecognized.event"),
])
@pytest.mark.asyncio
async def test_outbox_metadata_is_part_of_the_binding(rows, field, value):
    _, operation, _, outbox = rows
    setattr(outbox, field, value)
    factory, _ = sessions_for(rows)
    client = client_stub()
    await worker.run_once(client, lease_seconds=30, max_attempts=3, session_factory=factory)
    client.deliver.assert_not_awaited()
    assert operation.state == "failed"
    assert outbox.last_error_code == "marketing_command_binding_invalid"


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", '"not-an-object"'])
@pytest.mark.asyncio
async def test_poison_payload_is_dead_lettered_instead_of_crashing(rows, raw):
    _, operation, _, outbox = rows
    outbox.payload_json = raw
    factory, session = sessions_for(rows)
    # Invalid commands fail before looking up the campaign.
    session.scalar.side_effect = [outbox, outbox, operation]
    client = client_stub()
    assert await worker.run_once(client, lease_seconds=30, max_attempts=3, session_factory=factory)
    client.deliver.assert_not_awaited()
    assert operation.state == "failed"
    assert outbox.state == "dead_letter"
    assert outbox.lease_until is None
    assert outbox.last_error_code == "activation_payload_invalid"
    assert session.add.call_args.args[0].action == "campaign.invalid_command.delivery_failed"


@pytest.mark.asyncio
async def test_kill_switch_is_rechecked_after_claim_before_delivery(rows, monkeypatch):
    factory, _ = sessions_for(rows)
    monkeypatch.setattr(worker, "capability_enabled", MagicMock(side_effect=[True, False]))
    client = client_stub()
    await worker.run_once(client, lease_seconds=30, max_attempts=3, session_factory=factory)
    client.deliver.assert_not_awaited()
    assert rows[3].last_error_code == "live_advertising_disabled"


@pytest.mark.parametrize("state", ["superseded", "denied", "failed", "accepted", "reconciliation_required"])
@pytest.mark.asyncio
async def test_retired_operations_cannot_be_dispatched_or_resurrected(rows, state):
    _, operation, _, outbox = rows
    operation.state, operation.error_code = state, "prior-decision"
    factory, _ = sessions_for(rows)
    client = client_stub()
    await worker.run_once(client, lease_seconds=30, max_attempts=3, session_factory=factory)
    client.deliver.assert_not_awaited()
    assert operation.state == state
    assert operation.error_code == "prior-decision"
    assert outbox.state == "dead_letter"


@pytest.mark.parametrize("retryable", [True, False])
@pytest.mark.asyncio
async def test_late_failure_preserves_supersession_and_never_requeues(rows, retryable):
    _, operation, _, outbox = rows
    operation.state, operation.error_code = "superseded", "prior-decision"
    outbox.state, outbox.attempts = "processing", 1
    factory, _ = sessions_for(rows, after_claim=False)
    await worker.fail(
        claimed(rows), MiddlewareDeliveryError("late-error", retryable=retryable),
        8, session_factory=factory,
    )
    assert operation.state == "superseded"
    assert operation.error_code == "prior-decision"
    assert outbox.state == "dead_letter"
    assert outbox.lease_until is None


@pytest.mark.asyncio
async def test_late_success_preserves_supersession(rows):
    _, operation, _, outbox = rows
    operation.state = "superseded"
    outbox.state, outbox.attempts = "processing", 1
    factory, _ = sessions_for(rows, after_claim=False)
    await worker.complete(claimed(rows), {"operation_id": "remote", "state": "accepted"}, session_factory=factory)
    assert operation.state == "superseded"
    assert outbox.state == "published"


@pytest.mark.asyncio
async def test_older_attempt_cannot_complete_or_fail_newer_lease(rows):
    _, operation, _, outbox = rows
    outbox.state, outbox.attempts = "processing", 2
    for success in (True, False):
        factory, session = sessions_for(rows, after_claim=False)
        if success:
            await worker.complete(claimed(rows), {"state": "accepted"}, session_factory=factory)
        else:
            await worker.fail(claimed(rows), MiddlewareDeliveryError("old-error", retryable=False), 1, session_factory=factory)
        session.commit.assert_not_awaited()
        assert outbox.state == "processing"
        assert operation.state == "pending"


@pytest.mark.parametrize("kind", list(OPERATION_CONTRACTS))
@pytest.mark.asyncio
async def test_all_existing_lifecycle_commands_still_dispatch(rows, kind):
    payload, operation, campaign, outbox = rows
    action, expected_state, event_type = OPERATION_CONTRACTS[kind]
    operation.kind = kind
    outbox.event_type = event_type
    payload.update(action=action, expected_state=expected_state)
    outbox.payload_json = json.dumps(payload)
    campaign.state = "draft" if kind == "campaign.approval_invalidation_stop" else expected_state
    factory, session = sessions_for(rows)
    client = client_stub()
    assert await worker.run_once(client, lease_seconds=30, max_attempts=3, session_factory=factory)
    client.deliver.assert_awaited_once_with(payload)
    assert operation.state == "accepted"
    assert outbox.state == "published"


@pytest.mark.asyncio
async def test_stale_approval_still_blocks_delivery(rows):
    rows[2].resource_version = 2
    factory, _ = sessions_for(rows)
    client = client_stub()
    await worker.run_once(client, lease_seconds=30, max_attempts=3, session_factory=factory)
    client.deliver.assert_not_awaited()
    assert rows[3].last_error_code == "campaign_approval_stale"


def test_binding_cannot_substitute_another_claim_operation(rows):
    payload, operation, _, _ = rows
    claim = replace(claimed(rows), operation_id=uuid4())
    assert not worker._matches_operation(MarketingCommand.model_validate(payload), claim, operation)
