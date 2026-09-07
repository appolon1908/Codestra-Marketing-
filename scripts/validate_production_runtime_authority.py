#!/usr/bin/env python3
"""Validate an honest fail-closed Marketing runtime authority record."""

import ipaddress
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = ROOT / "config" / "production-runtime-authority.json"
EXPECTED_GATES = {
    "runtime_owner_attested",
    "tls_valid",
    "health_reachable",
    "immutable_image_digest_exposed",
    "protected_git_sha_exposed",
    "live_advertising_readback_false",
    "gateway_path_verified",
    "rollback_target_verified",
}


def validate(record: dict) -> None:
    if record.get("schema_version") != 1:
        raise ValueError("unexpected runtime authority schema")
    if record.get("repository") != "appolon1908-hue/Codestra-Marketing-":
        raise ValueError("unexpected repository authority")
    protected_source_sha = record.get("protected_source_sha")
    if protected_source_sha != "460ff98f64ef9f0724fe4d2afc51a1a6c5b053dd":
        raise ValueError("protected_source_sha differs from the reviewed source base")
    if record.get("public_hostname") != "marketing.codestra.co":
        raise ValueError("unexpected public hostname")
    observed = str(ipaddress.ip_address(record.get("observed_ipv4", "")))
    if record.get("runtime_location") != observed:
        raise ValueError("runtime location must equal the observed IPv4")

    source = record.get("source_capabilities", {})
    if source != {
        "live_advertising_default": False,
        "meta_read_sync_default": False,
        "provider_activation_implemented": False,
    }:
        raise ValueError("source capability baseline must remain fail closed")

    gates = record.get("required_gates", {})
    if set(gates) != EXPECTED_GATES or any(
        type(value) is not bool for value in gates.values()
    ):
        raise ValueError("required production gates must be an exact boolean map")
    runtime = record.get("runtime_evidence", {})
    runtime_git_sha = runtime.get("git_sha", "")
    derived_gates = {
        "tls_valid": runtime.get("tls_valid") is True,
        "health_reachable": runtime.get("health_reachable") is True,
        "immutable_image_digest_exposed": bool(
            re.fullmatch(r"sha256:[0-9a-f]{64}", runtime.get("image_digest", ""))
        ),
        "protected_git_sha_exposed": bool(
            re.fullmatch(r"[0-9a-f]{40}", runtime_git_sha)
            and runtime_git_sha == protected_source_sha
        ),
        "live_advertising_readback_false": runtime.get("live_advertising_enabled")
        is False,
    }
    for gate, derived in derived_gates.items():
        if gates[gate] is not derived:
            raise ValueError(f"{gate} differs from runtime evidence")
    for gate in (
        "runtime_owner_attested",
        "gateway_path_verified",
        "rollback_target_verified",
    ):
        if gates[gate] is not False:
            raise ValueError(f"{gate} lacks an evidence reference")
    all_gates_pass = all(gates.values())
    if record.get("production_ready") is not all_gates_pass:
        raise ValueError("production_ready must equal the conjunction of required gates")
    if record.get("deployment_authorized") is not False:
        raise ValueError("this evidence record does not authorize deployment")
    if record.get("production_writes_authorized") is not False:
        raise ValueError("production business writes must remain unauthorized")
    if record.get("authority_status") != "UNVERIFIED_OFF_HOST":
        raise ValueError("current authority status must remain UNVERIFIED_OFF_HOST")
    if all_gates_pass:
        raise ValueError("this source-only change cannot certify the off-host runtime")


def main() -> int:
    record = json.loads(AUTHORITY.read_text())
    validate(record)
    print(f"MARKETING_RUNTIME_LOCATION={record['runtime_location']}")
    print("MARKETING_PRODUCTION_AUTHORITY=FAIL")
    print("PRODUCTION_WRITES_AUTHORIZED=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
