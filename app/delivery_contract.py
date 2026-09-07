"""Provider-neutral commands accepted by Marketing's Middleware boundary.

This is deliberately not a provider request model. Account IDs, credentials,
URLs and arbitrary provider options cannot cross this boundary in the payload.
"""
from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identifiers import TENANT_PATTERN

ACTIVATION_PATH = "/api/v1/control/marketing/campaign-activations"
TRANSITION_PATH = "/api/v1/control/marketing/campaign-transitions"
ACTION_PATHS = {
    "activate": ACTIVATION_PATH,
    "pause": TRANSITION_PATH,
    "resume": TRANSITION_PATH,
}

# The normal lifecycle records the target state; an approval-invalidation stop
# deliberately references the previously approved version instead.
OPERATION_CONTRACTS = {
    "campaign.activate": ("activate", "approved", "marketing.campaign.activation_requested"),
    "campaign.pause": ("pause", "paused", "marketing.campaign.pause.requested"),
    "campaign.resume": ("resume", "approved", "marketing.campaign.resume.requested"),
    "campaign.approval_invalidation_stop": (
        "pause", "approved", "marketing.campaign.approval_invalidated_stop_requested"
    ),
}


class MarketingCommand(BaseModel):
    """Closed schema shared by the outbox worker and HTTP client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    campaign_id: UUID
    action: Literal["activate", "pause", "resume"]
    expected_state: Literal["approved", "paused"]
    expected_version: int = Field(strict=True, ge=1, le=2147483647)
    tenant_id: str = Field(strict=True, min_length=1, max_length=64, pattern=TENANT_PATTERN)
    correlation_id: str = Field(strict=True, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.action in {"activate", "resume"} and self.expected_state != "approved":
            raise ValueError("action_state_mismatch")
        return self

    def as_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")
