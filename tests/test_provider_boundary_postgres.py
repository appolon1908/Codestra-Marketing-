"""Real PostgreSQL serialization and crash-reconciliation regressions, no provider I/O."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import Principal
from app.marketing_policy import AccountPolicy, DraftSpec, PolicyDocument, PolicyError, ProviderCommand
from app.models import AudienceModel, AuditEventModel, CampaignModel, CreativeModel
from app.provider_models import ProviderCampaignModel, ProviderCommandModel
from app.provider_service import run_once, submit
from app.providers.guarded_drafts import ProviderError, SandboxDrafts

pytestmark = pytest.mark.postgres


@pytest.fixture
async def db_case(monkeypatch, tmp_path):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "test-" + uuid4().hex
    campaign, audience, creative = uuid4(), uuid4(), uuid4()
    principal = Principal("preparer", tenant, frozenset({"marketing.provider.command", "marketing.provider.read"}), "middleware")
    approver = Principal("approver", tenant, frozenset({"marketing.provider.command", "marketing.provider.approve"}), "middleware")
    policy = AccountPolicy(tenant_id=tenant, alias="sandbox", provider="sandbox", provider_account_id="123",
        mode="sandbox", currency="USD", max_daily_budget_minor=10000, max_total_budget_minor=100000,
        max_duration_days=30, max_start_delay_days=60, audiences=[audience], creatives=[creative],
        countries=["DO"], objectives=["OUTCOME_LEADS"])
    path = tmp_path / "policy.json"
    path.write_text(PolicyDocument(schema_version=1, accounts=[policy]).model_dump_json())
    monkeypatch.setenv("MARKETING_ACCOUNT_POLICY_FILE", str(path))
    async with sessions() as session:
        session.add(CampaignModel(id=campaign, tenant_id=tenant, name="No spend", objective="OUTCOME_LEADS",
            daily_budget_minor=1000, currency="USD", state="draft", resource_version=1,
            idempotency_key=uuid4().hex, request_fingerprint="0" * 64))
        session.add(AudienceModel(id=audience, tenant_id=tenant, name="Allowed", resource_version=1,
            definition_json='{"countries":["DO"],"age_min":21,"age_max":65}',
            idempotency_key=uuid4().hex, request_fingerprint="0" * 64))
        session.add(CreativeModel(id=creative, tenant_id=tenant, name="Approved", resource_version=1,
            content_json='{"headline":"Test"}', approval_state="approved",
            idempotency_key=uuid4().hex, request_fingerprint="0" * 64))
        await session.commit()
    now = datetime.now(timezone.utc)
    draft = ProviderCommand(campaign_id=campaign, action="create_draft", expected_version=0,
        draft=DraftSpec(account="sandbox", source_version=1, audience_id=audience, creative_id=creative,
                        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=3)))
    yield sessions, draft, principal, approver, policy, path
    async with sessions() as session:
        for model in (ProviderCommandModel, ProviderCampaignModel, AuditEventModel, CampaignModel, AudienceModel, CreativeModel):
            await session.execute(delete(model).where(model.tenant_id == tenant))
        await session.commit()
    await engine.dispose()


async def send(case, command=None, *, key=None, principal=None):
    sessions, draft, preparer, *_ = case
    return await submit(command or draft, principal or preparer, key or uuid4().hex, "correlation-test", session_factory=sessions)


async def plan_for(case):
    async with case[0]() as session:
        return await session.get(ProviderCampaignModel, case[1].campaign_id)


async def transition(case, action, principal=None):
    plan = await plan_for(case)
    command = ProviderCommand(campaign_id=plan.campaign_id, action=action, expected_version=plan.resource_version)
    return await send(case, command, principal=principal)


@pytest.mark.asyncio
async def test_first_create_replay_conflict_and_lifecycle(db_case):
    first = await send(db_case, key="first-create-key")
    assert await send(db_case, key="first-create-key") == first
    changed = db_case[1].model_copy(update={"draft": db_case[1].draft.model_copy(update={"account": "other"})})
    with pytest.raises(PolicyError, match="idempotency_conflict"):
        await send(db_case, changed, key="first-create-key")
    assert await run_once(session_factory=db_case[0])
    plan = await plan_for(db_case)
    assert plan.state == "draft" and plan.resource_version == 2
    assert json.loads(plan.snapshot_json)["spend_minor"] == 0
    with pytest.raises(Exception, match="required_scope_missing"):
        await transition(db_case, "approve")
    await transition(db_case, "approve", db_case[3])
    await transition(db_case, "schedule")
    assert (await plan_for(db_case)).state == "scheduled"
    with pytest.raises(PolicyError, match="activation_not_authorized"):
        await transition(db_case, "activate")
    await transition(db_case, "pause")
    await run_once(session_factory=db_case[0])
    assert (await plan_for(db_case)).state == "paused"
    await transition(db_case, "archive")
    await run_once(session_factory=db_case[0])
    plan = await plan_for(db_case)
    assert plan.state == "archived"
    assert json.loads(plan.snapshot_json)["configured_status"] == "ARCHIVED"
    # Original accepted receipt remains identical after all subsequent changes.
    assert await send(db_case, key="first-create-key") == first


@pytest.mark.asyncio
async def test_concurrent_identical_create_has_one_operation(db_case):
    receipts = await asyncio.gather(*(send(db_case, key="concurrent-key") for _ in range(4)))
    assert all(item == receipts[0] for item in receipts)
    async with db_case[0]() as session:
        count = await session.scalar(select(func.count()).select_from(ProviderCommandModel).where(
            ProviderCommandModel.tenant_id == db_case[2].tenant_id))
        assert count == 1


@pytest.mark.asyncio
async def test_wrong_tenant_account_and_stale_version_are_denied(db_case):
    wrong = Principal("preparer", "other", db_case[2].scopes, "middleware")
    with pytest.raises(PolicyError, match="campaign_not_found"):
        await send(db_case, principal=wrong)
    command = db_case[1].model_copy(update={"draft": db_case[1].draft.model_copy(update={"account": "unknown"})})
    with pytest.raises(PolicyError, match="account_not_authorized"):
        await send(db_case, command)
    await send(db_case)
    await run_once(session_factory=db_case[0])
    with pytest.raises(PolicyError, match="stale_resource_version"):
        await send(db_case, ProviderCommand(campaign_id=db_case[1].campaign_id, action="pause", expected_version=1))


@pytest.mark.asyncio
async def test_separation_of_duties_and_source_changes_block_approval(db_case):
    await send(db_case)
    await run_once(session_factory=db_case[0])
    same_actor = Principal("preparer", db_case[2].tenant_id, db_case[3].scopes, "middleware")
    with pytest.raises(PolicyError, match="separation_of_duties"):
        await transition(db_case, "approve", same_actor)
    async with db_case[0]() as session:
        source = await session.get(CampaignModel, db_case[1].campaign_id)
        source.resource_version += 1
        await session.commit()
    with pytest.raises(PolicyError, match="source_version_stale"):
        await transition(db_case, "approve", db_case[3])
    # Safety-reducing actions are not blocked by stale source content.
    await transition(db_case, "pause")
    await run_once(session_factory=db_case[0])
    assert (await plan_for(db_case)).state == "paused"


@pytest.mark.asyncio
async def test_worker_rechecks_policy_and_input_versions(db_case):
    receipt = await send(db_case)
    async with db_case[0]() as session:
        source = await session.get(CampaignModel, db_case[1].campaign_id)
        source.resource_version += 1
        await session.commit()
    class MustNotSend(SandboxDrafts):
        async def create(self, *args):
            pytest.fail("stale preparation reached provider")
    await run_once(session_factory=db_case[0], driver_factory=MustNotSend)
    async with db_case[0]() as session:
        operation = await session.get(ProviderCommandModel, UUID(receipt["operation_id"]))
        assert operation.state == "reconciliation_required"
        assert operation.error_code == "source_version_stale"


@pytest.mark.asyncio
async def test_unknown_creation_outcome_is_not_retried_and_readback_reconciles(db_case):
    receipt = await send(db_case)
    calls = []
    class TimeoutAfterCreation(SandboxDrafts):
        async def create(self, *args):
            calls.append("create")
            raise ProviderError("provider_outcome_unknown")
    await run_once(session_factory=db_case[0], driver_factory=TimeoutAfterCreation)
    assert not await run_once(session_factory=db_case[0], driver_factory=TimeoutAfterCreation)
    assert calls == ["create"]
    await transition(db_case, "readback")
    await run_once(session_factory=db_case[0])
    assert (await plan_for(db_case)).pending_command_id is None
    async with db_case[0]() as session:
        operation = await session.get(ProviderCommandModel, UUID(receipt["operation_id"]))
        assert operation.state == "reconciled"


@pytest.mark.asyncio
async def test_expired_running_command_never_repeats_provider_write(db_case):
    receipt = await send(db_case)
    async with db_case[0]() as session:
        operation = await session.get(ProviderCommandModel, UUID(receipt["operation_id"]))
        operation.state = "running"
        operation.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
    await run_once(session_factory=db_case[0], driver_factory=lambda policy: pytest.fail("must not dispatch"))
    async with db_case[0]() as session:
        operation = await session.get(ProviderCommandModel, UUID(receipt["operation_id"]))
        assert operation.state == "reconciliation_required"
        assert operation.error_code == "provider_worker_lease_expired"


@pytest.mark.asyncio
async def test_provider_identifier_survives_readback_failure(db_case):
    await send(db_case)
    class ReadFailure(SandboxDrafts):
        async def read(self, *args, **kwargs):
            raise ProviderError("provider_outcome_unknown")
    await run_once(session_factory=db_case[0], driver_factory=ReadFailure)
    plan = await plan_for(db_case)
    assert plan.provider_id.startswith("sandbox-")
    assert plan.snapshot_json is None
    assert not await run_once(session_factory=db_case[0], driver_factory=ReadFailure)


@pytest.mark.asyncio
async def test_account_binding_change_cannot_redirect_pending_write(db_case):
    receipt = await send(db_case)
    policy = db_case[4].model_copy(update={"provider_account_id": "999"})
    db_case[5].write_text(PolicyDocument(schema_version=1, accounts=[policy]).model_dump_json())
    await run_once(session_factory=db_case[0], driver_factory=lambda policy: pytest.fail("must not dispatch"))
    async with db_case[0]() as session:
        operation = await session.get(ProviderCommandModel, UUID(receipt["operation_id"]))
        assert operation.error_code == "provider_account_binding_changed"


@pytest.mark.asyncio
async def test_account_wide_budget_reservation_serializes_distinct_campaigns(db_case):
    policy = db_case[4].model_copy(update={"max_daily_budget_minor": 1500})
    db_case[5].write_text(PolicyDocument(schema_version=1, accounts=[policy]).model_dump_json())
    other_id = uuid4()
    async with db_case[0]() as session:
        session.add(CampaignModel(id=other_id, tenant_id=policy.tenant_id, name="Second", objective="OUTCOME_LEADS",
            daily_budget_minor=1000, currency="USD", state="draft", resource_version=1,
            idempotency_key=uuid4().hex, request_fingerprint="0" * 64))
        await session.commit()
    other = db_case[1].model_copy(update={"campaign_id": other_id})
    results = await asyncio.gather(send(db_case), send(db_case, other), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    failure = next(result for result in results if isinstance(result, Exception))
    assert isinstance(failure, PolicyError) and failure.code == "account_budget_reservation_exceeded"


@pytest.mark.asyncio
async def test_private_http_creation_replay_and_tenant_readback(db_case, monkeypatch):
    import httpx
    from app.asgi import app
    from app.auth import authenticate
    from app.provider_api import get_session_factory
    monkeypatch.setenv("MARKETING_COMMAND_CLIENT_IDS", "middleware")
    app.dependency_overrides[authenticate] = lambda: db_case[2]
    app.dependency_overrides[get_session_factory] = lambda: db_case[0]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Idempotency-Key": "http-create-key", "X-Correlation-ID": "http-correlation"}
            first = await client.post("/internal/v1/marketing/commands", json=db_case[1].model_dump(mode="json"), headers=headers)
            replay = await client.post("/internal/v1/marketing/commands", json=db_case[1].model_dump(mode="json"), headers=headers)
            assert first.status_code == replay.status_code == 202
            assert first.json() == replay.json()
            await run_once(session_factory=db_case[0])
            path = f"/internal/v1/marketing/campaigns/{db_case[1].campaign_id}"
            current = await client.get(path)
            assert current.status_code == 200
            assert current.json()["provider_snapshot"]["evidence_kind"] == "local_simulation"
            assert current.json()["provider_snapshot"]["spend_minor"] == 0
            other = Principal("user", "other-tenant", db_case[2].scopes, "middleware")
            app.dependency_overrides[authenticate] = lambda: other
            assert (await client.get(path)).status_code == 404
            assert (await client.get(first.json()["status_url"])).status_code == 404
    finally:
        app.dependency_overrides.clear()
