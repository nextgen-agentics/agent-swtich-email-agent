"""REST access to AgentSwitch — only for what MCP does not offer.

    POST /api/auth/login              → bearer token (MCP needs it)
    GET  /api/auth/me                 → who we are (Me)
    GET  /api/accounting/display-locale → country, currency, formats (RegimeLocale)
    GET  /api/schemas, /api/agent/tools, /api/mcp/tools, /openapi.json
    POST /api/bug-report, GET /api/bug-report/mine
    plus raw requests used by the protocol probes.

Everything the agent does to business data goes through MCP (mcp_session.py).

Retries use `tenacity`: network errors and 502/503/504 are retried with
exponential backoff; 4xx never are. Every exchange can be recorded as an
HttpExchange (secrets masked) for evidence.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx2
from pydantic import BaseModel
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from email_agent.config import Instance, Settings
from email_agent.contracts.mcp import McpCallError, McpError
from email_agent.contracts.platform import LoginResponse, Me, RegimeLocale
from email_agent.contracts.transport import HttpExchange

RETRY_STATUSES = {502, 503, 504}
_REDACT_KEYS = {"password", "token", "authorization"}

Recorder = Callable[[HttpExchange], None]


class _RetryableStatus(Exception):
    def __init__(self, response: httpx2.Response):
        self.response = response


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _REDACT_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def body_of(resp: httpx2.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


class _CachedToken(BaseModel):
    token: str
    email: str
    instance: str
    obtained_at: datetime


class RestClient:
    """Authenticated HTTP access to one instance, as one team (synchronous)."""

    def __init__(self, settings: Settings, instance: Instance | str,
                 recorder: Recorder | None = None):
        self.settings = settings
        self.instance = settings.instance(instance) if isinstance(instance, str) else instance
        self.recorder = recorder
        self._http = httpx2.Client(base_url=self.instance.base_url, timeout=settings.http_timeout_s)
        self._token: str | None = None
        self._login_lock = threading.Lock()

    # ── token cache, namespaced per team + instance (Session-20 §3) ──────────
    @property
    def _cache_path(self) -> Path:
        return self.settings.cache_dir / f"{self.settings.team}-{self.instance.name}.token.json"

    def _load_cached(self) -> str | None:
        try:
            cached = _CachedToken.model_validate_json(self._cache_path.read_text())
        except (OSError, ValueError):
            return None
        if cached.email != self.settings.as_email or cached.instance != self.instance.name:
            return None
        return cached.token

    def _save_cached(self, token: str) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(_CachedToken(
            token=token, email=self.settings.as_email, instance=self.instance.name,
            obtained_at=datetime.now(timezone.utc),
        ).model_dump_json())

    # ── one request: retry transient failures, record evidence ───────────────
    def request(self, method: str, path: str, *, json_body: Any = None,
                params: dict[str, Any] | None = None, auth: bool = True) -> httpx2.Response:
        """One call; on 401 (the token expired during a long run) log in again once and repeat it. Threads share the
        client, so only the first to see the stale token logs in."""
        stale = self._token
        resp = self._send(method, path, json_body=json_body, params=params, auth=auth)
        if auth and resp.status_code == 401:
            with self._login_lock:
                if self._token == stale:
                    self.login(force=True)
            resp = self._send(method, path, json_body=json_body, params=params, auth=auth)
        return resp

    def _send(self, method: str, path: str, *, json_body: Any, params: dict[str, Any] | None,
              auth: bool) -> httpx2.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}

        def attempt() -> httpx2.Response:
            started = time.perf_counter()
            resp = self._http.request(method, path, json=json_body, params=params, headers=headers)
            self._record(method, path, json_body, resp, started)
            if resp.status_code in RETRY_STATUSES:
                raise _RetryableStatus(resp)
            return resp

        try:
            for attempt_ctx in Retrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.5, max=4),
                retry=retry_if_exception_type((httpx2.TransportError, _RetryableStatus)),
                reraise=True,
            ):
                with attempt_ctx:
                    return attempt()
        except _RetryableStatus as e:
            return e.response  # still 5xx after retries: hand it back, caller decides
        except httpx2.TransportError as e:
            raise McpCallError(McpError(kind="transport", message=f"{method} {path}: {e!r}")) from e
        raise AssertionError("unreachable")

    def _record(self, method: str, path: str, body: Any, resp: httpx2.Response, started: float) -> None:
        if self.recorder is None:
            return
        self.recorder(HttpExchange(
            method=method, url=f"{self.instance.base_url}{path}",
            request_body=redact(body), status=resp.status_code,
            response_headers={k: v for k, v in resp.headers.items()
                              if k.lower() in {"content-type", "allow", "retry-after"}},
            response_body=redact(body_of(resp)),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        ))

    # ── auth ─────────────────────────────────────────────────────────────────
    def login(self, force: bool = False) -> str:
        if not force and (cached := self._load_cached()):
            self._token = cached
            return cached
        pw = self.settings.password_for(self.instance.name).get_secret_value()
        resp = self.request("POST", "/api/auth/login", auth=False,
                            json_body={"email": self.settings.as_email, "password": pw})
        if resp.status_code != 200:
            raise McpCallError(McpError(kind="auth", http_status=resp.status_code,
                                        message=f"login failed: {str(body_of(resp))[:200]}"))
        self._token = LoginResponse.model_validate(resp.json()).token
        self._save_cached(self._token)
        return self._token

    @property
    def token(self) -> str:
        return self._token or self.login()

    def valid_token(self) -> str:
        """A token that works right now (re-login if the cached one expired)."""
        if self.get("/api/auth/me").status_code == 200:
            return self.token
        return self.login(force=True)

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx2.Response:
        """Authenticated GET (an expired token is renewed by `request`)."""
        return self.request("GET", path, params=params)

    # ── typed reads ──────────────────────────────────────────────────────────
    def me(self) -> Me:
        resp = self.get("/api/auth/me")
        if resp.status_code != 200:
            raise McpCallError(McpError(kind="auth", http_status=resp.status_code,
                                        message=f"/api/auth/me → {resp.status_code}"))
        return Me.model_validate(resp.json())

    def display_locale(self) -> RegimeLocale:
        """The company's country/currency/formats — readable by every seat."""
        resp = self.get("/api/accounting/display-locale")
        resp.raise_for_status()
        return RegimeLocale.model_validate(resp.json()["locale"])
