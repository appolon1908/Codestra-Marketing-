"""Authenticated Middleware-only commands; never expose this prefix at public ingress."""
from __future__ import annotations

import json
import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from .auth import Principal, authenticate
from .db import SessionLocal
from .marketing_policy import PolicyError, ProviderCommand, load_policy
from .provider_models import ProviderCampaignModel, ProviderCommandModel
from .provider_service import submit
from .providers.guarded_drafts import ProviderError, driver

router = APIRouter(prefix="/internal/v1/marketing", tags=["private-marketing"], include_in_schema=False)


def get_session_factory():
    return SessionLocal


async def middleware_principal(principal: Principal = Depends(authenticate)) -> Principal:
    clients = {value.strip() for value in os.getenv("MARKETING_COMMAND_CLIENT_IDS", "").split(",") if value.strip()}
    if not clients:
        raise HTTPException(503, "marketing_command_identity_unconfigured")
    if principal.client_id not in clients:
        raise HTTPException(403, "middleware_client_required")
    return principal


@router.post("/commands", status_code=202)
async def commands(
    command: ProviderCommand,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200, pattern=r"^[!-~]+$")],
    correlation: Annotated[str, Header(alias="X-Correlation-ID", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")],
    principal: Principal = Depends(middleware_principal),
    sessions=Depends(get_session_factory),
):
    try:
        return await submit(command, principal, idempotency_key, correlation, session_factory=sessions)
    except PolicyError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.get("/campaigns/{campaign_id}")
async def read_campaign(campaign_id: UUID, principal: Principal = Depends(middleware_principal),
                        sessions=Depends(get_session_factory)):
    principal.require("marketing.provider.read")
    async with sessions() as session:
        row = await session.scalar(select(ProviderCampaignModel).where(
            ProviderCampaignModel.campaign_id == campaign_id, ProviderCampaignModel.tenant_id == principal.tenant_id,
        ))
        if row is None:
            raise HTTPException(404, "provider_campaign_not_found")
        return {"campaign_id": str(row.campaign_id), "account": row.account_alias, "state": row.state,
                "resource_version": row.resource_version, "prepared": json.loads(row.prepared_json),
                "pending_operation_id": str(row.pending_command_id) if row.pending_command_id else None,
                "provider_snapshot": json.loads(row.snapshot_json) if row.snapshot_json else None,
                "live_advertising": False, "publication_enabled": False}


@router.get("/operations/{operation_id}")
async def read_operation(operation_id: UUID, principal: Principal = Depends(middleware_principal),
                         sessions=Depends(get_session_factory)):
    principal.require("marketing.provider.read")
    async with sessions() as session:
        row = await session.scalar(select(ProviderCommandModel).where(
            ProviderCommandModel.id == operation_id, ProviderCommandModel.tenant_id == principal.tenant_id,
        ))
        if row is None:
            raise HTTPException(404, "provider_operation_not_found")
        return {**json.loads(row.receipt_json), "state": row.state, "error_code": row.error_code}


@router.get("/accounts/{account}/connectivity")
async def connectivity(account: str, principal: Principal = Depends(middleware_principal)):
    principal.require("marketing.provider.read")
    try:
        policy = load_policy().account(principal.tenant_id, account)
        return await driver(policy).connectivity()
    except PolicyError as exc:
        raise HTTPException(exc.status, exc.code) from None
    except ProviderError as exc:
        raise HTTPException(503, exc.code) from None


@router.get("/status")
async def status(principal: Principal = Depends(middleware_principal), sessions=Depends(get_session_factory)):
    principal.require("marketing.provider.read")
    try:
        policy = load_policy()
    except PolicyError as exc:
        raise HTTPException(exc.status, exc.code) from None
    try:
        async with sessions() as session:
            unresolved = await session.scalar(select(func.count()).select_from(ProviderCommandModel).where(
                ProviderCommandModel.tenant_id == principal.tenant_id,
                ProviderCommandModel.state == "reconciliation_required",
            ))
    except SQLAlchemyError:
        raise HTTPException(503, "provider_journal_unavailable") from None
    account_count = sum(item.tenant_id == principal.tenant_id for item in policy.accounts)
    if not account_count:
        raise HTTPException(503, "tenant_provider_account_unconfigured")
    return {"status": "ready" if not unresolved else "reconciliation_required",
            "account_count": account_count,
            "unresolved_operations": unresolved, "live_advertising": False, "publication_enabled": False,
            "provider_connectivity": "check_account_endpoint", "spend_counter": "check_campaign_snapshot",
            "source_sha": os.getenv("CODESTRA_GIT_SHA", "unknown"),
            "image_digest": os.getenv("CODESTRA_IMAGE_DIGEST", "unknown")}
