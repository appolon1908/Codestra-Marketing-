from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


ACTIVATION_PATH = "/api/v1/control/marketing/campaign-activations"
TRANSITION_PATH = "/api/v1/control/marketing/campaign-transitions"


class MiddlewareDeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class MiddlewareMarketingClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("MIDDLEWARE_BASE_URL", "").rstrip("/")
        self.token_file = os.getenv("MIDDLEWARE_TOKEN_FILE", "")
        self.timeout = max(0.5, min(float(os.getenv("MIDDLEWARE_TIMEOUT_SECONDS", "5")), 30.0))

    def _endpoint(self, action: str = "activate") -> str:
        parsed = urlsplit(self.base_url)
        loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            not (parsed.scheme == "https" or loopback)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise MiddlewareDeliveryError("middleware_base_url_invalid", retryable=False)
        path = ACTIVATION_PATH if action == "activate" else TRANSITION_PATH
        return f"{self.base_url}{path}"

    def _token(self) -> str:
        path = Path(self.token_file)
        if not self.token_file or not path.is_file() or path.is_symlink():
            raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False)
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise MiddlewareDeliveryError("middleware_token_empty", retryable=False)
        return token

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-Tenant-ID": str(payload["tenant_id"]),
            "X-Correlation-ID": str(payload["correlation_id"]),
            "Idempotency-Key": str(payload["operation_id"]),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self._endpoint(str(payload.get("action", "activate"))), headers=headers, json=payload
                )
        except httpx.TransportError as exc:
            raise MiddlewareDeliveryError("middleware_outcome_unknown", retryable=True) from exc
        if response.status_code not in {200, 202}:
            raise MiddlewareDeliveryError(
                f"middleware_rejected_{response.status_code}",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            document = response.json()
            operation_id = str(document["operation_id"])
            state = str(document["state"])
        except (ValueError, KeyError, TypeError) as exc:
            raise MiddlewareDeliveryError("middleware_response_invalid", retryable=True) from exc
        return {"operation_id": operation_id, "state": state}
