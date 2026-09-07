import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_authority", ROOT / "scripts" / "validate_production_runtime_authority.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def authority():
    return json.loads(MODULE.AUTHORITY.read_text())


def rejected(authority, mutation):
    candidate = copy.deepcopy(authority)
    mutation(candidate)
    with pytest.raises(ValueError):
        MODULE.validate(candidate)


def test_current_unverified_off_host_record_is_valid(authority):
    MODULE.validate(authority)


def test_observation_timestamp_is_recorded_in_utc(authority):
    assert authority["recorded_at"] == "2026-09-01T18:05:43Z"


def test_cannot_claim_production_ready(authority):
    rejected(authority, lambda row: row.__setitem__("production_ready", True))


def test_cannot_authorize_deployment_or_writes(authority):
    rejected(authority, lambda row: row.__setitem__("deployment_authorized", True))
    rejected(authority, lambda row: row.__setitem__("production_writes_authorized", True))


def test_cannot_hide_unknown_live_safety_with_gate(authority):
    rejected(
        authority,
        lambda row: row["required_gates"].__setitem__(
            "live_advertising_readback_false", True
        ),
    )


def test_runtime_location_must_match_observed_ip(authority):
    rejected(authority, lambda row: row.__setitem__("runtime_location", "65.109.65.169"))


def test_source_capabilities_remain_fail_closed(authority):
    rejected(
        authority,
        lambda row: row["source_capabilities"].__setitem__(
            "live_advertising_default", True
        ),
    )


def test_runtime_git_sha_must_equal_protected_source(authority):
    def mutate(row):
        row["runtime_evidence"]["git_sha"] = "a" * 40
        row["required_gates"]["protected_git_sha_exposed"] = True

    rejected(authority, mutate)


def test_matching_protected_runtime_sha_can_satisfy_only_its_gate(authority):
    candidate = copy.deepcopy(authority)
    candidate["runtime_evidence"]["git_sha"] = candidate["protected_source_sha"]
    candidate["required_gates"]["protected_git_sha_exposed"] = True
    MODULE.validate(candidate)


def test_main_reports_the_validated_runtime_location(
    authority, tmp_path, monkeypatch, capsys
):
    candidate = copy.deepcopy(authority)
    candidate["observed_ipv4"] = "203.0.113.10"
    candidate["runtime_location"] = "203.0.113.10"
    authority_path = tmp_path / "production-runtime-authority.json"
    authority_path.write_text(json.dumps(candidate))
    monkeypatch.setattr(MODULE, "AUTHORITY", authority_path)

    assert MODULE.main() == 0
    output = capsys.readouterr().out
    assert "MARKETING_RUNTIME_LOCATION=203.0.113.10" in output
    assert "MARKETING_PRODUCTION_AUTHORITY=FAIL" in output
    assert "PRODUCTION_WRITES_AUTHORIZED=NO" in output
