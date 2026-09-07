"""Shared HTTP-safe tenant alphabet, including slash and email-style identifiers."""
import re

TENANT_PATTERN = r"^[!-~]{1,64}$"


def valid_tenant_id(value: str) -> bool:
    return re.fullmatch(TENANT_PATTERN, value) is not None
