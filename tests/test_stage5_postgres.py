from __future__ import annotations

import os
import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.auth import Principal
from app.main import (
    ApprovalAction,
    AudienceCreate,
    AudienceUpdate,
    CampaignCreate,
    CampaignUpdate,
    CreativeCreate,
    CreativeUpdate,
    AttributionTouchCreate,
    create_attribution_touch,
    get_lead_attribution,
    performance,
    activate_campaign,
    approve_campaign,
    approve_creative,
    create_audience,
    create_campaign,
    create_creative,
    get_audience,
    get_creative,
    get_campaign,
    pause_campaign,
    resume_campaign,
    submit_for_approval,
    submit_creative_for_approval,
    update_audience,
    update_campaign,
    update_creative,
)
from app.models import AuditEventModel, CampaignModel, OperationModel, OutboxModel
from app.middleware_client import MiddlewareDeliveryError
from app.outbox_worker import Claim, claim_one, complete, run_once

pytestmark = pytest.mark.postgres


class _RecordingMiddleware:
    def __init__(self, error: MiddlewareDeliveryError | None = None):
        self.error = error
        self.payloads: list[dict[str, object]] = []

    async def deliver(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return {"operation_id": "middleware-operation-1", "state": "accepted"}


async def _seed_outbox(sessions, suffix: str) -> uuid.UUID:
    operation_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    tenant_id = f"tenant-worker-{suffix}-{uuid.uuid4()}"
    correlation_id = f"corr-{uuid.uuid4()}"
    payload = {
        "operation_id": str(operation_id),
        "campaign_id": str(campaign_id),
        "action": "activate",
        "expected_state": "approved",
        "expected_version": 1,
        "tenant_id": tenant_id,
        "correlation_id": correlation_id,
    }
    async with sessions() as session:
        session.add(
            CampaignModel(
                id=campaign_id,
                tenant_id=tenant_id,
                name="Worker campaign",
                objective="leads",
                daily_budget_minor=0,
                currency="USD",
                state="approved",
                idempotency_key=f"campaign-{suffix}-{uuid.uuid4()}",
                request_fingerprint="0" * 64,
                resource_version=1,
            )
        )
        session.add(
            OperationModel(
                id=operation_id,
                tenant_id=tenant_id,
                kind="campaign.activate",
                aggregate_id=campaign_id,
                state="pending",
                idempotency_key=f"worker-{suffix}-{uuid.uuid4()}",
                request_fingerprint="0" * 64,
                requested_by="test-operator",
                correlation_id=correlation_id,
            )
        )
        session.add(
            OutboxModel(
                tenant_id=tenant_id,
                operation_id=operation_id,
                destination="middleware",
                event_type="marketing.campaign.activation_requested",
                payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            )
        )
        await session.commit()
    return operation_id


@pytest.mark.asyncio
async def test_outbox_worker_delivers_once_and_records_audit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    operation_id = await _seed_outbox(sessions, "success")
    client = _RecordingMiddleware()

    assert await run_once(client, lease_seconds=30, max_attempts=3, session_factory=sessions) is True
    assert len(client.payloads) == 1
    async with sessions() as session:
        operation = await session.get(OperationModel, operation_id)
        outbox = await session.scalar(select(OutboxModel).where(OutboxModel.operation_id == operation_id))
        assert operation is not None and operation.state == "accepted"
        assert outbox is not None and outbox.state == "published" and outbox.attempts == 1
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.operation_id == operation_id,
                AuditEventModel.action == "campaign.activation.dispatched",
            )
        ) == 1
    assert await run_once(client, lease_seconds=30, max_attempts=3, session_factory=sessions) is False
    assert len(client.payloads) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_worker_retries_then_dead_letters_unknown_outcome(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    operation_id = await _seed_outbox(sessions, "failure")
    client = _RecordingMiddleware(MiddlewareDeliveryError("middleware_outcome_unknown", retryable=True))

    assert await run_once(client, lease_seconds=30, max_attempts=1, session_factory=sessions) is True
    async with sessions() as session:
        operation = await session.get(OperationModel, operation_id)
        outbox = await session.scalar(select(OutboxModel).where(OutboxModel.operation_id == operation_id))
        assert operation is not None and operation.state == "reconciliation_required"
        assert outbox is not None and outbox.state == "dead_letter" and outbox.attempts == 1
        assert outbox.last_error_code == "middleware_outcome_unknown"
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.operation_id == operation_id,
                AuditEventModel.action == "campaign.activation.delivery_failed",
                AuditEventModel.outcome == "dead_letter",
            )
        ) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_worker_rejects_stale_campaign_before_delivery(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    operation_id = await _seed_outbox(sessions, "stale")
    async with sessions() as session:
        operation = await session.get(OperationModel, operation_id)
        campaign = await session.get(CampaignModel, operation.aggregate_id)
        campaign.resource_version = 2
        campaign.state = "draft"
        await session.commit()
    client = _RecordingMiddleware()
    assert await run_once(client, lease_seconds=30, max_attempts=3, session_factory=sessions) is True
    assert client.payloads == []
    async with sessions() as session:
        operation = await session.get(OperationModel, operation_id)
        outbox = await session.scalar(select(OutboxModel).where(OutboxModel.operation_id == operation_id))
        assert operation.state == "failed"
        assert operation.attempts == 1
        assert outbox.state == "dead_letter"
        assert outbox.last_error_code == "campaign_approval_stale"
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_worker_claim_cannot_finalize_a_newer_lease(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    operation_id = await _seed_outbox(sessions, "fence")
    stale_claim = await claim_one(30, session_factory=sessions)
    assert stale_claim is not None and stale_claim.operation_id == operation_id
    async with sessions() as session:
        outbox = await session.scalar(
            select(OutboxModel).where(OutboxModel.operation_id == operation_id).with_for_update()
        )
        outbox.attempts += 1
        await session.commit()
    await complete(
        stale_claim,
        {"operation_id": "middleware-stale", "state": "accepted"},
        session_factory=sessions,
    )
    async with sessions() as session:
        operation = await session.get(OperationModel, operation_id)
        outbox = await session.scalar(select(OutboxModel).where(OutboxModel.operation_id == operation_id))
        assert operation.state == "pending"
        assert outbox.state == "processing"
        assert outbox.attempts == stale_claim.attempts + 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_idempotency_and_cross_tenant_reads_fail_closed():
    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a = f"tenant-a-{uuid.uuid4()}"
    tenant_b = f"tenant-b-{uuid.uuid4()}"
    key = f"campaign-{uuid.uuid4()}"
    body = CampaignCreate(name="Stage 5 campaign", objective="leads", daily_budget_minor=0)

    async with sessions() as session:
        first = await create_campaign(body, tenant_a, key, session)
        first_id = first.id
    async with sessions() as session:
        duplicate = await create_campaign(body, tenant_a, key, session)
        assert duplicate.id == first_id
    async with sessions() as session:
        with pytest.raises(HTTPException) as conflict:
            await create_campaign(
                CampaignCreate(name="Different campaign", objective="leads", daily_budget_minor=0),
                tenant_a,
                key,
                session,
            )
        assert conflict.value.status_code == 409
    async with sessions() as session:
        with pytest.raises(HTTPException) as denied:
            await get_campaign(first_id, tenant_b, session)
        assert denied.value.status_code == 404

    await engine.dispose()


@pytest.mark.asyncio
async def test_audience_and_creative_lifecycles_are_tenant_scoped_and_versioned():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-assets-{uuid.uuid4()}"
    other_tenant = f"tenant-other-{uuid.uuid4()}"
    writer = _request(tenant_id, "writer-1", {"marketing.write"})
    approver = _request(tenant_id, "approver-1", {"marketing.approve"})
    async with sessions() as session:
        audience = await create_audience(
            AudienceCreate(name="Audience", definition={"country": "DO"}),
            tenant_id, "audience-create", session,
        )
        creative = await create_creative(
            CreativeCreate(name="Creative", content={"headline": "Synthetic"}),
            tenant_id, "creative-create", session,
        )
    async with sessions() as session:
        audience = await update_audience(
            audience.id, AudienceUpdate(expected_version=audience.resource_version, name="Audience v2"),
            writer, tenant_id, "audience-update", session,
        )
        creative = await update_creative(
            creative.id, CreativeUpdate(expected_version=creative.resource_version, name="Creative v2"),
            writer, tenant_id, "creative-update", session,
        )
    async with sessions() as session:
        creative = await submit_creative_for_approval(
            creative.id,
            ApprovalAction(actor_id="writer-1", expected_version=creative.resource_version),
            writer, tenant_id, "creative-submit", session,
        )
    async with sessions() as session:
        creative = await approve_creative(
            creative.id,
            ApprovalAction(actor_id="approver-1", expected_version=creative.resource_version),
            approver, tenant_id, "creative-approve", session,
        )
        assert audience.name == "Audience v2"
        assert creative.approval_state == "approved"
    async with sessions() as session:
        with pytest.raises(HTTPException) as hidden_audience:
            await get_audience(audience.id, _request(other_tenant, "reader", {"marketing.read"}), other_tenant, session)
        assert hidden_audience.value.status_code == 404
        with pytest.raises(HTTPException) as hidden_creative:
            await get_creative(creative.id, _request(other_tenant, "reader", {"marketing.read"}), other_tenant, session)
        assert hidden_creative.value.status_code == 404

    await engine.dispose()


def _request(tenant_id: str, subject: str, scopes: set[str]) -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.correlation_id = f"corr-{uuid.uuid4()}"
    request.state.principal = Principal(subject, tenant_id, frozenset(scopes), "codestra-console")
    return request


@pytest.mark.asyncio
async def test_campaign_lifecycle_is_versioned_audited_and_idempotent():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-lifecycle-{uuid.uuid4()}"
    writer = _request(tenant_id, "writer-1", {"marketing.write"})
    approver = _request(tenant_id, "approver-1", {"marketing.approve"})
    async with sessions() as session:
        created = await create_campaign(
            CampaignCreate(name="Lifecycle", objective="leads", daily_budget_minor=100),
            tenant_id,
            f"create-{uuid.uuid4()}",
            session,
        )
        campaign_id = created.id
        version = created.resource_version
    async with sessions() as session:
        updated = await update_campaign(
            campaign_id,
            CampaignUpdate(expected_version=version, daily_budget_minor=200),
            writer,
            tenant_id,
            "update-key",
            session,
        )
    async with sessions() as session:
        submitted = await submit_for_approval(
            campaign_id,
            ApprovalAction(actor_id="writer-1", expected_version=updated.resource_version),
            writer,
            tenant_id,
            "submit-key",
            session,
        )
    async with sessions() as session:
        approved = await approve_campaign(
            campaign_id,
            ApprovalAction(actor_id="approver-1", expected_version=submitted.resource_version),
            approver,
            tenant_id,
            "approve-key",
            session,
        )
    async with sessions() as session:
        paused = await pause_campaign(
            campaign_id,
            ApprovalAction(actor_id="writer-1", expected_version=approved.resource_version),
            writer,
            tenant_id,
            "pause-key",
            session,
        )
    async with sessions() as session:
        resumed = await resume_campaign(
            campaign_id,
            ApprovalAction(actor_id="writer-1", expected_version=paused.resource_version),
            writer,
            tenant_id,
            "resume-key",
            session,
        )
        assert resumed.state == "approved"
    async with sessions() as session:
        replay = await resume_campaign(
            campaign_id,
            ApprovalAction(actor_id="writer-1", expected_version=paused.resource_version),
            writer,
            tenant_id,
            "resume-key",
            session,
        )
        assert replay.resource_version == resumed.resource_version
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.tenant_id == tenant_id,
                AuditEventModel.aggregate_id == campaign_id,
            )
        ) == 5

    await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_activation_is_durable_idempotent_and_has_no_outbox():
    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-activation-{uuid.uuid4()}"
    key = f"activate-{uuid.uuid4()}"

    async with sessions() as session:
        campaign = await create_campaign(
            CampaignCreate(name="Denied activation", objective="leads", daily_budget_minor=0),
            tenant_id,
            f"create-{uuid.uuid4()}",
            session,
        )
        campaign.state = "approved"
        await session.commit()
        campaign_id = campaign.id

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.correlation_id = f"corr-{uuid.uuid4()}"
    request.state.principal = Principal(
        subject="marketing-operator",
        tenant_id=tenant_id,
        scopes=frozenset({"marketing.provider.write"}),
        client_id="codestra-console",
    )
    async with sessions() as session:
        first = await activate_campaign(campaign_id, request, tenant_id, key, session)
        assert first.status_code == 423
    async with sessions() as session:
        duplicate = await activate_campaign(campaign_id, request, tenant_id, key, session)
        assert duplicate.status_code == 423
    async with sessions() as session:
        operation = (
            await session.execute(
                select(OperationModel).where(
                    OperationModel.tenant_id == tenant_id,
                    OperationModel.idempotency_key == key,
                )
            )
        ).scalar_one()
        assert operation.state == "denied"
        assert operation.error_code == "live_advertising_disabled"
        assert await session.scalar(
            select(func.count()).select_from(OutboxModel).where(OutboxModel.operation_id == operation.id)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(AuditEventModel.operation_id == operation.id)
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(CampaignModel).where(CampaignModel.id == campaign_id)
        ) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_live_campaign_allows_only_one_activation_operation(monkeypatch):
    monkeypatch.setattr("app.main.LIVE_ADVERTISING_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-single-activation-{uuid.uuid4()}"
    request = _request(tenant_id, "marketing-operator", {"marketing.provider.write"})
    async with sessions() as session:
        campaign = await create_campaign(
            CampaignCreate(name="Single activation", objective="leads", daily_budget_minor=100),
            tenant_id, f"create-{uuid.uuid4()}", session,
        )
        campaign.state = "approved"
        await session.commit()
        campaign_id = campaign.id
    async with sessions() as session:
        first = await activate_campaign(campaign_id, request, tenant_id, f"activate-{uuid.uuid4()}", session)
        assert first.state == "pending"
    async with sessions() as session:
        with pytest.raises(HTTPException, match="campaign_activation_already_exists") as duplicate:
            await activate_campaign(campaign_id, request, tenant_id, f"activate-{uuid.uuid4()}", session)
        assert duplicate.value.status_code == 409
    await engine.dispose()


@pytest.mark.asyncio
async def test_material_edit_queues_stop_for_accepted_activation(monkeypatch):
    monkeypatch.setattr("app.main.LIVE_ADVERTISING_ENABLED", True)
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-stop-on-edit-{uuid.uuid4()}"
    writer = _request(tenant_id, "writer", {"marketing.write"})
    async with sessions() as session:
        campaign = await create_campaign(
            CampaignCreate(name="Live campaign", objective="leads", daily_budget_minor=100),
            tenant_id, f"create-{uuid.uuid4()}", session,
        )
        campaign.state = "approved"
        activation = OperationModel(
            tenant_id=tenant_id, kind="campaign.activate", aggregate_id=campaign.id,
            state="accepted", idempotency_key=f"activate-{uuid.uuid4()}",
            request_fingerprint="0" * 64, requested_by="operator",
            correlation_id=f"corr-{uuid.uuid4()}", result_json="{}",
        )
        session.add(activation)
        await session.flush()
        session.add(
            OutboxModel(
                tenant_id=tenant_id, operation_id=activation.id, destination="middleware",
                event_type="marketing.campaign.activation_requested",
                payload_json=json.dumps(
                    {
                        "operation_id": str(activation.id), "campaign_id": str(campaign.id),
                        "action": "activate", "expected_state": "approved",
                        "expected_version": campaign.resource_version, "tenant_id": tenant_id,
                        "correlation_id": activation.correlation_id,
                    }
                ),
                state="published",
            )
        )
        await session.flush()
        await session.commit()
        campaign_id = campaign.id
        activation_id = activation.id
        version = campaign.resource_version
    async with sessions() as session:
        updated = await update_campaign(
            campaign_id, CampaignUpdate(expected_version=version, daily_budget_minor=200),
            writer, tenant_id, f"update-{uuid.uuid4()}", session,
        )
        assert updated.state == "draft"
        stop = await session.scalar(
            select(OperationModel).where(
                OperationModel.tenant_id == tenant_id,
                OperationModel.kind == "campaign.approval_invalidation_stop",
            )
        )
        assert stop is not None and stop.state == "pending"
        outbox = await session.scalar(select(OutboxModel).where(OutboxModel.operation_id == stop.id))
        assert outbox is not None
        payload = json.loads(outbox.payload_json)
        assert payload["action"] == "pause"
        assert payload["expected_state"] == "approved"
        assert "reason" not in payload
        await session.commit()
        stop_id = stop.id
    client = _RecordingMiddleware()
    for _ in range(50):
        if payload in client.payloads:
            break
        assert await run_once(client, lease_seconds=30, max_attempts=3, session_factory=sessions) is True
    assert payload in client.payloads
    async with sessions() as session:
        activation = await session.scalar(
            select(OperationModel).where(
                OperationModel.tenant_id == tenant_id,
                OperationModel.kind == "campaign.activate",
            )
        )
        assert activation.state == "superseded"
        stop = await session.get(OperationModel, stop_id)
        assert stop is not None and stop.state == "accepted"

        # Model an activation response arriving after the invalidation stop.
        # Its lease is otherwise valid, but it must not resurrect the operation.
        activation_outbox = await session.scalar(
            select(OutboxModel).where(OutboxModel.operation_id == activation_id).with_for_update()
        )
        assert activation_outbox is not None
        activation_outbox.state = "processing"
        activation_outbox.attempts = 1
        activation_payload = json.loads(activation_outbox.payload_json)
        activation_outbox_id = activation_outbox.id
        await session.commit()
    await complete(
        Claim(activation_outbox_id, activation_id, activation_payload, 1),
        {"operation_id": str(activation_id), "state": "accepted"},
        session_factory=sessions,
    )
    async with sessions() as session:
        activation = await session.get(OperationModel, activation_id)
        assert activation is not None and activation.state == "superseded"
    await engine.dispose()


@pytest.mark.asyncio
async def test_pause_propagates_while_activation_completion_is_pending(monkeypatch):
    monkeypatch.setattr("app.main.LIVE_ADVERTISING_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-pause-pending-{uuid.uuid4()}"
    writer = _request(tenant_id, "writer", {"marketing.write"})
    async with sessions() as session:
        campaign = await create_campaign(
            CampaignCreate(name="Pending activation", objective="leads", daily_budget_minor=100),
            tenant_id, f"create-{uuid.uuid4()}", session,
        )
        campaign.state = "approved"
        session.add(
            OperationModel(
                tenant_id=tenant_id, kind="campaign.activate", aggregate_id=campaign.id,
                state="pending", idempotency_key=f"activate-{uuid.uuid4()}",
                request_fingerprint="0" * 64, requested_by="operator",
                correlation_id=f"corr-{uuid.uuid4()}", result_json="{}",
            )
        )
        await session.commit()
        campaign_id = campaign.id
        version = campaign.resource_version
    async with sessions() as session:
        paused = await pause_campaign(
            campaign_id, ApprovalAction(actor_id="writer", expected_version=version),
            writer, tenant_id, f"pause-{uuid.uuid4()}", session,
        )
        assert paused.state == "paused"
        pause_operation = await session.scalar(
            select(OperationModel).where(
                OperationModel.tenant_id == tenant_id,
                OperationModel.kind == "campaign.pause",
            )
        )
        assert pause_operation is not None and pause_operation.state == "pending"
        assert await session.scalar(
            select(func.count()).select_from(OutboxModel).where(
                OutboxModel.operation_id == pause_operation.id
            )
        ) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_attribution_is_tenant_scoped_idempotent_and_reporting_is_bounded():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-attribution-{uuid.uuid4()}"
    request = _request(tenant_id, "attribution-writer", {"marketing.attribution.write"})
    request.state.correlation_id = f"corr-{uuid.uuid4()}"
    body = AttributionTouchCreate(
        event_id=f"event-{uuid.uuid4()}",
        lead_id="lead-synthetic-1",
        channel="paid_search",
        occurred_at=datetime.now().astimezone(),
        metadata={"source": "synthetic"},
    )
    key = f"attribution-{uuid.uuid4()}"
    async with sessions() as session:
        first = await create_attribution_touch(body, request, tenant_id, key, session)
    async with sessions() as session:
        duplicate = await create_attribution_touch(body, request, tenant_id, key, session)
        assert duplicate.id == first.id
    reader = _request(tenant_id, "attribution-reader", {"marketing.attribution.read", "marketing.performance.read"})
    async with sessions() as session:
        rows = await get_lead_attribution("lead-synthetic-1", reader, tenant_id, 50, session)
        assert [row.id for row in rows] == [first.id]
        now = datetime.now().astimezone()
        report = await performance(reader, tenant_id, now - timedelta(days=1), now + timedelta(seconds=1), session)
        assert report["items"][0]["touch_count"] == 1
        with pytest.raises(HTTPException, match="reporting_window_too_large"):
            await performance(reader, tenant_id, now - timedelta(days=94), now, session)
    other_tenant = f"tenant-attribution-other-{uuid.uuid4()}"
    async with sessions() as session:
        campaign = await create_campaign(
            CampaignCreate(name="Other tenant campaign", objective="leads", daily_budget_minor=0),
            other_tenant, f"create-{uuid.uuid4()}", session,
        )
    invalid = body.model_copy(update={"event_id": f"event-{uuid.uuid4()}", "campaign_id": campaign.id})
    async with sessions() as session:
        with pytest.raises(HTTPException, match="campaign_not_found") as hidden:
            await create_attribution_touch(invalid, request, tenant_id, f"attribution-{uuid.uuid4()}", session)
        assert hidden.value.status_code == 404
    await engine.dispose()
