from __future__ import annotations

import hashlib
import json
import os
import asyncio
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .auth import Principal, authenticate
from .models import (
    ApprovalModel,
    AttributionTouchModel,
    AudienceModel,
    AuditEventModel,
    CampaignModel,
    CreativeModel,
    OperationModel,
    OutboxModel,
)
from .providers.meta_read import MetaReadClient

app = FastAPI(title="Codestra Marketing API", version="0.3.0")


def require_correlation_id(
    x_correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=8, max_length=128)],
) -> str:
    return x_correlation_id


router = APIRouter(
    prefix="/v1/marketing",
    dependencies=[Depends(authenticate), Depends(require_correlation_id)],
)

LIVE_ADVERTISING_ENABLED = os.getenv("LIVE_ADVERTISING_ENABLED", "false").lower() == "true"
META_READ_SYNC_ENABLED = os.getenv("META_READ_SYNC_ENABLED", "false").lower() == "true"
META_ALLOWED_AD_ACCOUNT_IDS = {
    value.strip()
    for value in os.getenv("META_ALLOWED_AD_ACCOUNT_IDS", "").split(",")
    if value.strip()
}
SERVICE = "codestra-marketing"
CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
REQUEST_COUNT = Counter(
    "codestra_marketing_http_requests_total",
    "Marketing API requests",
    ("method", "route", "status"),
)
REQUEST_LATENCY = Histogram(
    "codestra_marketing_http_request_duration_seconds",
    "Marketing API request latency",
    ("method", "route"),
)
ATTRIBUTION_TOUCHES = Counter(
    "codestra_marketing_attribution_touches_total",
    "Durably accepted attribution touches",
)


@app.middleware("http")
async def operational_headers(request: Request, call_next):
    supplied_correlation_id = request.headers.get("X-Correlation-ID", "").strip()
    if supplied_correlation_id and not CORRELATION_RE.fullmatch(supplied_correlation_id):
        correlation_id = str(uuid4())
        return JSONResponse(
            status_code=400,
            content={"detail": "invalid_correlation_id", "correlation_id": correlation_id},
            headers={"Cache-Control": "no-store", "X-Correlation-ID": correlation_id},
        )
    correlation_id = supplied_correlation_id or str(uuid4())
    request.state.correlation_id = correlation_id
    started = asyncio.get_running_loop().time()
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse(status_code=500, content={"detail": "internal_error", "correlation_id": correlation_id})
    route = request.scope.get("route")
    route_path = getattr(route, "path", "unmatched")
    elapsed = asyncio.get_running_loop().time() - started
    REQUEST_LATENCY.labels(request.method, route_path).observe(elapsed)
    REQUEST_COUNT.labels(request.method, route_path, str(response.status_code)).inc()
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Correlation-ID"] = correlation_id
    return response

TenantHeader = Annotated[str, Header(alias="X-Tenant-ID", min_length=1, max_length=64)]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


class CampaignState(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    PAUSED = "paused"


class CampaignCreate(BaseModel):
    tenant_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=80)
    daily_budget_minor: int = Field(ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class Campaign(BaseModel):
    id: UUID
    tenant_id: str
    name: str
    objective: str
    daily_budget_minor: int
    currency: str
    state: str
    resource_version: int
    model_config = {"from_attributes": True}


class ApprovalAction(BaseModel):
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=2000)
    expected_version: int = Field(ge=1)


class CampaignUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    objective: str | None = Field(default=None, min_length=1, max_length=80)
    daily_budget_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class AudienceCreate(BaseModel):
    tenant_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    definition: dict[str, Any]


class Audience(BaseModel):
    id: UUID
    tenant_id: str
    name: str
    definition: dict[str, Any]
    resource_version: int


class AudienceUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    definition: dict[str, Any] | None = None


class CreativeCreate(BaseModel):
    tenant_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    content: dict[str, Any]


class Creative(BaseModel):
    id: UUID
    tenant_id: str
    name: str
    content: dict[str, Any]
    approval_state: str
    resource_version: int


class CreativeUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    content: dict[str, Any] | None = None


class Operation(BaseModel):
    id: UUID
    tenant_id: str
    kind: str
    aggregate_id: UUID
    state: str
    correlation_id: str
    attempts: int
    error_code: str | None
    status_url: str


class AttributionTouchCreate(BaseModel):
    model_config = {"extra": "forbid"}
    event_id: str = Field(min_length=1, max_length=128)
    lead_id: str = Field(min_length=1, max_length=128)
    campaign_id: UUID | None = None
    channel: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class AttributionTouch(BaseModel):
    id: UUID
    event_id: str
    lead_id: str
    campaign_id: UUID | None
    channel: str
    occurred_at: datetime
    model_config = {"from_attributes": True}


def _operation_response(row: OperationModel) -> Operation:
    return Operation(
        id=row.id,
        tenant_id=row.tenant_id,
        kind=row.kind,
        aggregate_id=row.aggregate_id,
        state=row.state,
        correlation_id=row.correlation_id,
        attempts=row.attempts,
        error_code=row.error_code,
        status_url=f"/v1/marketing/operations/{row.id}",
    )


