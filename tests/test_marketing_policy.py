from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from app.marketing_policy import (
    AccountPolicy, DraftSpec, PolicyDocument, PolicyError, ProviderCommand,
    binding_hash, load_policy, next_state, validate_draft,
)


@pytest.fixture
def policy_case():
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    audience_id, creative_id, campaign_id = uuid4(), uuid4(), uuid4()
    policy = AccountPolicy(
        tenant_id="team@example.com", alias="test-account", provider="sandbox", provider_account_id="100",
        mode="sandbox", currency="USD", max_daily_budget_minor=1000, max_total_budget_minor=10000,
        max_duration_days=30, max_start_delay_days=60, audiences=[audience_id], creatives=[creative_id],
        countries=["DO", "US"], objectives=["OUTCOME_LEADS"],
    )
    spec = DraftSpec(account="test-account", source_version=1, audience_id=audience_id, creative_id=creative_id,
                     starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=3))
    source = SimpleNamespace(id=campaign_id, tenant_id=policy.tenant_id, name="Draft", objective="OUTCOME_LEADS",
                             currency="USD", daily_budget_minor=1000, resource_version=1)
    audience = SimpleNamespace(id=audience_id, tenant_id=policy.tenant_id, resource_version=1,
                                definition_json=json.dumps({"countries": ["DO"], "age_min": 21, "age_max": 65}))
    creative = SimpleNamespace(id=creative_id, tenant_id=policy.tenant_id, resource_version=1,
                                approval_state="approved", content_json='{"headline":"Test"}')
    return policy, spec, source, audience, creative, now


def validate(case):
    policy, spec, source, audience, creative, now = case
    return validate_draft(policy, spec, source, audience, creative, now=now)


def test_policy_accepts_bound_approved_draft_and_conservative_budget(policy_case):
    result = validate(policy_case)
    assert result["reserved_total_minor"] == 2000
    assert result["creative_hash"] and result["audience_hash"]
    assert "token_file" not in result and "provider_account_id" not in result


@pytest.mark.parametrize("field,value,error", [
    ("daily_budget_minor", -1, "daily_budget_cap_exceeded"),
    ("daily_budget_minor", True, "daily_budget_cap_exceeded"),
    ("daily_budget_minor", 1001, "daily_budget_cap_exceeded"),
    ("currency", "EUR", "campaign_policy_mismatch"),
    ("objective", "arbitrary", "campaign_policy_mismatch"),
    ("resource_version", 2, "source_version_stale"),
    ("tenant_id", "other", "resource_not_found"),
])
def test_campaign_policy_denials(policy_case, field, value, error):
    setattr(policy_case[2], field, value)
    with pytest.raises(PolicyError, match=error):
        validate(policy_case)


@pytest.mark.parametrize("which", [3, 4])
def test_assets_are_tenant_isolated(policy_case, which):
    policy_case[which].tenant_id = "other"
    with pytest.raises(PolicyError, match="resource_not_found"):
        validate(policy_case)


@pytest.mark.parametrize("which", [3, 4])
def test_assets_must_be_individually_allowlisted(policy_case, which):
    policy_case[which].id = uuid4()
    with pytest.raises(PolicyError, match="asset_not_authorized"):
        validate(policy_case)


@pytest.mark.parametrize("state", ["draft", "pending", "rejected", "invalidated"])
def test_content_must_be_approved(policy_case, state):
    policy_case[4].approval_state = state
    with pytest.raises(PolicyError, match="content_not_approved"):
        validate(policy_case)


@pytest.mark.parametrize("definition", [
    {"countries": ["FR"], "age_min": 18, "age_max": 65},
    {"countries": ["DO"], "age_min": 17, "age_max": 65},
    {"countries": ["DO"], "age_min": 65, "age_max": 18},
    {"countries": ["DO"], "age_min": 18, "age_max": 65, "custom_audiences": ["unapproved"]},
    {"countries": ["DO"], "age_min": "18", "age_max": 65},
    {}, [],
])
def test_targeting_is_closed_and_restricted(policy_case, definition):
    policy_case[3].definition_json = json.dumps(definition)
    with pytest.raises(PolicyError, match="audience_policy"):
        validate(policy_case)


@pytest.mark.parametrize("start,end", [(-1, 1), (61, 62), (1, 40)])
def test_date_bounds(policy_case, start, end):
    policy, spec, source, audience, creative, now = policy_case
    policy = policy.model_copy(update={"max_total_budget_minor": 1000000})
    spec = spec.model_copy(update={"starts_at": now + timedelta(days=start), "ends_at": now + timedelta(days=end)})
    with pytest.raises(PolicyError, match="campaign_date_bounds_exceeded"):
        validate_draft(policy, spec, source, audience, creative, now=now)


