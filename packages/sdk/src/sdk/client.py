from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib import error, parse, request

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None


class OIDCTokenProvider:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        timeout: int = 20,
    ):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.timeout = timeout
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        now = time.time()
        if self._access_token and now < (self._expires_at - 30):
            return self._access_token

        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            payload["scope"] = self.scope

        if requests:
            response = requests.post(self.token_url, data=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        else:
            body = parse.urlencode(payload).encode("utf-8")
            req = request.Request(
                url=self.token_url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except error.HTTPError as exc:
                details = exc.read().decode("utf-8")
                raise RuntimeError(
                    f"OIDC token request failed ({exc.code}): {details}"
                ) from exc

        token = data.get("access_token")
        if not token:
            raise RuntimeError("OIDC token response missing access_token")

        expires_in = int(data.get("expires_in", 300))
        self._access_token = token
        self._expires_at = time.time() + expires_in
        return token


class _BaseClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 20,
        tenant_id: str | None = None,
        user_id: str | None = None,
        role: str | None = None,
        auth_token: str | None = None,
        token_provider: OIDCTokenProvider | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.tenant_id = tenant_id or os.getenv("FS_TENANT_ID", "dev-tenant")
        self.user_id = user_id or os.getenv("FS_USER_ID", "sdk-user")
        self.role = role or os.getenv("FS_ROLE", "admin")
        self.auth_token = auth_token or os.getenv("FS_AUTH_TOKEN")
        self.token_provider = token_provider

    def _resolve_token(self) -> str | None:
        if self.auth_token:
            return self.auth_token
        if self.token_provider:
            return self.token_provider.get_token()
        return None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": self.tenant_id,
            "X-User-Id": self.user_id,
            "X-Role": self.role,
        }
        token = self._resolve_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{parse.urlencode(params)}"

        if requests:
            response = requests.request(
                method=method,
                url=url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json() if response.content else {}

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(
            url=url,
            data=body,
            headers=self._headers(),
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                content = resp.read().decode("utf-8")
                return json.loads(content) if content else {}
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8")
            raise RuntimeError(f"{method} {url} failed ({exc.code}): {details}") from exc


class FeatureGroupClient(_BaseClient):
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def create_feature_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/feature-groups", payload=payload)

    def get_feature_group(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/feature-groups/{name}")


class FeatureStoreClient(_BaseClient):
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def ingest_offline(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/offline/ingest", payload=payload)

    def materialize(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/materialize", payload=payload)

    def get_materialization_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/materialize/jobs/{job_id}")

    def get_online(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/online/get", payload=payload)


class LineageClient(_BaseClient):
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def emit_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/lineage/events", payload=payload)