def _audience_response(row: AudienceModel) -> Audience:
    return Audience(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        definition=json.loads(row.definition_json),
        resource_version=row.resource_version,
    )


def _creative_response(row: CreativeModel) -> Creative:
    return Creative(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        content=json.loads(row.content_json),
        approval_state=row.approval_state,
        resource_version=row.resource_version,
    )


def _tenant(header_tenant: str, body_tenant: str | None = None) -> str:
    if body_tenant is not None and body_tenant != header_tenant:
        raise HTTPException(status_code=403, detail="tenant_mismatch")
    return header_tenant


def _principal(request: Request) -> Principal:
    value = getattr(request.state, "principal", None)
    if not isinstance(value, Principal):
        raise HTTPException(status_code=401, detail="authenticated_principal_required")
    return value


def _request_scope(request: Request | None, scope: str) -> Principal | None:
    # Direct service-function tests may omit Request; every HTTP route receives it
    # from FastAPI and is additionally guarded by the router authentication dependency.
    if request is None:
        return None
    principal = _principal(request)
    principal.require(scope)
    return principal


def _bind_actor(principal: Principal, claimed_actor: str) -> None:
    if claimed_actor != principal.subject:
        raise HTTPException(status_code=403, detail="actor_identity_mismatch")


def _fingerprint(kind: str, tenant_id: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": kind, "tenant_id": tenant_id, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _campaign_for_tenant(
    session: AsyncSession, campaign_id: UUID, tenant_id: str, *, lock: bool = False
) -> CampaignModel:
    statement = select(CampaignModel).where(
            CampaignModel.id == campaign_id,
            CampaignModel.tenant_id == tenant_id,
        )
    if lock:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="campaign_not_found")
    return row


async def _record_mutation(
    session: AsyncSession,
    *,
    aggregate_id: UUID,
    kind: str,
    tenant_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    principal: Principal,
    correlation_id: str,
    outcome: str = "completed",
    aggregate_type: str = "campaign",
) -> bool:
    fingerprint = _fingerprint(
        kind,
        tenant_id,
        {"aggregate_id": str(aggregate_id), "payload": payload},
    )
    result = await session.execute(
        select(OperationModel).where(
            OperationModel.tenant_id == tenant_id,
            OperationModel.kind == kind,
            OperationModel.idempotency_key == idempotency_key,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return False
    operation = OperationModel(
        tenant_id=tenant_id,
        kind=kind,
        aggregate_id=aggregate_id,
        state=outcome,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        requested_by=principal.subject,
        correlation_id=correlation_id,
        result_json="{}",
    )
    session.add(operation)
    await session.flush()
    session.add(
        AuditEventModel(
            tenant_id=tenant_id,
            operation_id=operation.id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            action=kind,
            outcome=outcome,
            actor_id=principal.subject,
            correlation_id=correlation_id,
            detail_json="{}",
        )
    )
    return True


@app.get("/health")
@app.get("/health/live")
def health(request: Request = None) -> dict[str, object]:
    correlation_id = getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))
    return {
        "status": "ok",
        "service": SERVICE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id,
    }


@app.get("/ready")
@app.get("/readiness")
@app.get("/health/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)):
    required_identity = ("KEYCLOAK_ISSUER", "KEYCLOAK_AUDIENCE", "KEYCLOAK_ALLOWED_CLIENT_IDS")
    if any(not os.getenv(name, "").strip() for name in required_identity):
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": SERVICE,
                "dependencies": {"database": "unknown", "identity": "unconfigured"},
                "correlation_id": request.state.correlation_id,
            },
        )
    try:
        await asyncio.wait_for(session.execute(select(1)), timeout=2.0)
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "service": SERVICE, "dependencies": {"database": "unavailable"}, "correlation_id": request.state.correlation_id})
    return {"status": "ready", "service": SERVICE, "dependencies": {"database": "ready", "configuration": "ready"}, "correlation_id": request.state.correlation_id}


@app.get("/version")
def version(request: Request = None) -> dict[str, object]:
    correlation_id = getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))
    return {
        "service": SERVICE,
        "application_version": app.version,
        "api_versions": ["v1"],
        "release_id": os.getenv("CODESTRA_RELEASE_ID", "unknown"),
        "git_sha": os.getenv("CODESTRA_GIT_SHA", "unknown"),
        "image_digest": os.getenv("CODESTRA_IMAGE_DIGEST", "unknown"),
        "build_time": os.getenv("CODESTRA_BUILD_TIMESTAMP", "unknown"),
        "schema_version": os.getenv("CODESTRA_MIGRATION_REVISION", "unknown"),
        "configuration_checksum": os.getenv("CODESTRA_CONFIGURATION_CHECKSUM", "unknown"),
        "environment": os.getenv("CODESTRA_ENVIRONMENT", "unknown"),
        "correlation_id": correlation_id,
    }


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request, principal: Principal = Depends(authenticate)) -> Response:
    principal.require("marketing.metrics.read")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/capabilities")
