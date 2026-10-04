"""The model behind Perception and Decision. One interface: `await llm.chat(LlmRequest) -> LlmReply`.

GeminiClient uses google-genai directly: native function calling for Decision (the offered tools come from the
goal's skill), and JSON output bound to a schema for Perception. OpenAICompatClient does the same over an
OpenAI-compatible endpoint — W&B Inference by default.

`make_llm` returns a RoutedLlm: one ordered list of options (Revision 11). PROVIDER's options come first, then
the other provider's: Gemini = GEMINI_MODEL on key 1, then keys 2–5 (failover only, never rotated for load);
W&B = each model in WANDB_MODELS. Each call goes to the first usable option. A failure is classified, and the
option rests (rate limit, daily quota, server trouble), dies (bad key, unknown model), or is skipped for this
call only (ran out of thinking budget). Health is shared by the whole process, so a harness batch carries it from
task to task. Every reply records who answered (provider, model, key SLOT — never a key).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx
import openai
from google import genai
from google.genai import errors, types
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt, wait_exponential

from email_agent.config import Settings
from email_agent.contracts.llm import LlmReply, LlmRequest, ToolCall, Usage

log = logging.getLogger(__name__)
QUICK_TRIES = 2                       # server trouble: retried this often on the same option before switching
PACIFIC = ZoneInfo("America/Los_Angeles")   # Gemini daily quotas reset at midnight Pacific time


class LlmError(Exception):
    pass


class BudgetExceeded(LlmError):
    """The model used its whole token budget (twice) without finishing a reply."""


class Llm(Protocol):
    model: str

    async def chat(self, request: LlmRequest) -> LlmReply: ...


def _log_retry(state: RetryCallState) -> None:
    """Say why we are waiting, so a retry never looks like a hang (shown by the CLI's terminal view)."""
    err = state.outcome.exception() if state.outcome else None
    code = getattr(err, "code", None) or getattr(err, "status_code", None)
    why = f"{type(err).__name__}{f' ({code})' if code else ''}"
    wait = state.next_action.sleep if state.next_action else 0
    log.warning("⏳ %s: waiting %.0fs, then try %d of %d on the same model", why, wait, state.attempt_number + 1,
                QUICK_TRIES)


def _safe(name: str) -> str:
    """Tool names like `EmailThread.list` → `EmailThread__list` (function names stay simple)."""
    return name.replace(".", "__")


# ── the two clients (one model, one key each) ────────────────────────────────

def _gemini_server_trouble(e: BaseException) -> bool:
    return ((isinstance(e, errors.APIError) and (getattr(e, "code", 0) or 0) >= 500)
            or isinstance(e, (httpx.HTTPError, TimeoutError)))


class GeminiClient:
    def __init__(self, model: str, api_key: str, thinking_level: str | None = "MINIMAL", timeout_s: float = 180.0):
        self.model = model
        self.thinking_level = thinking_level
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))

    def _config(self, req: LlmRequest) -> types.GenerateContentConfig:
        tools = None
        if req.tools:
            tools = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=_safe(t.name), description=t.description[:1000],
                                          parameters_json_schema=t.parameters)
                for t in req.tools])]
        return types.GenerateContentConfig(
            system_instruction=req.system,
            temperature=req.temperature,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            response_mime_type="application/json" if req.response_schema else None,
            response_json_schema=req.response_schema,
            thinking_config=(types.ThinkingConfig(thinking_level=self.thinking_level)
                             if self.thinking_level else None),
        )

    async def chat(self, req: LlmRequest) -> LlmReply:
        contents = [types.Content(role=m.role, parts=[types.Part(text=m.text)]) for m in req.messages]
        names = {_safe(t.name): t.name for t in req.tools}
        started = time.perf_counter()
        async for attempt in AsyncRetrying(stop=stop_after_attempt(QUICK_TRIES), wait=wait_exponential(1, max=4),
                                           before_sleep=_log_retry, retry=retry_if_exception(_gemini_server_trouble),
                                           reraise=True):
            with attempt:                      # 429 is not retried here: the route moves to the next key instead
                resp = await self._client.aio.models.generate_content(
                    model=self.model, contents=contents, config=self._config(req))
        calls = [ToolCall(name=names.get(fc.name, fc.name), arguments=dict(fc.args or {}))
                 for fc in (resp.function_calls or [])]
        text = ""
        if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
            text = "".join(p.text for p in resp.candidates[0].content.parts if p.text and not p.thought)
        meta = resp.usage_metadata
        return LlmReply(
            model=self.model,
            text=text.strip(),
            tool_calls=calls,
            finish_reason=(getattr(resp.candidates[0].finish_reason, "value", None) if resp.candidates else None),
            usage=Usage(input_tokens=(meta.prompt_token_count or 0) if meta else 0,
                        output_tokens=(meta.candidates_token_count or 0) if meta else 0,
                        thinking_tokens=(meta.thoughts_token_count or 0) if meta else 0),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )


