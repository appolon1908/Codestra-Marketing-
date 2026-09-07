from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    scopes: frozenset[str]
    client_id: str | None

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise HTTPException(status_code=403, detail="required_scope_missing")


def _required_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=503, detail="identity_configuration_unavailable")
    return value


@lru_cache(maxsize=4)
def _jwk_client(jwks_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    values: set[str] = set()
    scope = claims.get("scope")
    if isinstance(scope, str):
        values.update(scope.split())
    permissions = claims.get("permissions")
    if isinstance(permissions, list):
        values.update(value for value in permissions if isinstance(value, str))
    return frozenset(values)


def _tenant_claim(claims: dict[str, Any]) -> str | None:
    for name in ("tenant_id", "tenant"):
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def authenticate(
    request: Request,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="bearer_token_required")

    issuer = _required_setting("KEYCLOAK_ISSUER").rstrip("/")
    audience = _required_setting("KEYCLOAK_AUDIENCE")
    jwks_url = os.getenv("KEYCLOAK_JWKS_URL", f"{issuer}/protocol/openid-connect/certs").strip()
    try:
        key = _jwk_client(jwks_url).get_signing_key_from_jwt(credentials.credentials)
        claims = jwt.decode(
            credentials.credentials,
            key.key,
            algorithms=["RS256", "PS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid_access_token") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="identity_verification_unavailable") from exc

    subject = claims.get("sub")
    tenant_id = _tenant_claim(claims)
    if not isinstance(subject, str) or not subject or tenant_id is None:
        raise HTTPException(status_code=403, detail="required_identity_claim_missing")
    if x_tenant_id is None or x_tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant_mismatch")

    client_id = claims.get("azp")
    allowed_clients = {
        value.strip()
        for value in _required_setting("KEYCLOAK_ALLOWED_CLIENT_IDS").split(",")
        if value.strip()
    }
    if not isinstance(client_id, str) or client_id not in allowed_clients:
        raise HTTPException(status_code=403, detail="client_not_authorized")
    principal = Principal(
        subject=subject,
        tenant_id=tenant_id,
        scopes=_scopes(claims),
        client_id=client_id,
    )
    request.state.principal = principal
    return principal
