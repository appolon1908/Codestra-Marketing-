from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import auth
from app.main import _bind_actor, app


class _Request:
    def __init__(self) -> None:
        self.state = SimpleNamespace()


class _Key:
    key = object()


class _Jwks:
    def get_signing_key_from_jwt(self, token: str) -> _Key:
        assert token == "signed-token"
        return _Key()


@pytest.mark.asyncio
async def test_verified_keycloak_claims_bind_principal_to_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://identity.example/realms/codestra")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "marketing-service")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_CLIENT_IDS", "codestra-console,marketing-service")
    monkeypatch.setattr(auth, "_jwk_client", lambda _url: _Jwks())
    monkeypatch.setattr(
        auth.jwt,
        "decode",
        lambda *_args, **_kwargs: {
            "sub": "operator-1",
            "tenant_id": "tenant-1",
            "scope": "marketing.read marketing.write",
            "azp": "codestra-console",
        },
    )
    request = _Request()
    principal = await auth.authenticate(
        request,  # type: ignore[arg-type]
        "tenant-1",
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token"),
    )
    assert principal.subject == "operator-1"
    assert principal.tenant_id == "tenant-1"
    assert principal.scopes == frozenset({"marketing.read", "marketing.write"})
    assert request.state.principal == principal


@pytest.mark.asyncio
async def test_tenant_header_must_match_verified_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://identity.example/realms/codestra")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "marketing-service")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_CLIENT_IDS", "codestra-console,marketing-service")
    monkeypatch.setattr(auth, "_jwk_client", lambda _url: _Jwks())
    monkeypatch.setattr(
        auth.jwt,
        "decode",
        lambda *_args, **_kwargs: {"sub": "operator-1", "tenant_id": "tenant-1", "scope": "marketing.read"},
    )
    with pytest.raises(HTTPException, match="tenant_mismatch") as denied:
        await auth.authenticate(
            _Request(),  # type: ignore[arg-type]
            "tenant-2",
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token"),
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_spoofed_scope_header_without_bearer_is_rejected() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/marketing/campaigns",
            headers={"X-Tenant-ID": "tenant-1", "X-Codestra-Verified-Scopes": "marketing.read marketing.write"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "bearer_token_required"


def test_principal_scope_is_fail_closed() -> None:
    principal = auth.Principal("operator-1", "tenant-1", frozenset({"marketing.read"}), None)
    with pytest.raises(HTTPException, match="required_scope_missing") as denied:
        principal.require("marketing.write")
    assert denied.value.status_code == 403


def test_request_body_cannot_spoof_approval_actor() -> None:
    principal = auth.Principal("operator-1", "tenant-1", frozenset({"marketing.approve"}), None)
    with pytest.raises(HTTPException, match="actor_identity_mismatch") as denied:
        _bind_actor(principal, "different-operator")
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_unapproved_authorized_party_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://identity.example/realms/codestra")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "marketing-service")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_CLIENT_IDS", "codestra-console")
    monkeypatch.setattr(auth, "_jwk_client", lambda _url: _Jwks())
    monkeypatch.setattr(
        auth.jwt,
        "decode",
        lambda *_args, **_kwargs: {
            "sub": "operator-1",
            "tenant_id": "tenant-1",
            "scope": "marketing.read",
            "azp": "unapproved-client",
        },
    )
    with pytest.raises(HTTPException, match="client_not_authorized") as denied:
        await auth.authenticate(
            _Request(),  # type: ignore[arg-type]
            "tenant-1",
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token"),
        )
    assert denied.value.status_code == 403
