"""No-spend drivers. No API in this module can activate ads or publish content."""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..marketing_policy import AccountPolicy, read_regular_file


class ProviderError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Snapshot:
    provider_id: str
    account_id: str
    configured_status: str
    spend_minor: int | None
    observed_at: str
    evidence_kind: str

    def document(self) -> dict[str, Any]:
        return asdict(self)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SandboxDrafts:
    """Deterministic local simulator, explicitly never provider certification."""

    def __init__(self, policy: AccountPolicy):
        self.policy = policy

    async def create(self, marker: str, prepared: dict[str, Any]) -> str:
        return "sandbox-" + marker.rsplit("-", 1)[-1]

    async def assert_ownership(self, provider_id: str) -> None:
        if not provider_id.startswith("sandbox-"):
            raise ProviderError("provider_campaign_binding_mismatch")

    async def change(self, provider_id: str, action: str) -> None:
        if action not in {"pause", "archive"}:
            raise ProviderError("provider_action_forbidden")

    async def read(self, provider_id: str, *, expected: str) -> Snapshot:
        return Snapshot(provider_id, self.policy.provider_account_id, expected, 0, now_iso(), "local_simulation")

    async def find(self, marker: str) -> str:
        return "sandbox-" + marker.rsplit("-", 1)[-1]

    async def connectivity(self) -> dict[str, str]:
        return {"status": "simulation_only", "evidence_kind": "local_simulation"}