@router.get("/capabilities")
def capabilities(request: Request = None) -> dict[str, object]:
    correlation_id = getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))
    return {
        "service": SERVICE,
        "maintenance_mode": os.getenv("MAINTENANCE_MODE", "false").lower() == "true",
        "degraded_mode": False,
        "business_writes_enabled": False,
        "external_delivery_enabled": LIVE_ADVERTISING_ENABLED,
        "live_advertising_enabled": LIVE_ADVERTISING_ENABLED,
        "read_only_mode": not LIVE_ADVERTISING_ENABLED,
        "simulation_enabled": not LIVE_ADVERTISING_ENABLED,
        "supported_api_versions": ["v1"],
        "campaigns": True,
        "audiences": True,
        "creatives": True,
        "approvals": True,
        "attribution": True,
        "meta_read_sync": META_READ_SYNC_ENABLED,
        "provider_writes": LIVE_ADVERTISING_ENABLED,
        "correlation_id": correlation_id,
    }


@router.post("/campaigns", response_model=Campaign, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> CampaignModel:
    _request_scope(request, "marketing.write")
    tenant_id = _tenant(x_tenant_id, body.tenant_id)
    data = body.model_dump(mode="json", exclude={"tenant_id"})
    fingerprint = _fingerprint("campaign.create", tenant_id, data)
    existing = await session.execute(
        select(CampaignModel).where(
            CampaignModel.tenant_id == tenant_id,
            CampaignModel.idempotency_key == idempotency_key,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        if row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return row

    row = CampaignModel(
        **data,
        tenant_id=tenant_id,
        state=CampaignState.DRAFT.value,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.execute(
            select(CampaignModel).where(
                CampaignModel.tenant_id == tenant_id,
                CampaignModel.idempotency_key == idempotency_key,
            )
        )
        row = existing.scalar_one_or_none()
        if row is None or row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return row
    await session.refresh(row)
    return row


@router.get("/campaigns/{campaign_id}", response_model=Campaign)
async def get_campaign(
    campaign_id: UUID,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> CampaignModel:
    _request_scope(request, "marketing.read")
    return await _campaign_for_tenant(session, campaign_id, x_tenant_id)


@router.get("/campaigns", response_model=list[Campaign])
async def list_campaigns(
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> list[CampaignModel]:
    _request_scope(request, "marketing.read")
    result = await session.execute(
        select(CampaignModel)
        .where(CampaignModel.tenant_id == x_tenant_id)
        .order_by(CampaignModel.created_at.desc())
    )
    return list(result.scalars().all())


@router.patch("/campaigns/{campaign_id}", response_model=Campaign)
async def update_campaign(
    campaign_id: UUID,
    body: CampaignUpdate,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> CampaignModel:
    principal = _principal(request)
    principal.require("marketing.write")
    result = await session.execute(
        select(CampaignModel)
        .where(CampaignModel.id == campaign_id, CampaignModel.tenant_id == x_tenant_id)
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="campaign_not_found")
    payload = body.model_dump(mode="json")
    if not await _record_mutation(
        session,
        aggregate_id=campaign_id,
        kind="campaign.update",
        tenant_id=x_tenant_id,
        idempotency_key=idempotency_key,
        payload=payload,
        principal=principal,
        correlation_id=request.state.correlation_id,
    ):
        return row
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    changed_approval_input = False
    for field in ("name", "objective", "daily_budget_minor", "currency"):
        value = getattr(body, field)
        if value is not None and value != getattr(row, field):
            setattr(row, field, value)
            changed_approval_input = changed_approval_input or field in {
                "objective",
                "daily_budget_minor",
                "currency",
            }
    if changed_approval_input and row.state in {
        CampaignState.PENDING_APPROVAL.value,
        CampaignState.APPROVED.value,
        CampaignState.PAUSED.value,
    }:
        pending_approvals = await session.scalars(
            select(ApprovalModel).where(
                ApprovalModel.tenant_id == x_tenant_id,
                ApprovalModel.campaign_id == campaign_id,
                ApprovalModel.state == "pending",
            ).with_for_update()
        )
        for approval in pending_approvals:
            approval.state = "invalidated"
            approval.decided_by = principal.subject
            approval.decided_at = datetime.now(timezone.utc)
            approval.reason = "campaign_materially_changed"
        row.state = CampaignState.DRAFT.value
    row.resource_version += 1
    if changed_approval_input and LIVE_ADVERTISING_ENABLED:
        activation = await session.scalar(
            select(OperationModel).where(
                OperationModel.tenant_id == x_tenant_id,
                OperationModel.aggregate_id == campaign_id,
                OperationModel.kind == "campaign.activate",
                OperationModel.state.in_({"pending", "processing", "accepted", "reconciliation_required"}),
            )
        )
        if activation is not None:
            activation_outbox = await session.scalar(
                select(OutboxModel).where(OutboxModel.operation_id == activation.id)
            )
            if activation_outbox is None:
                raise RuntimeError("activation outbox evidence missing")
            try:
                activated_version = int(json.loads(activation_outbox.payload_json)["expected_version"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("activation version evidence invalid") from exc
            stop_key = "system-stop-" + hashlib.sha256(
                f"{idempotency_key}:approval-invalidation-stop".encode()
            ).hexdigest()
            await _record_mutation(
                session,
                aggregate_id=campaign_id,
                kind="campaign.approval_invalidation_stop",
                tenant_id=x_tenant_id,
                idempotency_key=stop_key,
                payload={"expected_version": activated_version, "expected_state": "approved"},
                principal=principal,
                correlation_id=request.state.correlation_id,
                outcome="pending",
            )
            stop_operation = await session.scalar(
                select(OperationModel).where(
                    OperationModel.tenant_id == x_tenant_id,
                    OperationModel.kind == "campaign.approval_invalidation_stop",
                    OperationModel.idempotency_key == stop_key,
                )
            )
            if stop_operation is None:
                raise RuntimeError("approval invalidation stop operation missing")
            session.add(
                OutboxModel(
                    tenant_id=x_tenant_id,
                    operation_id=stop_operation.id,
                    destination="middleware",
                    event_type="marketing.campaign.approval_invalidated_stop_requested",
                    payload_json=json.dumps(
                        {
                            "operation_id": str(stop_operation.id),
                            "campaign_id": str(campaign_id),
                            "action": "pause",
                            "expected_state": "approved",
                            "expected_version": activated_version,
                            "tenant_id": x_tenant_id,
                            "correlation_id": request.state.correlation_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
    await session.commit()
    await session.refresh(row)
    return row


@router.post("/campaigns/{campaign_id}/submit-for-approval", response_model=Campaign)
async def submit_for_approval(
    campaign_id: UUID,
    body: ApprovalAction,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> CampaignModel:
    principal = _principal(request)
    principal.require("marketing.write")
    _bind_actor(principal, body.actor_id)
    row = await _campaign_for_tenant(session, campaign_id, x_tenant_id, lock=True)
    if not await _record_mutation(
        session,
        aggregate_id=campaign_id,
        kind="campaign.submit_for_approval",
        tenant_id=x_tenant_id,
        idempotency_key=idempotency_key,
        payload=body.model_dump(mode="json"),
        principal=principal,
        correlation_id=request.state.correlation_id,
    ):
        return row
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if row.state != CampaignState.DRAFT.value:
        raise HTTPException(status_code=409, detail="invalid_campaign_state")
    row.state = CampaignState.PENDING_APPROVAL.value
    row.resource_version += 1
    session.add(
        ApprovalModel(
            tenant_id=x_tenant_id,
            campaign_id=row.id,
            requested_by=principal.subject,
            state="pending",
            reason=body.reason,
        )
    )
    await session.commit()
    await session.refresh(row)
    return row


@router.post("/campaigns/{campaign_id}/approve", response_model=Campaign)
async def approve_campaign(
    campaign_id: UUID,
    body: ApprovalAction,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> CampaignModel:
    principal = _principal(request)
    principal.require("marketing.approve")
    _bind_actor(principal, body.actor_id)
    row = await _campaign_for_tenant(session, campaign_id, x_tenant_id, lock=True)
    if not await _record_mutation(
        session,
        aggregate_id=campaign_id,
        kind="campaign.approve",
        tenant_id=x_tenant_id,
        idempotency_key=idempotency_key,
        payload=body.model_dump(mode="json"),
        principal=principal,
        correlation_id=request.state.correlation_id,
    ):
        return row
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if row.state != CampaignState.PENDING_APPROVAL.value:
        raise HTTPException(status_code=409, detail="invalid_campaign_state")
    approval_result = await session.execute(
        select(ApprovalModel)
        .where(
            ApprovalModel.tenant_id == x_tenant_id,
            ApprovalModel.campaign_id == campaign_id,
            ApprovalModel.state == "pending",
        )
        .order_by(ApprovalModel.created_at.desc())
    )
    approval = approval_result.scalars().first()
    if approval is None:
        raise HTTPException(status_code=409, detail="pending_approval_missing")
    if approval.requested_by == principal.subject:
        raise HTTPException(status_code=409, detail="approval_separation_of_duties_required")
    approval.state = "approved"
    approval.decided_by = principal.subject
    approval.reason = body.reason
    approval.decided_at = datetime.now(timezone.utc)
    row.state = CampaignState.APPROVED.value
    row.resource_version += 1
    await session.commit()
    await session.refresh(row)
    return row


async def _transition_campaign(
    campaign_id: UUID,
    body: ApprovalAction,
    request: Request,
    tenant_id: str,
    idempotency_key: str,
    session: AsyncSession,
    *,
    action: str,
    required_scope: str,
    from_state: str,
    to_state: str,
) -> CampaignModel:
    principal = _principal(request)
    principal.require(required_scope)
    _bind_actor(principal, body.actor_id)
    row = await _campaign_for_tenant(session, campaign_id, tenant_id, lock=True)
    if not await _record_mutation(
        session,
        aggregate_id=campaign_id,
        kind=action,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        payload=body.model_dump(mode="json"),
        principal=principal,
        correlation_id=request.state.correlation_id,
    ):
        return row
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if row.state != from_state:
        raise HTTPException(status_code=409, detail="invalid_campaign_state")
    if action == "campaign.reject":
        approval = (
            await session.execute(
                select(ApprovalModel)
                .where(
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.campaign_id == campaign_id,
                    ApprovalModel.state == "pending",
                )
                .order_by(ApprovalModel.created_at.desc())
            )
        ).scalars().first()
        if approval is None:
            raise HTTPException(status_code=409, detail="pending_approval_missing")
        if approval.requested_by == principal.subject:
            raise HTTPException(status_code=409, detail="approval_separation_of_duties_required")
        approval.state = "rejected"
        approval.decided_by = principal.subject
        approval.reason = body.reason
        approval.decided_at = datetime.now(timezone.utc)
    row.state = to_state
    row.resource_version += 1
    if action in {"campaign.pause", "campaign.resume"} and LIVE_ADVERTISING_ENABLED:
        prior_activation = await session.scalar(
            select(OperationModel).where(
                OperationModel.tenant_id == tenant_id,
                OperationModel.aggregate_id == campaign_id,
                OperationModel.kind == "campaign.activate",
                OperationModel.state.in_({"pending", "processing", "accepted", "reconciliation_required"}),
            )
        )
        if prior_activation is not None:
            operation = await session.scalar(
                select(OperationModel).where(
                    OperationModel.tenant_id == tenant_id,
                    OperationModel.kind == action,
                    OperationModel.idempotency_key == idempotency_key,
                )
            )
            if operation is None:
                raise RuntimeError("durable transition operation missing")
            operation.state = "pending"
            session.add(
                OutboxModel(
                    tenant_id=tenant_id,
                    operation_id=operation.id,
                    destination="middleware",
                    event_type=f"marketing.{action}.requested",
                    payload_json=json.dumps(
                        {
                            "operation_id": str(operation.id),
                            "campaign_id": str(campaign_id),
                            "action": action.removeprefix("campaign."),
                            "expected_state": to_state,
                            "expected_version": row.resource_version,
                            "tenant_id": tenant_id,
                            "correlation_id": request.state.correlation_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
    await session.commit()
    await session.refresh(row)
    return row


@router.post("/campaigns/{campaign_id}/reject", response_model=Campaign)
async def reject_campaign(
    campaign_id: UUID,
    body: ApprovalAction,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> CampaignModel:
    return await _transition_campaign(
        campaign_id, body, request, x_tenant_id, idempotency_key, session,
        action="campaign.reject", required_scope="marketing.approve",
        from_state=CampaignState.PENDING_APPROVAL.value, to_state=CampaignState.DRAFT.value,
    )


@router.post("/campaigns/{campaign_id}/pause", response_model=Campaign)
async def pause_campaign(
    campaign_id: UUID,
    body: ApprovalAction,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> CampaignModel:
    return await _transition_campaign(
        campaign_id, body, request, x_tenant_id, idempotency_key, session,
        action="campaign.pause", required_scope="marketing.write",
        from_state=CampaignState.APPROVED.value, to_state=CampaignState.PAUSED.value,
    )


@router.post("/campaigns/{campaign_id}/resume", response_model=Campaign)
async def resume_campaign(
    campaign_id: UUID,
    body: ApprovalAction,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> CampaignModel:
    return await _transition_campaign(
        campaign_id, body, request, x_tenant_id, idempotency_key, session,
        action="campaign.resume", required_scope="marketing.write",
        from_state=CampaignState.PAUSED.value, to_state=CampaignState.APPROVED.value,
    )


@router.post(
    "/campaigns/{campaign_id}/activate",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={423: {"model": Operation}},
)
async def activate_campaign(
    campaign_id: UUID,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> Operation | JSONResponse:
    principal = _principal(request)
    principal.require("marketing.provider.write")
    # Serialize all activation attempts for this aggregate. This makes the
    # aggregate-level duplicate check below effective even when callers use
    # different idempotency keys concurrently.
    row = await _campaign_for_tenant(session, campaign_id, x_tenant_id, lock=True)
    fingerprint = _fingerprint("campaign.activate", x_tenant_id, {"campaign_id": str(campaign_id)})
    existing_result = await session.execute(
        select(OperationModel).where(
            OperationModel.tenant_id == x_tenant_id,
            OperationModel.kind == "campaign.activate",
            OperationModel.idempotency_key == idempotency_key,
        )
    )
    operation = existing_result.scalar_one_or_none()
    if operation is not None:
        if operation.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
    else:
        active_operation = await session.scalar(
            select(OperationModel).where(
                OperationModel.tenant_id == x_tenant_id,
                OperationModel.aggregate_id == campaign_id,
                OperationModel.kind == "campaign.activate",
                OperationModel.state.in_({"pending", "processing", "accepted", "reconciliation_required"}),
            )
        )
        if active_operation is not None:
            raise HTTPException(status_code=409, detail="campaign_activation_already_exists")
        if row.state != CampaignState.APPROVED.value:
            raise HTTPException(status_code=409, detail="campaign_not_approved")
        denied = not LIVE_ADVERTISING_ENABLED
        operation = OperationModel(
            tenant_id=x_tenant_id,
            kind="campaign.activate",
            aggregate_id=campaign_id,
            state="denied" if denied else "pending",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            requested_by=principal.subject,
            correlation_id=request.state.correlation_id,
            error_code="live_advertising_disabled" if denied else None,
        )
        session.add(operation)
        await session.flush()
        if not denied:
            session.add(
                OutboxModel(
                    tenant_id=x_tenant_id,
                    operation_id=operation.id,
                    destination="middleware",
                    event_type="marketing.campaign.activation_requested",
                    payload_json=json.dumps(
                        {
                            "operation_id": str(operation.id),
                            "campaign_id": str(campaign_id),
                            "action": "activate",
                            "expected_state": CampaignState.APPROVED.value,
                            "expected_version": row.resource_version,
                            "tenant_id": x_tenant_id,
                            "correlation_id": request.state.correlation_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        session.add(
            AuditEventModel(
                tenant_id=x_tenant_id,
                operation_id=operation.id,
                aggregate_type="campaign",
                aggregate_id=campaign_id,
                action="campaign.activate",
                outcome="denied" if denied else "accepted",
                actor_id=principal.subject,
                correlation_id=request.state.correlation_id,
                detail_json=json.dumps({"external_effect_executed": False}, separators=(",", ":")),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            retry_result = await session.execute(
                select(OperationModel).where(
                    OperationModel.tenant_id == x_tenant_id,
                    OperationModel.kind == "campaign.activate",
                    OperationModel.idempotency_key == idempotency_key,
                )
            )
            operation = retry_result.scalar_one_or_none()
            if operation is None or operation.request_fingerprint != fingerprint:
                raise HTTPException(status_code=409, detail="idempotency_conflict")
        await session.refresh(operation)

    body = _operation_response(operation)
    if operation.state == "denied":
        return JSONResponse(status_code=423, content=body.model_dump(mode="json"))
    return body


@router.get("/operations/{operation_id}", response_model=Operation)
async def get_operation(
    operation_id: UUID,
    request: Request,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> Operation:
    _principal(request).require("marketing.read")
    result = await session.execute(
        select(OperationModel).where(
            OperationModel.id == operation_id,
            OperationModel.tenant_id == x_tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="operation_not_found")
    return _operation_response(row)


@router.post("/audiences", response_model=Audience, status_code=status.HTTP_201_CREATED)
async def create_audience(
    body: AudienceCreate,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> Audience:
    _request_scope(request, "marketing.write")
    tenant_id = _tenant(x_tenant_id, body.tenant_id)
    definition_json = json.dumps(body.definition, sort_keys=True, separators=(",", ":"))
    fingerprint = _fingerprint("audience.create", tenant_id, {"name": body.name, "definition": body.definition})
    result = await session.execute(
        select(AudienceModel).where(
            AudienceModel.tenant_id == tenant_id,
            AudienceModel.idempotency_key == idempotency_key,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        if row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
    else:
        row = AudienceModel(
            tenant_id=tenant_id,
            name=body.name,
            definition_json=definition_json,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _audience_response(row)


@router.get("/audiences", response_model=list[Audience])
async def list_audiences(
    request: Request,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> list[Audience]:
    _principal(request).require("marketing.read")
    rows = (
        await session.execute(
            select(AudienceModel).where(AudienceModel.tenant_id == x_tenant_id).order_by(AudienceModel.name)
        )
    ).scalars().all()
    return [_audience_response(row) for row in rows]


@router.get("/audiences/{audience_id}", response_model=Audience)
async def get_audience(
    audience_id: UUID,
    request: Request,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> Audience:
    _principal(request).require("marketing.read")
    row = await session.scalar(
        select(AudienceModel).where(AudienceModel.id == audience_id, AudienceModel.tenant_id == x_tenant_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="audience_not_found")
    return _audience_response(row)


@router.patch("/audiences/{audience_id}", response_model=Audience)
async def update_audience(
    audience_id: UUID,
    body: AudienceUpdate,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> Audience:
    principal = _principal(request)
    principal.require("marketing.write")
    row = await session.scalar(
        select(AudienceModel)
        .where(AudienceModel.id == audience_id, AudienceModel.tenant_id == x_tenant_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="audience_not_found")
    if not await _record_mutation(
        session, aggregate_id=audience_id, kind="audience.update", tenant_id=x_tenant_id,
        idempotency_key=idempotency_key, payload=body.model_dump(mode="json"), principal=principal,
        correlation_id=request.state.correlation_id, aggregate_type="audience",
    ):
        return _audience_response(row)
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if body.name is not None:
        row.name = body.name
    if body.definition is not None:
        row.definition_json = json.dumps(body.definition, sort_keys=True, separators=(",", ":"))
    row.resource_version += 1
    await session.commit()
    await session.refresh(row)
    return _audience_response(row)


@router.post("/creatives", response_model=Creative, status_code=status.HTTP_201_CREATED)
async def create_creative(
    body: CreativeCreate,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> Creative:
    _request_scope(request, "marketing.write")
    tenant_id = _tenant(x_tenant_id, body.tenant_id)
    content_json = json.dumps(body.content, sort_keys=True, separators=(",", ":"))
    fingerprint = _fingerprint("creative.create", tenant_id, {"name": body.name, "content": body.content})
    result = await session.execute(
        select(CreativeModel).where(
            CreativeModel.tenant_id == tenant_id,
            CreativeModel.idempotency_key == idempotency_key,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        if row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
    else:
        row = CreativeModel(
            tenant_id=tenant_id,
            name=body.name,
            content_json=content_json,
            approval_state="draft",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _creative_response(row)


@router.get("/creatives", response_model=list[Creative])
async def list_creatives(
    request: Request,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> list[Creative]:
    _principal(request).require("marketing.read")
    rows = (
        await session.execute(
            select(CreativeModel).where(CreativeModel.tenant_id == x_tenant_id).order_by(CreativeModel.name)
        )
    ).scalars().all()
    return [_creative_response(row) for row in rows]


@router.get("/creatives/{creative_id}", response_model=Creative)
async def get_creative(
    creative_id: UUID,
    request: Request,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> Creative:
    _principal(request).require("marketing.read")
    row = await session.scalar(
        select(CreativeModel).where(CreativeModel.id == creative_id, CreativeModel.tenant_id == x_tenant_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="creative_not_found")
    return _creative_response(row)


@router.patch("/creatives/{creative_id}", response_model=Creative)
async def update_creative(
    creative_id: UUID,
    body: CreativeUpdate,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> Creative:
    principal = _principal(request)
    principal.require("marketing.write")
    row = await session.scalar(
        select(CreativeModel)
        .where(CreativeModel.id == creative_id, CreativeModel.tenant_id == x_tenant_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="creative_not_found")
    if not await _record_mutation(
        session, aggregate_id=creative_id, kind="creative.update", tenant_id=x_tenant_id,
        idempotency_key=idempotency_key, payload=body.model_dump(mode="json"), principal=principal,
        correlation_id=request.state.correlation_id, aggregate_type="creative",
    ):
        return _creative_response(row)
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if body.name is not None:
        row.name = body.name
    if body.content is not None:
        row.content_json = json.dumps(body.content, sort_keys=True, separators=(",", ":"))
        row.approval_state = "draft"
        row.approval_requested_by = None
    row.resource_version += 1
    await session.commit()
    await session.refresh(row)
    return _creative_response(row)


async def _transition_creative(
    creative_id: UUID,
    body: ApprovalAction,
    request: Request,
    tenant_id: str,
    idempotency_key: str,
    session: AsyncSession,
    *,
    action: str,
    required_scope: str,
    from_state: str,
    to_state: str,
) -> Creative:
    principal = _principal(request)
    principal.require(required_scope)
    _bind_actor(principal, body.actor_id)
    row = await session.scalar(
        select(CreativeModel)
        .where(CreativeModel.id == creative_id, CreativeModel.tenant_id == tenant_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="creative_not_found")
    if not await _record_mutation(
        session, aggregate_id=creative_id, kind=action, tenant_id=tenant_id,
        idempotency_key=idempotency_key, payload=body.model_dump(mode="json"), principal=principal,
        correlation_id=request.state.correlation_id, aggregate_type="creative",
    ):
        return _creative_response(row)
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if row.approval_state != from_state:
        raise HTTPException(status_code=409, detail="invalid_creative_state")
    if action in {"creative.approve", "creative.reject"}:
        if row.approval_requested_by == principal.subject:
            raise HTTPException(status_code=409, detail="approval_separation_of_duties_required")
        if not row.approval_requested_by:
            raise HTTPException(status_code=409, detail="approval_requester_missing")
    row.approval_state = to_state
    row.approval_requested_by = principal.subject if action == "creative.submit_for_approval" else None
    row.resource_version += 1
    await session.commit()
    await session.refresh(row)
    return _creative_response(row)


@router.post("/creatives/{creative_id}/submit-for-approval", response_model=Creative)
async def submit_creative_for_approval(
    creative_id: UUID, body: ApprovalAction, request: Request, x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader, session: AsyncSession = Depends(get_session),
) -> Creative:
    return await _transition_creative(
        creative_id, body, request, x_tenant_id, idempotency_key, session,
        action="creative.submit_for_approval", required_scope="marketing.write",
        from_state="draft", to_state="pending_approval",
    )


@router.post("/creatives/{creative_id}/approve", response_model=Creative)
async def approve_creative(
    creative_id: UUID, body: ApprovalAction, request: Request, x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader, session: AsyncSession = Depends(get_session),
) -> Creative:
    return await _transition_creative(
        creative_id, body, request, x_tenant_id, idempotency_key, session,
        action="creative.approve", required_scope="marketing.approve",
        from_state="pending_approval", to_state="approved",
    )


@router.post("/creatives/{creative_id}/reject", response_model=Creative)
async def reject_creative(
    creative_id: UUID, body: ApprovalAction, request: Request, x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader, session: AsyncSession = Depends(get_session),
) -> Creative:
    return await _transition_creative(
        creative_id, body, request, x_tenant_id, idempotency_key, session,
        action="creative.reject", required_scope="marketing.approve",
        from_state="pending_approval", to_state="draft",
    )


@router.post("/attribution/touches", response_model=AttributionTouch, status_code=201)
async def create_attribution_touch(
    body: AttributionTouchCreate,
    request: Request,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> AttributionTouchModel:
    principal = _principal(request)
    principal.require("marketing.attribution.write")
    if body.occurred_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="occurred_at_timezone_required")
    if body.campaign_id is not None:
        campaign = await session.scalar(
            select(CampaignModel.id).where(
                CampaignModel.id == body.campaign_id,
                CampaignModel.tenant_id == x_tenant_id,
            )
        )
        if campaign is None:
            # Tenant-scoped not-found avoids leaking another tenant's campaign.
            raise HTTPException(status_code=404, detail="campaign_not_found")
    metadata_json = json.dumps(body.metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(metadata_json.encode()) > 16_384:
        raise HTTPException(status_code=413, detail="attribution_metadata_too_large")
    payload = body.model_dump(mode="json")
    fingerprint = _fingerprint("attribution.touch", x_tenant_id, payload)
    existing = await session.scalar(
        select(AttributionTouchModel).where(
            AttributionTouchModel.tenant_id == x_tenant_id,
            AttributionTouchModel.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return existing
    row = AttributionTouchModel(
        tenant_id=x_tenant_id,
        event_id=body.event_id,
        lead_id=body.lead_id,
        campaign_id=body.campaign_id,
        channel=body.channel,
        occurred_at=body.occurred_at,
        metadata_hash=hashlib.sha256(metadata_json.encode()).hexdigest(),
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    session.add(row)
    await session.flush()
    session.add(
        AuditEventModel(
            tenant_id=x_tenant_id,
            operation_id=None,
            aggregate_type="attribution_touch",
            aggregate_id=row.id,
            action="attribution.touch.accepted",
            outcome="completed",
            actor_id=principal.subject,
            correlation_id=request.state.correlation_id,
            detail_json=json.dumps({"channel": body.channel}, separators=(",", ":")),
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        prior = await session.scalar(
            select(AttributionTouchModel).where(
                AttributionTouchModel.tenant_id == x_tenant_id,
                AttributionTouchModel.idempotency_key == idempotency_key,
            )
        )
        if prior is None or prior.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="attribution_event_conflict") from exc
        return prior
    await session.refresh(row)
    ATTRIBUTION_TOUCHES.inc()
    return row


@router.get("/leads/{lead_id}/attribution", response_model=list[AttributionTouch])
async def get_lead_attribution(
    lead_id: str,
    request: Request,
    x_tenant_id: TenantHeader,
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[AttributionTouchModel]:
    _principal(request).require("marketing.attribution.read")
    rows = await session.scalars(
        select(AttributionTouchModel)
        .where(
            AttributionTouchModel.tenant_id == x_tenant_id,
            AttributionTouchModel.lead_id == lead_id,
        )
        .order_by(AttributionTouchModel.occurred_at.desc(), AttributionTouchModel.id.desc())
        .limit(limit)
    )
    return list(rows)


@router.get("/performance")
async def performance(
    request: Request,
    x_tenant_id: TenantHeader,
    start: datetime,
    end: datetime,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    _principal(request).require("marketing.performance.read")
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise HTTPException(status_code=422, detail="invalid_reporting_window")
    if end - start > timedelta(days=93):
        raise HTTPException(status_code=422, detail="reporting_window_too_large")
    rows = (
        await session.execute(
            select(
                AttributionTouchModel.campaign_id,
                AttributionTouchModel.channel,
                func.count(AttributionTouchModel.id),
            )
            .where(
                AttributionTouchModel.tenant_id == x_tenant_id,
                AttributionTouchModel.occurred_at >= start,
                AttributionTouchModel.occurred_at < end,
            )
            .group_by(AttributionTouchModel.campaign_id, AttributionTouchModel.channel)
            .order_by(AttributionTouchModel.campaign_id, AttributionTouchModel.channel)
            .limit(500)
        )
    ).all()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "items": [
            {
                "campaign_id": str(campaign_id) if campaign_id else None,
                "channel": channel,
                "touch_count": count,
            }
            for campaign_id, channel, count in rows
        ],
    }


@router.get("/providers/meta/accounts/{ad_account_id}/campaigns")
async def meta_campaign_snapshots(
    ad_account_id: str,
    request: Request,
    x_tenant_id: TenantHeader,
) -> list[dict[str, object]]:
    _principal(request).require("marketing.provider.read")
    del x_tenant_id  # tenant is mandatory even though provider credentials remain centrally scoped.
    if not META_READ_SYNC_ENABLED:
        raise HTTPException(status_code=423, detail="meta_read_sync_disabled")
    if ad_account_id not in META_ALLOWED_AD_ACCOUNT_IDS:
        raise HTTPException(status_code=403, detail="meta_ad_account_not_allowlisted")
    snapshots = await MetaReadClient().list_campaigns(ad_account_id)
    return [
        {
            "provider_campaign_id": item.provider_campaign_id,
            "name": item.name,
            "status": item.status,
            "objective": item.objective,
            "daily_budget_minor": item.daily_budget_minor,
        }
        for item in snapshots
    ]


app.include_router(router)
