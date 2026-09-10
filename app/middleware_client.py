from __future__ import annotations

import math
import os
import re
import stat
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .delivery_contract import ACTION_PATHS, ACTIVATION_PATH, TRANSITION_PATH, MarketingCommand


MAX_TOKEN_BYTES = 16384
TOKEN_RE = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Acceptance is not evidence that an external provider changed state.
ACCEPTED_STATES = frozenset({"accepted", "pending", "queued", "processing", "completed", "succeeded"})


class MiddlewareDeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class MiddlewareMarketingClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = os.getenv("MIDDLEWARE_BASE_URL", "").rstrip("/")
        self.token_file = os.getenv("MIDDLEWARE_TOKEN_FILE", "")
        self.transport = transport
        try:
            timeout = float(os.getenv("MIDDLEWARE_TIMEOUT_SECONDS", "5"))
            if not math.isfinite(timeout):
                raise ValueError("non_finite_timeout")
        except ValueError as exc:
            raise MiddlewareDeliveryError("middleware_timeout_invalid", retryable=False) from exc
        self.timeout = max(0.5, min(timeout, 30.0))

    def _endpoint(self, action: str = "activate") -> str:
        if action not in ACTION_PATHS:
            raise MiddlewareDeliveryError("marketing_action_invalid", retryable=False)
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port  # Force validation: urlsplit alone accepts invalid ports.
            loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            valid = (
                bool(self.base_url)
                and all(33 <= ord(character) <= 126 for character in self.base_url)
                and "\\" not in self.base_url
                and (parsed.scheme == "https" or loopback)
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.path == ""
                and not parsed.query
                and not parsed.fragment
                and (port is None or port > 0)
            )
        except ValueError:
            valid = False
        if not valid:
            raise MiddlewareDeliveryError("middleware_base_url_invalid", retryable=False)
        return f"{self.base_url}{ACTION_PATHS[action]}"

    def _token(self) -> str:
        if not self.token_file:
            raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False)
        try:
            # Refuse symlinks and non-regular files without blocking on a FIFO.
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            descriptor = os.open(self.token_file, flags)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False)
                raw = stream.read(MAX_TOKEN_BYTES + 1)
            if len(raw) > MAX_TOKEN_BYTES:
                raise MiddlewareDeliveryError("middleware_token_invalid", retryable=False)
            token = raw.decode("ascii").strip()
        except (OSError, UnicodeError, ValueError) as exc:
            raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False) from exc
        if not token:
            raise MiddlewareDeliveryError("middleware_token_empty", retryable=False)
        if TOKEN_RE.fullmatch(token) is None:
            raise MiddlewareDeliveryError("middleware_token_invalid", retryable=False)
        return token

    @staticmethod
    def _receipt(document: Any, command: MarketingCommand) -> dict[str, str]:
        if not isinstance(document, dict):
            raise MiddlewareDeliveryError("middleware_response_invalid", retryable=True)
        operation_id, state = document.get("operation_id"), document.get("state")
        if (
            not isinstance(operation_id, str)
            or RECEIPT_ID_RE.fullmatch(operation_id) is None
            or not isinstance(state, str)
            or state not in ACCEPTED_STATES
        ):
            raise MiddlewareDeliveryError("middleware_response_invalid", retryable=True)
        # Receivers may issue a different durable operation ID. Optional echo
        # fields, when present, must still bind to the request we actually sent.
        for field, expected in (
            ("tenant_id", command.tenant_id),
            ("campaign_id", str(command.campaign_id)),
            ("correlation_id", command.correlation_id),
        ):
            if field in document and document[field] != expected:
                raise MiddlewareDeliveryError("middleware_response_mismatch", retryable=True)
        return {"operation_id": operation_id, "state": state}

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            command = MarketingCommand.model_validate(payload)
        except ValidationError:
            # Do not leak command bodies or credentials through validation errors.
            raise MiddlewareDeliveryError("marketing_command_invalid", retryable=False) from None
        endpoint = self._endpoint(command.action)
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-Tenant-ID": command.tenant_id,
            "X-Correlation-ID": command.correlation_id,
            "Idempotency-Key": str(command.operation_id),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport,
                follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.post(endpoint, headers=headers, json=command.as_payload())
        except httpx.RequestError as exc:
            raise MiddlewareDeliveryError("middleware_outcome_unknown", retryable=True) from exc
        except (httpx.InvalidURL, ValueError) as exc:
            raise MiddlewareDeliveryError("middleware_request_invalid", retryable=False) from exc
        if response.status_code not in {200, 202}:
            raise MiddlewareDeliveryError(
                f"middleware_rejected_{response.status_code}",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
            )
        try:
            document = response.json()
        except ValueError:
            raise MiddlewareDeliveryError("middleware_response_invalid", retryable=True) from None
        return self._receipt(document, command)
