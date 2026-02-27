import os
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Header, HTTPException


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    user_id: str
    role: str


def _auth_enabled() -> bool:
    return os.getenv("AUTH_ENABLED", "false").strip().lower() == "true"


def _auth_mode() -> str:
    return os.getenv("AUTH_MODE", "static").strip().lower()


def _allowed_tokens() -> set[str]:
    raw = os.getenv("API_TOKENS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _roles_from_claims(claims: dict[str, Any]) -> list[str]:
    roles: set[str] = set()

    fs_role = claims.get("fs_role")
    if isinstance(fs_role, str) and fs_role:
        roles.add(fs_role.lower())

    realm_access = claims.get("realm_access", {})
    if isinstance(realm_access, dict):
        realm_roles = realm_access.get("roles", [])
        if isinstance(realm_roles, list):
            roles.update(str(item).lower() for item in realm_roles)

    client_id = os.getenv("OIDC_CLIENT_ID", "")
    resource_access = claims.get("resource_access", {})
    if client_id and isinstance(resource_access, dict):
        client_access = resource_access.get(client_id, {})
        if isinstance(client_access, dict):
            client_roles = client_access.get("roles", [])
            if isinstance(client_roles, list):
                roles.update(str(item).lower() for item in client_roles)

    return list(roles)


def _highest_role(roles: list[str], fallback: str = "reader") -> str:
    if "admin" in roles:
        return "admin"
    if "writer" in roles:
        return "writer"
    if "reader" in roles:
        return "reader"
    return fallback


def _parse_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


def _validate_oidc_token(token: str) -> dict[str, Any]:
    jwks_url = os.getenv("OIDC_JWKS_URL")
    if not jwks_url:
        raise HTTPException(status_code=500, detail="OIDC_JWKS_URL is not configured")

    audience = os.getenv("OIDC_AUDIENCE")
    issuer = os.getenv("OIDC_ISSUER")

    try:
        signing_key = jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key
        options = {"verify_aud": bool(audience), "verify_iss": bool(issuer)}
        kwargs: dict[str, Any] = {"algorithms": ["RS256"], "options": options}
        if audience:
            kwargs["audience"] = audience
        if issuer:
            kwargs["issuer"] = issuer
        return jwt.decode(token, signing_key, **kwargs)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid OIDC token: {exc}") from exc


def get_request_context(
    x_tenant_id: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_role: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> RequestContext:
    if not _auth_enabled():
        if not x_tenant_id:
            raise HTTPException(status_code=400, detail="Missing required header: X-Tenant-Id")
        return RequestContext(
            tenant_id=x_tenant_id,
            user_id=x_user_id or "anonymous",
            role=(x_role or "reader").lower(),
        )

    mode = _auth_mode()

    if mode == "oidc":
        token = _parse_bearer(authorization)
        claims = _validate_oidc_token(token)

        tenant_id = str(claims.get("tenant_id") or claims.get("tid") or x_tenant_id or "")
        if not tenant_id:
            raise HTTPException(status_code=401, detail="tenant_id claim/header is required")

        user_id = str(claims.get("preferred_username") or claims.get("sub") or x_user_id or "")
        if not user_id:
            raise HTTPException(status_code=401, detail="user identity claim is required")

        role = _highest_role(_roles_from_claims(claims), fallback=(x_role or "reader").lower())
        return RequestContext(tenant_id=tenant_id, user_id=user_id, role=role)

    token = _parse_bearer(authorization)
    allowed = _allowed_tokens()
    if allowed and token not in allowed:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="Missing required header: X-Tenant-Id")

    user_id = x_user_id or "anonymous"
    if user_id == "anonymous":
        raise HTTPException(status_code=401, detail="Missing required header: X-User-Id")

    return RequestContext(tenant_id=x_tenant_id, user_id=user_id, role=(x_role or "reader").lower())


def require_writer_or_admin(ctx: RequestContext) -> None:
    if ctx.role not in {"writer", "admin"}:
        raise HTTPException(status_code=403, detail="Requires writer or admin role")