def _openai_server_trouble(e: BaseException) -> bool:
    return isinstance(e, (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError))


class OpenAICompatClient:
    """W&B Inference (or any OpenAI-compatible endpoint) behind the same chat() interface.

    Reasoning models (e.g. zai-org/GLM-5.3-Flash) think before answering and that counts against
    max_tokens; a reply cut off by the limit is retried once with double the budget."""

    def __init__(self, model: str, api_key: str, base_url: str, project: str | None = None, max_tokens: int = 16000,
                 http: openai.AsyncOpenAI | None = None):
        self.model = model
        self.max_tokens = max_tokens
        headers = {"OpenAI-Project": project} if project else {}
        self._client = http or openai.AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=headers,
                                                  max_retries=0)    # our own retries and the route decide
        self._json_schema_ok = True                                  # set False if the endpoint refuses it once

    def _kwargs(self, req: LlmRequest, max_tokens: int) -> dict[str, Any]:
        system = req.system
        kw: dict[str, Any] = {"model": self.model, "temperature": req.temperature, "max_tokens": max_tokens}
        if req.tools:
            kw["tools"] = [{"type": "function", "function": {"name": _safe(t.name), "description": t.description[:1000],
                                                             "parameters": t.parameters}} for t in req.tools]
        if req.response_schema:
            if self._json_schema_ok:
                kw["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": req.purpose, "schema": req.response_schema, "strict": False}}
            else:
                kw["response_format"] = {"type": "json_object"}
                system += "\n\nAnswer with one JSON object matching this JSON schema:\n" + json.dumps(req.response_schema)
        kw["messages"] = [{"role": "system", "content": system}] + [
            {"role": "assistant" if m.role == "model" else "user", "content": m.text} for m in req.messages]
        return kw

    async def _create(self, req: LlmRequest, max_tokens: int):
        async for attempt in AsyncRetrying(stop=stop_after_attempt(QUICK_TRIES), wait=wait_exponential(1, max=4),
                                           before_sleep=_log_retry, retry=retry_if_exception(_openai_server_trouble),
                                           reraise=True):
            with attempt:
                try:
                    return await self._client.chat.completions.create(**self._kwargs(req, max_tokens))
                except openai.BadRequestError as e:
                    if req.response_schema and self._json_schema_ok and "response_format" in str(e):
                        self._json_schema_ok = False            # fall back to json_object + schema in the prompt
                        log.warning("⏳ %s refused json_schema answers: using json_object from now on", self.model)
                        return await self._client.chat.completions.create(**self._kwargs(req, max_tokens))
                    raise

    async def chat(self, req: LlmRequest) -> LlmReply:
        names = {_safe(t.name): t.name for t in req.tools}
        started = time.perf_counter()
        resp = await self._create(req, self.max_tokens)
        choice = resp.choices[0]
        # Cut off by the token limit with no tool call: the text (if any) is half an answer, never a whole one.
        if choice.finish_reason == "length" and not choice.message.tool_calls:
            log.warning("⏳ %s ran out of its %d-token budget before finishing: retrying once with %d",
                        self.model, self.max_tokens, self.max_tokens * 2)
            resp = await self._create(req, self.max_tokens * 2)   # the thinking used up the budget: once more
            choice = resp.choices[0]
            if choice.finish_reason == "length" and not choice.message.tool_calls:
                raise BudgetExceeded(f"{self.model} used all {self.max_tokens * 2} tokens without finishing its "
                                     "reply; raise LLM_MAX_TOKENS in .env or choose another model")
        msg = choice.message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed_arguments": tc.function.arguments}   # Action's contract check reports it
            calls.append(ToolCall(name=names.get(tc.function.name, tc.function.name),
                                  arguments=args if isinstance(args, dict) else {"_unparsed_arguments": args}))
        u = resp.usage
        details = getattr(u, "completion_tokens_details", None) if u else None
        return LlmReply(
            model=self.model,
            text=(msg.content or "").strip(),
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            usage=Usage(input_tokens=(u.prompt_tokens or 0) if u else 0,
                        output_tokens=(u.completion_tokens or 0) if u else 0,
                        thinking_tokens=(getattr(details, "reasoning_tokens", 0) or 0) if details else 0),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )


