"""Server-owned account policy and closed, no-spend command contracts."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .identifiers import TENANT_PATTERN

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=64, pattern=TENANT_PATTERN)]
Alias = Annotated[str, Field(strict=True, min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
Version = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
Money = Annotated[int, Field(strict=True, ge=0, le=9000000000000000)]


class PolicyError(ValueError):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code, self.status = code, status


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Lifecycle(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class DraftSpec(ClosedModel):
    account: Alias
    source_version: Version
    audience_id: UUID
    creative_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("campaign_date_order_invalid")
        return self


class ProviderCommand(ClosedModel):
    campaign_id: UUID
    action: Literal["create_draft", "approve", "schedule", "activate", "pause", "archive", "readback"]
    expected_version: Annotated[int, Field(strict=True, ge=0, le=2147483647)]
    draft: DraftSpec | None = None

    @model_validator(mode="after")
    def closed_shape(self) -> Self:
        if self.action == "create_draft":
            if self.expected_version != 0 or self.draft is None:
                raise ValueError("draft_requires_spec_and_zero_version")
        elif self.expected_version < 1 or self.draft is not None:
            raise ValueError("transition_requires_version_without_draft")
        return self


class AudiencePolicy(ClosedModel):
    countries: list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = Field(min_length=1, max_length=32)
    age_min: Annotated[int, Field(strict=True, ge=18, le=65)]
    age_max: Annotated[int, Field(strict=True, ge=18, le=65)]

    @model_validator(mode="after")
    def age_order(self) -> Self:
        if self.age_max < self.age_min or len(set(self.countries)) != len(self.countries):
            raise ValueError("audience_bounds_invalid")
        return self


class AccountPolicy(ClosedModel):
    tenant_id: Identifier
    alias: Alias
    provider: Literal["sandbox", "meta"]
    provider_account_id: Annotated[str, Field(pattern=r"^[0-9]{1,32}$", strict=True)]
    mode: Literal["sandbox", "draft_only"]
    currency: Literal["USD", "EUR", "DOP", "GBP", "CAD"]
    max_daily_budget_minor: Money
    max_total_budget_minor: Money
    max_duration_days: Annotated[int, Field(strict=True, ge=1, le=366)]
    max_start_delay_days: Annotated[int, Field(strict=True, ge=1, le=366)]
    audiences: list[UUID] = Field(min_length=1, max_length=1000)
    creatives: list[UUID] = Field(min_length=1, max_length=1000)
    countries: list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = Field(min_length=1, max_length=100)
    minimum_age: Annotated[int, Field(strict=True, ge=18, le=65)] = 18
    objectives: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(min_length=1, max_length=32)
    # These fields are configuration, never accepted in a command or returned by the API.
    token_file: str | None = Field(default=None, min_length=1, max_length=512)
    graph_version: str | None = Field(default=None, pattern=r"^v[0-9]{1,3}\.[0-9]{1,2}$")
    special_ad_categories: list[Literal["CREDIT", "EMPLOYMENT", "FINANCIAL_PRODUCTS_SERVICES", "HOUSING", "ISSUES_ELECTIONS_POLITICS"]] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def provider_mode(self) -> Self:
        if self.provider == "sandbox":
            if self.mode != "sandbox" or self.token_file is not None or self.graph_version is not None:
                raise ValueError("sandbox_must_not_have_credentials")
        elif self.mode != "draft_only" or not self.token_file or not self.graph_version:
            raise ValueError("meta_requires_draft_only_configuration")
        return self


class PolicyDocument(ClosedModel):
    schema_version: Literal[1]
    accounts: list[AccountPolicy] = Field(max_length=1000)

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        aliases = [(item.tenant_id, item.alias) for item in self.accounts]
        accounts = [(item.provider, item.provider_account_id) for item in self.accounts]
        # A provider account may not silently be shared across tenants or aliases.
        if len(set(aliases)) != len(aliases) or len(set(accounts)) != len(accounts):
            raise ValueError("duplicate_account_binding")
        return self

    def account(self, tenant_id: str, alias: str) -> AccountPolicy:
        for item in self.accounts:
            if item.tenant_id == tenant_id and item.alias == alias:
                return item
        raise PolicyError("account_not_authorized", 403)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def read_regular_file(path: str, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("regular_file_required")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file_too_large")
    return data


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def load_policy() -> PolicyDocument:
    try:
        raw = read_regular_file(os.environ["MARKETING_ACCOUNT_POLICY_FILE"], 1048576)
        return PolicyDocument.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
    except (KeyError, OSError, ValueError):
        raise PolicyError("marketing_policy_unavailable", 503) from None


def binding_hash(policy: AccountPolicy) -> str:
    # Token rotation does not change identity; account reassignment does.
    return digest({"tenant_id": policy.tenant_id, "alias": policy.alias, "provider": policy.provider,
                   "provider_account_id": policy.provider_account_id, "mode": policy.mode,
                   "currency": policy.currency})


def validate_draft(
    policy: AccountPolicy, draft: DraftSpec, campaign: Any, audience: Any, creative: Any,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if any(row is None or row.tenant_id != policy.tenant_id for row in (campaign, audience, creative)):
        raise PolicyError("resource_not_found", 404)
    if campaign.resource_version != draft.source_version:
        raise PolicyError("source_version_stale", 409)
    if audience.id not in policy.audiences or creative.id not in policy.creatives:
        raise PolicyError("asset_not_authorized", 403)
    if creative.approval_state != "approved":
        raise PolicyError("content_not_approved", 409)
    budget = campaign.daily_budget_minor
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0 or budget > policy.max_daily_budget_minor:
        raise PolicyError("daily_budget_cap_exceeded", 403)
    if campaign.currency != policy.currency or campaign.objective not in policy.objectives:
        raise PolicyError("campaign_policy_mismatch", 403)
    seconds = (draft.ends_at - draft.starts_at).total_seconds()
    # UTC touched calendar days are conservatively reserved, including partial days.
    start = draft.starts_at.astimezone(timezone.utc)
    end = draft.ends_at.astimezone(timezone.utc)
    days = (end.date() - start.date()).days + (1 if end.time().isoformat() != "00:00:00" else 0)
    days = max(1, days)
    if days * budget > policy.max_total_budget_minor:
        raise PolicyError("total_budget_cap_exceeded", 403)
    if draft.starts_at < now or seconds > policy.max_duration_days * 86400 or (draft.starts_at - now).total_seconds() > policy.max_start_delay_days * 86400:
        raise PolicyError("campaign_date_bounds_exceeded", 403)
    try:
        targeting = AudiencePolicy.model_validate(json.loads(audience.definition_json))
    except (ValueError, TypeError):
        raise PolicyError("audience_policy_invalid", 403) from None
    if not set(targeting.countries).issubset(policy.countries) or targeting.age_min < policy.minimum_age:
        raise PolicyError("audience_policy_denied", 403)
    return {
        "campaign_id": str(campaign.id), "source_version": campaign.resource_version,
        "name": campaign.name, "objective": campaign.objective, "currency": campaign.currency,
        "daily_budget_minor": budget, "reserved_total_minor": days * budget,
        "audience_id": str(audience.id), "audience_version": audience.resource_version,
        "audience_hash": digest(json.loads(audience.definition_json)),
        "creative_id": str(creative.id), "creative_version": creative.resource_version,
        "creative_hash": digest(json.loads(creative.content_json)),
        "starts_at": draft.starts_at.isoformat(), "ends_at": draft.ends_at.isoformat(),
    }


def next_state(current: str, action: str) -> str:
    if action == "activate":
        raise PolicyError("live_advertising_activation_not_authorized", 423)
    allowed = {
        "approve": {"draft": "approved"},
        "schedule": {"approved": "scheduled"},
        "pause": {state: "paused" for state in ("draft", "approved", "scheduled", "active", "paused")},
        "archive": {state: "archived" for state in ("draft", "approved", "scheduled", "active", "paused", "archived")},
        "readback": {state.value: state.value for state in Lifecycle},
    }
    target = allowed.get(action, {}).get(current)
    if target is None:
        raise PolicyError("invalid_campaign_transition", 409)
    return target
