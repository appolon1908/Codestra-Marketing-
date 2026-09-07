from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select

from .db import SessionLocal
from .middleware_client import MiddlewareDeliveryError, MiddlewareMarketingClient
from .models import AuditEventModel, CampaignModel, OperationModel, OutboxModel


UTC = timezone.utc


@dataclass(frozen=True)
class Claim:
    id: UUID
    operation_id: UUID
    payload: dict[str, object]
    attempts: int


def capability_enabled() -> bool:
    return os.getenv("LIVE_ADVERTISING_ENABLED", "false").strip().lower() == "true"


async def claim_one(lease_seconds: int, *, session_factory=SessionLocal) -> Claim | None:
    if not capability_enabled():
        return None
    now = datetime.now(UTC)
    async with session_factory() as session:
        row = await session.scalar(
            select(OutboxModel)
            .where(
                or_(
                    and_(OutboxModel.state == "pending", OutboxModel.next_attempt_at <= now),
                    and_(OutboxModel.state == "processing", OutboxModel.lease_until < now),
                )
            )
            .order_by(OutboxModel.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.state = "processing"
        row.attempts += 1
        row.lease_until = now + timedelta(seconds=lease_seconds)
        row.last_error_code = None
        operation = await session.get(OperationModel, row.operation_id, with_for_update=True)
        if operation is None:
            row.state = "dead_letter"
            row.last_error_code = "operation_missing"
            await session.commit()
            return None
        operation.attempts = row.attempts
        await session.commit()
        return Claim(row.id, row.operation_id, json.loads(row.payload_json), row.attempts)


async def complete(claim: Claim, result: dict[str, object], *, session_factory=SessionLocal) -> None:
    async with session_factory() as session:
        row = await session.scalar(select(OutboxModel).where(OutboxModel.id == claim.id).with_for_update())
        operation = await session.scalar(
            select(OperationModel).where(OperationModel.id == claim.operation_id).with_for_update()
        )
        if (
            row is None
            or operation is None
            or row.state != "processing"
            or row.attempts != claim.attempts
        ):
            return
        row.state = "published"
        row.lease_until = None
        # A concurrently completed approval-invalidated stop may have retired an
        # activation while its original delivery call was in flight. Preserve
        # that terminal supersession instead of resurrecting it as accepted.
        if operation.state != "superseded":
            operation.state = "accepted"
        operation.result_json = json.dumps(result, sort_keys=True, separators=(",", ":"))
        if operation.kind == "campaign.approval_invalidation_stop":
            prior_activations = await session.scalars(
                select(OperationModel)
                .where(
                    OperationModel.tenant_id == operation.tenant_id,
                    OperationModel.aggregate_id == operation.aggregate_id,
                    OperationModel.kind == "campaign.activate",
                    OperationModel.state.in_({"pending", "processing", "accepted", "reconciliation_required"}),
                )
                .with_for_update()
            )
            for prior_activation in prior_activations:
                prior_activation.state = "superseded"
        action = str(claim.payload.get("action", "activate"))
        audit_action = "activation" if action == "activate" else action
        session.add(
            AuditEventModel(
                tenant_id=operation.tenant_id, operation_id=operation.id, aggregate_type="campaign",
                aggregate_id=operation.aggregate_id,
                action=f"campaign.{audit_action}.dispatched",
                outcome="accepted", actor_id="marketing-outbox-worker",
                correlation_id=operation.correlation_id, detail_json="{}",
            )
        )
        await session.commit()


async def fail(
    claim: Claim, error: MiddlewareDeliveryError, max_attempts: int, *, session_factory=SessionLocal
) -> None:
    async with session_factory() as session:
        row = await session.scalar(select(OutboxModel).where(OutboxModel.id == claim.id).with_for_update())
        operation = await session.scalar(
            select(OperationModel).where(OperationModel.id == claim.operation_id).with_for_update()
        )
        if (
            row is None
            or operation is None
            or row.state != "processing"
            or row.attempts != claim.attempts
        ):
            return
        terminal = not error.retryable or claim.attempts >= max_attempts
        row.state = "dead_letter" if terminal else "pending"
        row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(2 ** min(claim.attempts, 8), 300))
        row.lease_until = None
        row.last_error_code = error.code[:80]
        if terminal:
            operation.state = "reconciliation_required" if error.retryable else "failed"
            operation.error_code = error.code[:80]
        action = str(claim.payload.get("action", "activate"))
        audit_action = "activation" if action == "activate" else action
        session.add(
            AuditEventModel(
                tenant_id=operation.tenant_id,
                operation_id=operation.id,
                aggregate_type="campaign",
                aggregate_id=operation.aggregate_id,
                action=f"campaign.{audit_action}.delivery_failed",
                outcome="dead_letter" if terminal else "retry_scheduled",
                actor_id="marketing-outbox-worker",
                correlation_id=operation.correlation_id,
                detail_json=json.dumps(
                    {"attempt": claim.attempts, "error_code": error.code[:80]},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        await session.commit()


async def run_once(
    client: MiddlewareMarketingClient,
    *,
    lease_seconds: int,
    max_attempts: int,
    session_factory=SessionLocal,
) -> bool:
    item = await claim_one(lease_seconds, session_factory=session_factory)
    if item is None:
        return False
    try:
        expected_version = int(item.payload["expected_version"])
        expected_state = str(item.payload["expected_state"])
        campaign_id = UUID(str(item.payload["campaign_id"]))
    except (KeyError, TypeError, ValueError):
        await fail(
            item,
            MiddlewareDeliveryError("activation_payload_invalid", retryable=False),
            max_attempts,
            session_factory=session_factory,
        )
        return True
    async with session_factory() as session:
        operation = await session.scalar(
            select(OperationModel).where(OperationModel.id == item.operation_id)
        )
        campaign = await session.scalar(
            select(CampaignModel).where(
                CampaignModel.id == campaign_id,
                CampaignModel.tenant_id == str(item.payload.get("tenant_id", "")),
            ).with_for_update()
        )
        approval_stop = (
            operation is not None and operation.kind == "campaign.approval_invalidation_stop"
        )
        valid = campaign is not None and (
            (approval_stop and campaign.state == "draft")
            or (
                not approval_stop
                and campaign.state == expected_state
                and campaign.resource_version == expected_version
            )
        )
        if not valid:
            delivery_error = MiddlewareDeliveryError("campaign_approval_stale", retryable=False)
            result = None
        else:
            delivery_error = None
            try:
                # Keep the aggregate row locked through Middleware acceptance.
                # Material edits and lifecycle changes use the same lock, so an
                # approval cannot be invalidated between validation and delivery.
                result = await client.deliver(item.payload)
            except MiddlewareDeliveryError as exc:
                delivery_error = exc
    if delivery_error is not None:
        await fail(item, delivery_error, max_attempts, session_factory=session_factory)
    else:
        assert result is not None
        await complete(item, result, session_factory=session_factory)
    return True


async def main() -> None:
    lease = max(5, min(int(os.getenv("MARKETING_OUTBOX_LEASE_SECONDS", "30")), 300))
    attempts = max(1, min(int(os.getenv("MARKETING_OUTBOX_MAX_ATTEMPTS", "8")), 32))
    poll = max(0.1, min(float(os.getenv("MARKETING_OUTBOX_POLL_SECONDS", "1")), 30.0))
    client = MiddlewareMarketingClient()
    while True:
        if not await run_once(client, lease_seconds=lease, max_attempts=attempts):
            await asyncio.sleep(poll)


if __name__ == "__main__":
    asyncio.run(main())