def test_total_budget_bound(policy_case):
    policy, spec, source, audience, creative, now = policy_case
    policy = policy.model_copy(update={"max_total_budget_minor": 1999})
    with pytest.raises(PolicyError, match="total_budget_cap_exceeded"):
        validate_draft(policy, spec, source, audience, creative, now=now)


def test_calendar_day_reservation_counts_midnight_crossing(policy_case):
    policy, spec, source, audience, creative, now = policy_case
    spec = spec.model_copy(update={"starts_at": now + timedelta(days=1, hours=23),
                                  "ends_at": now + timedelta(days=2, hours=1)})
    assert validate_draft(policy, spec, source, audience, creative, now=now)["reserved_total_minor"] == 2000


@pytest.mark.parametrize("field,value", [("credentials", "secret"), ("provider_url", "https://evil.example"),
                                          ("account_id", "unrestricted"), ("actor_id", "forged")])
def test_command_rejects_provider_options(policy_case, field, value):
    with pytest.raises(ValidationError):
        ProviderCommand.model_validate({"campaign_id": str(policy_case[2].id), "action": "create_draft",
            "expected_version": 0, "draft": policy_case[1].model_dump(mode="json"), field: value})


@pytest.mark.parametrize("version", [True, 1.0, "1", -1, 2147483648])
def test_command_versions_are_strict(version):
    with pytest.raises(ValidationError):
        ProviderCommand(campaign_id=uuid4(), action="pause", expected_version=version)


def test_command_shape_and_timezone_are_required(policy_case):
    with pytest.raises(ValidationError):
        ProviderCommand(campaign_id=uuid4(), action="create_draft", expected_version=0)
    with pytest.raises(ValidationError):
        ProviderCommand(campaign_id=uuid4(), action="pause", expected_version=1, draft=policy_case[1])
    with pytest.raises(ValidationError):
        DraftSpec.model_validate({**policy_case[1].model_dump(mode="json"), "starts_at": "2026-09-08T00:00:00"})


@pytest.mark.parametrize("state", ["draft", "approved", "scheduled", "active", "paused", "archived"])
def test_activation_is_always_denied(state, monkeypatch):
    monkeypatch.setenv("LIVE_ADVERTISING", "true")
    monkeypatch.setenv("LIVE_ADVERTISING_ENABLED", "true")
    with pytest.raises(PolicyError, match="activation_not_authorized"):
        next_state(state, "activate")


def test_lifecycle_separates_scheduling_from_activation():
    assert next_state("draft", "approve") == "approved"
    assert next_state("approved", "schedule") == "scheduled"
    assert next_state("scheduled", "pause") == "paused"
    assert next_state("paused", "archive") == "archived"
    with pytest.raises(PolicyError):
        next_state("archived", "approve")


def test_account_allowlist_is_tenant_bound_and_cannot_alias_same_provider_account(policy_case):
    policy = policy_case[0]
    document = PolicyDocument(schema_version=1, accounts=[policy])
    with pytest.raises(PolicyError, match="account_not_authorized"):
        document.account("wrong", policy.alias)
    with pytest.raises(ValidationError):
        PolicyDocument(schema_version=1, accounts=[policy, policy.model_copy(update={"tenant_id": "other"})])
    assert binding_hash(policy) != binding_hash(policy.model_copy(update={"provider_account_id": "101"}))


def test_policy_file_is_fail_closed_and_rejects_duplicate_json_keys(policy_case, monkeypatch, tmp_path):
    path = tmp_path / "policy.json"
    monkeypatch.setenv("MARKETING_ACCOUNT_POLICY_FILE", str(path))
    with pytest.raises(PolicyError, match="marketing_policy_unavailable"):
        load_policy()
    path.write_text('{"schema_version":1,"accounts":[],"accounts":[]}')
    with pytest.raises(PolicyError):
        load_policy()
    path.write_text(PolicyDocument(schema_version=1, accounts=[policy_case[0]]).model_dump_json())
    assert load_policy().account("team@example.com", "test-account") == policy_case[0]
    other = tmp_path / "link.json"
    other.symlink_to(path)
    monkeypatch.setenv("MARKETING_ACCOUNT_POLICY_FILE", str(other))
    with pytest.raises(PolicyError):
        load_policy()


def test_private_command_contract_has_no_schema_drift():
    from pathlib import Path
    expected = json.loads((Path(__file__).resolve().parents[1] / "contracts/marketing.commands.v1.json").read_text())
    assert expected == ProviderCommand.model_json_schema()