class MetaDrafts:
    """Only paused campaign shells; no ad sets, ads, creatives, or activation writes.

    Meta does not give this path an exactly-once transactional contract. The
    worker journals before calling create and never automatically repeats it.
    """

    def __init__(self, policy: AccountPolicy, *, transport: httpx.AsyncBaseTransport | None = None):
        if policy.provider != "meta" or policy.mode != "draft_only" or not policy.graph_version:
            raise ProviderError("meta_draft_configuration_invalid")
        self.policy, self.transport = policy, transport
        self.origin = f"https://graph.facebook.com/{policy.graph_version}"

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            token = read_regular_file(self.policy.token_file or "", 16384).decode("ascii").strip()
            if not re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", token):
                raise ValueError("invalid_token")
        except (OSError, ValueError):
            raise ProviderError("provider_credentials_unavailable") from None
        try:
            async with asyncio.timeout(20), httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                arguments = {"params": params} if method == "GET" else {"data": params}
                async with client.stream(method, self.origin + path,
                                         headers={"Authorization": f"Bearer {token}"}, **arguments) as response:
                    chunks, length = [], 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > 1048576:
                            raise ProviderError("provider_response_too_large")
                        chunks.append(chunk)
                    if response.status_code != 200:
                        raise ProviderError(f"provider_http_{response.status_code}")
            document = json.loads(b"".join(chunks))
            if not isinstance(document, dict) or "error" in document:
                raise ValueError("invalid_response")
            return document
        except (TimeoutError, httpx.HTTPError, ValueError):
            raise ProviderError("provider_outcome_unknown") from None

    @staticmethod
    def _id(value: Any) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,32}", value) is None:
            raise ProviderError("provider_id_invalid")
        return value

    async def connectivity(self) -> dict[str, str]:
        account = self.policy.provider_account_id
        document = await self._request("GET", f"/act_{account}", {"fields": "account_id,currency,account_status"})
        if document.get("account_id") != account or document.get("currency") != self.policy.currency or document.get("account_status") != 1:
            raise ProviderError("provider_account_mismatch_or_unavailable")
        return {"status": "connected", "evidence_kind": "provider_readback", "observed_at": now_iso()}

    async def create(self, marker: str, prepared: dict[str, Any]) -> str:
        if os.getenv("MARKETING_DRAFT_PROVIDER_WRITES_ENABLED", "false") != "true":
            raise ProviderError("provider_draft_writes_disabled")
        if prepared["objective"] not in {
            "OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT", "OUTCOME_LEADS",
            "OUTCOME_APP_PROMOTION", "OUTCOME_SALES",
        }:
            raise ProviderError("provider_objective_not_supported")
        await self.connectivity()
        document = await self._request("POST", f"/act_{self.policy.provider_account_id}/campaigns", {
            "name": marker, "objective": prepared["objective"], "status": "PAUSED",
            "special_ad_categories": json.dumps(self.policy.special_ad_categories),
        })
        return self._id(document.get("id"))

    async def assert_ownership(self, provider_id: str) -> None:
        provider_id = self._id(provider_id)
        document = await self._request("GET", f"/{provider_id}", {"fields": "id,account_id"})
        if document.get("id") != provider_id or document.get("account_id") != self.policy.provider_account_id:
            raise ProviderError("provider_campaign_binding_mismatch")

    async def change(self, provider_id: str, action: str) -> None:
        if action not in {"pause", "archive"}:
            raise ProviderError("provider_action_forbidden")
        document = await self._request("POST", f"/{self._id(provider_id)}",
                                       {"status": "PAUSED" if action == "pause" else "ARCHIVED"})
        if document.get("success") is not True:
            raise ProviderError("provider_acknowledgement_invalid")

    async def find(self, marker: str) -> str:
        found, after = [], None
        for _ in range(10):
            query = {"fields": "id,name", "limit": 100}
            if after is not None:
                query["after"] = after
            document = await self._request("GET", f"/act_{self.policy.provider_account_id}/campaigns", query)
            rows = document.get("data")
            if not isinstance(rows, list):
                raise ProviderError("provider_lookup_invalid")
            for row in rows:
                if not isinstance(row, dict):
                    raise ProviderError("provider_lookup_invalid")
                if row.get("name") == marker:
                    found.append(self._id(row.get("id")))
            paging = document.get("paging", {})
            if not isinstance(paging, dict):
                raise ProviderError("provider_lookup_invalid")
            if not paging.get("next"):
                if len(found) != 1:
                    raise ProviderError("provider_reconciliation_not_unique")
                return found[0]
            cursors = paging.get("cursors", {})
            candidate = cursors.get("after") if isinstance(cursors, dict) else None
            if not isinstance(candidate, str) or not candidate or len(candidate) > 2048 or candidate == after:
                raise ProviderError("provider_cursor_invalid")
            # Never follow a provider-supplied next URL with credentials.
            after = candidate
        raise ProviderError("provider_reconciliation_scan_incomplete")

    async def read(self, provider_id: str, *, expected: str) -> Snapshot:
        provider_id = self._id(provider_id)
        document = await self._request("GET", f"/{provider_id}", {"fields": "id,account_id,configured_status"})
        if document.get("id") != provider_id or document.get("account_id") != self.policy.provider_account_id:
            raise ProviderError("provider_campaign_binding_mismatch")
        status = document.get("configured_status")
        if status != expected or status not in {"PAUSED", "ARCHIVED"}:
            raise ProviderError("provider_not_in_expected_safe_state")
        insights = await self._request("GET", f"/{provider_id}/insights", {"fields": "spend", "date_preset": "maximum"})
        rows = insights.get("data")
        paging = insights.get("paging", {})
        if not isinstance(rows, list) or len(rows) > 1 or not isinstance(paging, dict) or paging.get("next"):
            raise ProviderError("provider_spend_readback_incomplete")
        # Empty insights are UNKNOWN, not a fabricated zero-spend assertion.
        spend = None
        if rows:
            try:
                value = rows[0]["spend"]
                if not isinstance(value, str) or len(value) > 40:
                    raise ValueError("spend_type_invalid")
                amount = Decimal(value) * 100
                if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value() or amount > 9000000000000000:
                    raise ValueError("spend_invalid")
                spend = int(amount)
            except (ValueError, KeyError, TypeError, InvalidOperation):
                raise ProviderError("provider_spend_readback_invalid") from None
        return Snapshot(provider_id, self.policy.provider_account_id, status, spend, now_iso(), "provider_readback")


def driver(policy: AccountPolicy) -> SandboxDrafts | MetaDrafts:
    return SandboxDrafts(policy) if policy.provider == "sandbox" else MetaDrafts(policy)
