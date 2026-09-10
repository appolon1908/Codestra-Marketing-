"""Transactional no-spend command handling and an at-most-once provider worker."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .auth import Principal
from .db import SessionLocal
from .marketing_policy import (
    DraftSpec, PolicyError, ProviderCommand, binding_hash, canonical_json, digest,
    load_policy, next_state, validate_draft,
)
from .models import AudienceModel, AuditEventModel, CampaignModel, CreativeModel
from .provider_models import ProviderCampaignModel, ProviderCommandModel
from .providers.guarded_drafts import ProviderError, driver


def audit(session, operation, outcome: str, code: str | None = None) -> None:
    session.add(AuditEventModel(
        tenant_id=operation.tenant_id, operation_id=operation.id,
        aggregate_type="provider_campaign", aggregate_id=operation.campaign_id,
        action=f"provider_campaign.{operation.action}", outcome=outcome,
        actor_id=operation.requested_by, correlation_id=operation.correlation_id,
        detail_json=canonical_json({"error_code": code, "live_activation_executed": False}),
    ))


async def campaign_for(session, campaign_id: UUID, tenant_id: str):
    row = await session.scalar(select(CampaignModel).where(
        CampaignModel.id == campaign_id, CampaignModel.tenant_id == tenant_id,
    ).with_for_update())
    if row is None:
        raise PolicyError("campaign_not_found", 404)
    return row


async def assets(session, policy, spec, source):
    audience = await session.scalar(select(AudienceModel).where(
        AudienceModel.id == spec.audience_id, AudienceModel.tenant_id == policy.tenant_id,
    ).with_for_update())
    creative = await session.scalar(select(CreativeModel).where(
        CreativeModel.id == spec.creative_id, CreativeModel.tenant_id == policy.tenant_id,
    ).with_for_update())
    return validate_draft(policy, spec, source, audience, creative)


async def verify_prepared(session, policy, plan, source):
    saved = json.loads(plan.prepared_json)
    spec = DraftSpec(
        account=plan.account_alias, source_version=saved["source_version"],
        audience_id=saved["audience_id"], creative_id=saved["creative_id"],
        starts_at=saved["starts_at"], ends_at=saved["ends_at"],
    )
    current = await assets(session, policy, spec, source)
    if current != saved:
        raise PolicyError("prepared_inputs_changed", 409)
    return saved


async def reserve_budget(session, policy, prepared) -> None:
    # Serialize reservations across different campaigns on the same account.
    lock_key = int(digest([policy.tenant_id, policy.alias])[:16], 16)
    if lock_key >= 2**63:
        lock_key -= 2**64
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    plans = await session.scalars(select(ProviderCampaignModel).where(
        ProviderCampaignModel.tenant_id == policy.tenant_id,
        ProviderCampaignModel.account_alias == policy.alias,
        ProviderCampaignModel.state != "archived",
    ))
    daily, total = prepared["daily_budget_minor"], prepared["reserved_total_minor"]
    for plan in plans:
        saved = json.loads(plan.prepared_json)
        daily += saved["daily_budget_minor"]
        total += saved["reserved_total_minor"]
    if daily > policy.max_daily_budget_minor or total > policy.max_total_budget_minor:
        raise PolicyError("account_budget_reservation_exceeded", 403)


async def _existing(session, tenant, key, fingerprint):
    row = await session.scalar(select(ProviderCommandModel).where(
        ProviderCommandModel.tenant_id == tenant, ProviderCommandModel.idempotency_key == key,
    ))
    if row is not None and row.fingerprint != fingerprint:
        raise PolicyError("idempotency_conflict", 409)
    return row


async def submit(command: ProviderCommand, principal: Principal, key: str, correlation: str,
                 *, session_factory=SessionLocal) -> dict:
    principal.require("marketing.provider.command")
    if command.action == "approve":
        principal.require("marketing.provider.approve")
    fingerprint = digest({"tenant": principal.tenant_id, "actor": principal.subject,
                          "command": command.model_dump(mode="json")})
    try:
        async with session_factory() as session:
            previous = await _existing(session, principal.tenant_id, key, fingerprint)
            if previous is not None:
                return json.loads(previous.receipt_json)
            source = await campaign_for(session, command.campaign_id, principal.tenant_id)
            # Recheck after serializing concurrent requests for this campaign.
            previous = await _existing(session, principal.tenant_id, key, fingerprint)
            if previous is not None:
                return json.loads(previous.receipt_json)
            plan = await session.scalar(select(ProviderCampaignModel).where(
                ProviderCampaignModel.campaign_id == command.campaign_id,
                ProviderCampaignModel.tenant_id == principal.tenant_id,
            ).with_for_update())
            if command.action == "activate":
                raise PolicyError("live_advertising_activation_not_authorized", 423)
            operation_id = uuid4()
            if command.action == "create_draft":
                if plan is not None:
                    raise PolicyError("provider_campaign_already_exists", 409)
                spec = command.draft
                policy = load_policy().account(principal.tenant_id, spec.account)
                if policy.provider == "meta" and os.getenv("MARKETING_DRAFT_PROVIDER_WRITES_ENABLED", "false") != "true":
                    raise PolicyError("provider_draft_writes_disabled", 423)
                prepared = await assets(session, policy, spec, source)
                await reserve_budget(session, policy, prepared)
                plan = ProviderCampaignModel(
                    campaign_id=command.campaign_id, tenant_id=principal.tenant_id,
                    account_alias=spec.account, binding_hash=binding_hash(policy),
                    prepared_json=canonical_json(prepared), marker=f"codestra-draft-{operation_id.hex}",
                    state="draft", resource_version=1, prepared_by=principal.subject,
                )
                session.add(plan)
                target = "draft"
            else:
                if plan is None:
                    raise PolicyError("provider_campaign_not_found", 404)
                if command.expected_version != plan.resource_version:
                    raise PolicyError("stale_resource_version", 409)
                policy = load_policy().account(principal.tenant_id, plan.account_alias)
                if binding_hash(policy) != plan.binding_hash:
                    raise PolicyError("provider_account_binding_changed", 409)
                pending = None
                if plan.pending_command_id is not None:
                    pending = await session.get(ProviderCommandModel, plan.pending_command_id)
                    if pending is None or pending.state in {"pending", "running"}:
                        raise PolicyError("provider_command_in_progress", 409)
                    if command.action not in {"readback", "pause", "archive"}:
                        raise PolicyError("provider_reconciliation_required", 409)
                target = next_state(plan.state, command.action)
                if command.action == "readback" and pending is not None and pending.state == "reconciliation_required":
                    target = pending.target_state
                if command.action in {"approve", "schedule"}:
                    await verify_prepared(session, policy, plan, source)
                    if plan.snapshot_json is None:
                        raise PolicyError("provider_readback_required", 409)
                    snapshot = json.loads(plan.snapshot_json)
                    if snapshot.get("configured_status") != "PAUSED" or snapshot.get("spend_minor") != 0:
                        raise PolicyError("provider_zero_spend_readback_required", 409)
                    if command.action == "approve" and principal.subject == plan.prepared_by:
                        raise PolicyError("approval_separation_of_duties_required", 403)
                    if command.action == "approve":
                        plan.approved_by = principal.subject
                plan.resource_version += 1
            immediate = command.action in {"approve", "schedule"}
            state = "completed" if immediate else "pending"
            receipt = {"operation_id": str(operation_id), "campaign_id": str(command.campaign_id),
                       "state": state, "correlation_id": correlation,
                       "status_url": f"/internal/v1/marketing/operations/{operation_id}"}
            operation = ProviderCommandModel(
                id=operation_id, tenant_id=principal.tenant_id, campaign_id=command.campaign_id,
                action=command.action, target_state=target, expected_version=plan.resource_version,
                idempotency_key=key, fingerprint=fingerprint, requested_by=principal.subject,
                correlation_id=correlation, state=state, receipt_json=canonical_json(receipt),
            )
            session.add(operation)
            if immediate:
                plan.state = target
            else:
                plan.pending_command_id = operation_id
            audit(session, operation, state)
            await session.commit()
            return receipt
    except IntegrityError:
        async with session_factory() as session:
            previous = await _existing(session, principal.tenant_id, key, fingerprint)
            if previous is not None:
                return json.loads(previous.receipt_json)
        raise PolicyError("concurrent_command_conflict", 409) from None
    except PolicyError as exc:
        # Rollback the rejected mutation, then retain a separate denial audit.
        async with session_factory() as session:
            denied = ProviderCommandModel(id=uuid4(), tenant_id=principal.tenant_id,
                campaign_id=command.campaign_id, action=command.action,
                requested_by=principal.subject, correlation_id=correlation)
            audit(session, denied, "denied", exc.code)
            await session.commit()
        raise


async def _fail(operation_id, code, *, session_factory):
    async with session_factory() as session:
        operation = await session.get(ProviderCommandModel, operation_id, with_for_update=True)
        if operation is not None and operation.state == "running":
            operation.state = "reconciliation_required"
            operation.error_code = code[:80]
            operation.lease_until = None
            audit(session, operation, "reconciliation_required", code[:80])
            await session.commit()


async def run_once(*, session_factory=SessionLocal, driver_factory=driver) -> bool:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        operation = await session.scalar(select(ProviderCommandModel).where(or_(
            ProviderCommandModel.state == "pending",
            and_(ProviderCommandModel.state == "running", ProviderCommandModel.lease_until < now),
        )).order_by(ProviderCommandModel.created_at).with_for_update(skip_locked=True).limit(1))
        if operation is None:
            return False
        if operation.state == "running":
            operation.state = "reconciliation_required"
            operation.error_code = "provider_worker_lease_expired"
            operation.lease_until = None
            audit(session, operation, "reconciliation_required", operation.error_code)
            await session.commit()
            return True
        operation_id = operation.id
        operation.state = "running"
        operation.lease_until = now + timedelta(seconds=300)
        await session.commit()
    try:
        async with session_factory() as session:
            operation = await session.get(ProviderCommandModel, operation_id, with_for_update=True)
            if operation.state != "running":
                return True
            source = await campaign_for(session, operation.campaign_id, operation.tenant_id)
            plan = await session.scalar(select(ProviderCampaignModel).where(
                ProviderCampaignModel.campaign_id == operation.campaign_id,
                ProviderCampaignModel.tenant_id == operation.tenant_id,
            ).with_for_update())
            if plan is None or plan.pending_command_id != operation.id or plan.resource_version != operation.expected_version:
                raise ProviderError("provider_command_binding_invalid")
            policy = load_policy().account(operation.tenant_id, plan.account_alias)
            if binding_hash(policy) != plan.binding_hash:
                raise ProviderError("provider_account_binding_changed")
            adapter = driver_factory(policy)
            if operation.action == "create_draft":
                prepared = await verify_prepared(session, policy, plan, source)
                plan.provider_id = await adapter.create(plan.marker, prepared)
                # Keep a confirmed ID even when the subsequent read-back times out.
                await session.commit()
                operation = await session.get(ProviderCommandModel, operation_id, with_for_update=True)
                plan = await session.get(ProviderCampaignModel, operation.campaign_id, with_for_update=True)
                if operation.state != "running" or plan.pending_command_id != operation_id:
                    return True
            elif operation.action not in {"readback", "pause", "archive"}:
                raise ProviderError("provider_action_forbidden")
            if plan.provider_id is None:
                plan.provider_id = await adapter.find(plan.marker)
            expected = "ARCHIVED" if operation.target_state == "archived" else "PAUSED"
            if operation.action in {"pause", "archive"}:
                # Confirm account ownership before any reducing-state write.
                await adapter.assert_ownership(plan.provider_id)
                await adapter.change(plan.provider_id, operation.action)
            snapshot = await adapter.read(plan.provider_id, expected=expected)
            if snapshot.account_id != policy.provider_account_id or snapshot.provider_id != plan.provider_id or snapshot.configured_status != expected:
                raise ProviderError("provider_readback_binding_invalid")
            plan.snapshot_json = canonical_json(snapshot.document())
            plan.state = operation.target_state
            plan.resource_version += 1
            plan.pending_command_id = None
            operation.state = "completed"
            operation.lease_until = None
            operation.error_code = None
            if operation.action in {"readback", "pause", "archive"}:
                prior = await session.scalars(select(ProviderCommandModel).where(
                    ProviderCommandModel.tenant_id == operation.tenant_id,
                    ProviderCommandModel.campaign_id == operation.campaign_id,
                    ProviderCommandModel.state == "reconciliation_required",
                ).with_for_update())
                for unresolved in prior:
                    unresolved.state = "reconciled"
                    audit(session, unresolved, "reconciled")
            audit(session, operation, "completed")
            await session.commit()
    except (ProviderError, PolicyError, ValueError, KeyError, TypeError) as exc:
        await _fail(operation_id, getattr(exc, "code", "provider_evidence_invalid"), session_factory=session_factory)
    except Exception:
        await _fail(operation_id, "provider_unexpected_failure", session_factory=session_factory)
    return True


async def main() -> None:
    while True:
        try:
            if not await run_once():
                await asyncio.sleep(1)
        except SQLAlchemyError:
            # Leave any durable lease for reconciliation; do not spin on DB loss.
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