# ── what a failure means for the route ───────────────────────────────────────

Kind = Literal["rate_limit", "daily_quota", "dead_key", "dead_model", "server", "this_call"]


@dataclass
class Failure:
    kind: Kind
    message: str
    retry_after: float | None = None       # seconds, when the server said how long to wait

    def words(self) -> str:
        return {"rate_limit": f"rate-limited (retry in {self.retry_after or 0:.0f}s)",
                "daily_quota": "out of today's quota", "dead_key": "key refused",
                "dead_model": "model unavailable", "server": "server trouble",
                "this_call": "could not finish this reply"}[self.kind]


def _seconds_to_pacific_midnight() -> float:
    now = datetime.now(PACIFIC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return (tomorrow - now).total_seconds()


def _gemini_details(e: errors.APIError) -> list[dict]:
    details = getattr(e, "details", None) or {}
    return (details.get("error", {}) if isinstance(details, dict) else {}).get("details", []) or []


def classify_gemini(e: BaseException) -> Failure | None:
    if isinstance(e, (httpx.HTTPError, TimeoutError)):
        return Failure("server", f"{type(e).__name__}: {e}")
    if not isinstance(e, errors.APIError):
        return None
    code, text, details = getattr(e, "code", 0) or 0, str(e), _gemini_details(e)
    if code == 429:
        per_day = any("PerDay" in str(v.get("quotaId", "")) for d in details for v in d.get("violations", []) or [])
        if per_day:
            return Failure("daily_quota", text[:300], _seconds_to_pacific_midnight())
        delay = next((str(d.get("retryDelay")) for d in details if d.get("retryDelay")), "")
        try:
            wait = float(delay.rstrip("s")) + 1 if delay.endswith("s") else 30.0
        except ValueError:
            wait = 30.0
        return Failure("rate_limit", text[:300], wait)
    if code in (401, 403) or "API_KEY_INVALID" in text or "API key not valid" in text:
        return Failure("dead_key", text[:300])
    if code == 404:
        return Failure("dead_model", text[:300])
    if code >= 500:
        return Failure("server", text[:300])
    return Failure("this_call", text[:300])


def classify_openai(e: BaseException) -> Failure | None:
    if isinstance(e, BudgetExceeded):
        return Failure("this_call", str(e))
    if isinstance(e, openai.RateLimitError):
        retry = e.response.headers.get("retry-after") if getattr(e, "response", None) is not None else None
        try:
            wait = float(retry) if retry else 30.0
        except ValueError:
            wait = 30.0
        return Failure("rate_limit", str(e)[:300], wait)
    if isinstance(e, openai.AuthenticationError):
        return Failure("dead_key", str(e)[:300])
    if isinstance(e, (openai.NotFoundError, openai.PermissionDeniedError)):
        return Failure("dead_model", str(e)[:300])
    if isinstance(e, openai.BadRequestError):
        text = str(e).lower()
        model_gone = "model" in text and any(w in text for w in ("not found", "does not exist", "not supported",
                                                                 "unknown model", "invalid model"))
        return Failure("dead_model" if model_gone else "this_call", str(e)[:300])
    if isinstance(e, (openai.InternalServerError, openai.APIConnectionError, openai.APITimeoutError)):
        return Failure("server", str(e)[:300])
    if isinstance(e, openai.APIStatusError):
        return Failure("this_call", str(e)[:300])
    return None


# ── the route ────────────────────────────────────────────────────────────────

@dataclass
class Option:
    """One place an answer can come from: a provider, a model and (Gemini) a key slot, with its health."""

    provider: Literal["gemini", "openai"]
    model: str
    key_slot: int | None
    client: Any
    resting_until: float = 0.0               # time.monotonic() when it may be tried again
    dead_reason: str | None = None
    last_error: str | None = None
    served: int = field(default=0)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}" + (f" key #{self.key_slot}" if self.key_slot else "")

    def state(self, now: float) -> str:
        if self.dead_reason:
            return f"{self.label}: dead ({self.dead_reason})"
        if self.resting_until > now:
            return f"{self.label}: resting {self.resting_until - now:.0f}s ({self.last_error or ''})"
        return f"{self.label}: ready"


def plan_route(settings: Settings) -> list[tuple[str, str, int | None]]:
    """(provider, model, key slot) in priority order — no clients made, no network."""
    order = [settings.provider] + [p for p in ("gemini", "openai") if p != settings.provider]
    planned: list[tuple[str, str, int | None]] = []
    for provider in order:
        if provider == "gemini":
            slots = [slot for slot, _ in settings.gemini_keys()]
            planned += [("gemini", m, slot) for m in settings.models_for("gemini") for slot in slots]
        elif settings.wandb_api_key is not None and settings.wandb_api_key.get_secret_value():
            planned += [("openai", m, None) for m in settings.models_for("openai")]
    return planned[:1] if not settings.fallback else planned


def route_text(settings: Settings) -> str:
    """The route in one line, e.g. `gemini/gemini-3.5-flash-lite (keys 1, 2) → openai/zai-org/GLM-5.3-Flash → …`."""
    groups: list[tuple[str, list[int]]] = []          # consecutive key slots of one model are shown together
    for provider, model, slot in plan_route(settings):
        name = f"{provider}/{model}"
        if groups and groups[-1][0] == name and slot:
            groups[-1][1].append(slot)
        else:
            groups.append((name, [slot] if slot else []))
    parts = [f"{name} (key{'s' if len(slots) > 1 else ''} {', '.join(map(str, slots))})" if slots else name
             for name, slots in groups]
    return " → ".join(parts) or "no LLM key set"


class RoutedLlm:
    """Tries each option in priority order; see the module docstring for what each failure does."""

    def __init__(self, options: list[Option], wait_max_s: float = 120.0, breaker_s: float = 60.0, text: str = "",
                 call_ceiling_s: float = 900.0):
        if not options:
            raise LlmError("no LLM key is set: add GEMINI_API_KEY and/or WANDB_API_KEY to .env")
        self.options, self.wait_max_s, self.breaker_s, self.text = options, wait_max_s, breaker_s, text
        # A hard limit per option per call: SDK timeouts measure the gap between bytes, so a server that keeps a
        # connection open without finishing can slip past them (two W&B calls hung for 50+ minutes, 2026-10-03).
        self.call_ceiling_s = call_ceiling_s
        self.model = options[0].model

    def _apply(self, opt: Option, f: Failure) -> None:
        now = time.monotonic()
        opt.last_error = f.words()
        if f.kind in ("rate_limit", "daily_quota"):
            opt.resting_until = now + (f.retry_after or 30.0)
        elif f.kind == "server":
            opt.resting_until = now + self.breaker_s
        elif f.kind == "dead_key":
            # Gemini: only this key. W&B: one key serves every model, so all of them stop.
            for o in self.options:
                if o is opt or (opt.provider == "openai" and o.provider == "openai"):
                    o.dead_reason = "key refused"
        elif f.kind == "dead_model":
            for o in self.options:              # the same model on every key
                if o.provider == opt.provider and o.model == opt.model:
                    o.dead_reason = "model unavailable"

    async def chat(self, req: LlmRequest) -> LlmReply:
        deadline = time.monotonic() + self.wait_max_s
        failed_now: set[int] = set()               # options that could not finish THIS call (no rest)
        while True:
            skipped: list[str] = []
            now = time.monotonic()
            for i, opt in enumerate(self.options):
                if opt.dead_reason or opt.resting_until > now or i in failed_now:
                    skipped.append(opt.state(now) if i not in failed_now else f"{opt.label}: {opt.last_error}")
                    continue
                try:
                    reply = await asyncio.wait_for(opt.client.chat(req), self.call_ceiling_s)
                except Exception as e:
                    f = (Failure("server", f"no reply within {self.call_ceiling_s:.0f}s")
                         if isinstance(e, TimeoutError) and not isinstance(e, (openai.APITimeoutError,))
                         else (classify_gemini if opt.provider == "gemini" else classify_openai)(e))
                    if f is None:
                        raise
                    self._apply(opt, f)
                    if f.kind == "this_call":
                        failed_now.add(i)
                    nxt = next((o.label for j, o in enumerate(self.options) if j > i and not o.dead_reason
                                and o.resting_until <= time.monotonic() and j not in failed_now), None)
                    log.warning("⏳ %s %s → %s", opt.label, f.words(), nxt or "no other option ready")
                    skipped.append(f"{opt.label}: {f.words()}")
                    now = time.monotonic()
                    continue
                opt.served += 1
                return reply.model_copy(update={"provider": opt.provider, "key_slot": opt.key_slot,
                                                "fallback_from": skipped})
            alive = [o for j, o in enumerate(self.options) if not o.dead_reason and j not in failed_now]
            now = time.monotonic()
            if not alive:
                raise LlmError("no LLM option could answer: " + "; ".join(
                    f"{o.label}: {o.last_error} (this call)" if j in failed_now else o.state(now)
                    for j, o in enumerate(self.options)))
            wake = min(o.resting_until for o in alive)
            if wake > deadline:
                raise LlmError(f"every LLM option is resting longer than {self.wait_max_s:.0f}s: "
                               + "; ".join(o.state(now) for o in self.options))
            log.warning("⏳ every option is resting: waiting %.0fs", max(0.0, wake - now))
            await asyncio.sleep(max(0.0, wake - now))


_ROUTES: dict[tuple, RoutedLlm] = {}


def build_route(settings: Settings) -> RoutedLlm:
    keys = dict(settings.gemini_keys())
    shared_openai: openai.AsyncOpenAI | None = None
    options = []
    for provider, model, slot in plan_route(settings):
        if provider == "gemini":
            client = GeminiClient(model, keys[slot].get_secret_value(), settings.gemini_thinking_level,
                                  settings.llm_timeout_s)
        else:
            if shared_openai is None:
                headers = {"OpenAI-Project": settings.wandb_project} if settings.wandb_project else {}
                shared_openai = openai.AsyncOpenAI(api_key=settings.wandb_api_key.get_secret_value(),
                                                   base_url=settings.openai_base_url, default_headers=headers,
                                                   max_retries=0, timeout=settings.llm_timeout_s)
            client = OpenAICompatClient(model, "", settings.openai_base_url, max_tokens=settings.llm_max_tokens,
                                        http=shared_openai)
        options.append(Option(provider=provider, model=model, key_slot=slot, client=client))
    # ceiling = every try a client may make for one reply (2 quick tries × the doubled-budget retry) + margin
    return RoutedLlm(options, settings.llm_wait_max_s, settings.llm_breaker_s, route_text(settings),
                     call_ceiling_s=settings.llm_timeout_s * QUICK_TRIES * 2 + 30)


def make_llm(settings: Settings) -> RoutedLlm:
    """One route per settings for the whole process, so option health carries from run to run (harness batches)."""
    key = (tuple(plan_route(settings)), settings.llm_max_tokens, settings.llm_wait_max_s, settings.llm_breaker_s,
           settings.gemini_thinking_level, settings.llm_timeout_s)
    if key not in _ROUTES:
        _ROUTES[key] = build_route(settings)
    return _ROUTES[key]
