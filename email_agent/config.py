"""Settings and the instance registry.

Everything configurable comes from `.env` (see `.env.example`) through the
`Settings` model. Instances are data, not code: adding a vertical means one
line in INSTANCES.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOUSE_RULES_CHARS = 4000


class Instance(BaseModel):
    """One AgentSwitch deployment. Same software, different company/locale."""

    name: str
    base_url: str
    description: str = ""
    mailboxes: list[str] | None = None   # the addresses the agent works in; None = every mailbox our login has

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/api/mcp"


INSTANCES: dict[str, Instance] = {
    i.name: i
    for i in [
        Instance(name="suryodaya", base_url="https://agentswitch.theschoolofai.in",
                 description="Suryodaya Precision Works — India, Ind AS, GST"),
        # Every mailbox our login has (your choice 2026-10-03: orders@ and team10@). A task or --mailbox can narrow
        # it; the Keystone triage task keeps team10@ only (its answer key, 2026-09-29).
        Instance(name="keystone", base_url="https://class.agentswitch.theschoolofai.in",
                 description="Keystone Precision Works LLC — US, US GAAP, Sales & Use Tax"),
        # Verticals: same tables, different nouns. Login not yet confirmed (Stage 1).
        Instance(name="school", base_url="https://school.agentswitch.theschoolofai.in"),
        Instance(name="clinic", base_url="https://clinic.agentswitch.theschoolofai.in"),
        Instance(name="retail", base_url="https://retail.agentswitch.theschoolofai.in"),
        Instance(name="agency", base_url="https://agency.agentswitch.theschoolofai.in"),
    ]
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore",
        env_ignore_empty=True,  # `AS_EMAIL=` (blank) in .env keeps the default instead of ""
    )

    team: str = "team10"
    as_email: str = "team10@theschoolofai.in"
    as_suryodaya_password: SecretStr | None = None
    as_keystone_password: SecretStr | None = None
    as_school_password: SecretStr | None = None
    as_clinic_password: SecretStr | None = None
    as_retail_password: SecretStr | None = None
    as_agency_password: SecretStr | None = None

    # LLM route (Revision 11): PROVIDER goes first, then the other provider — every option in priority order.
    provider: Literal["gemini", "openai"] = "gemini"
    model: str | None = None           # optional: replaces the primary provider's FIRST model (older .env files)
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_api_key: SecretStr | None = None          # key 1; keys 2–5 are failover only (Google's terms forbid
    gemini_api_key_2: SecretStr | None = None        # circumventing rate limits, so they are never rotated for load)
    gemini_api_key_3: SecretStr | None = None
    gemini_api_key_4: SecretStr | None = None
    gemini_api_key_5: SecretStr | None = None
    gemini_thinking_level: Literal["MINIMAL", "LOW", "MEDIUM", "HIGH"] | None = "MINIMAL"
    wandb_api_key: SecretStr | None = None
    wandb_project: str | None = None  # "<team>/<project>" for W&B usage tracking
    # order from the bake-off 2026-10-03 (docs/project/plan.md, Revision 11): DeepSeek 5/5 right, GLM 4/5, Qwen 2/4
    wandb_models: str = "deepseek-ai/DeepSeek-V4.1-Flash,zai-org/GLM-5.3-Flash,Qwen/Qwen3-30B-A3B-Instruct-2507"
    openai_base_url: str = "https://api.inference.wandb.ai/v1"
    # Reasoning on W&B models (Revision 17). Off = faster replies and fewer tokens: sent as
    # chat_template_kwargs.enable_thinking=false, which DeepSeek-V4.1-Flash honours; GLM-5.3-Flash always reasons and
    # Qwen3-30B-A3B-Instruct never does (W&B / CoreWeave reasoning docs, 2026-10). A model that refuses the flag is
    # asked again without it.
    wandb_thinking: bool = False
    llm_max_tokens: int = 16000  # reasoning models spend part of this before answering; GLM-5.3-Flash used 8–16k
                                 # thinking on 8-item batches (2026-10-03), so 8000 was too small
    fallback: bool = True              # false = only the first option (comparisons, bake-offs)
    llm_wait_max_s: float = 120.0      # when every option is resting, wait at most this long
    llm_breaker_s: float = 60.0        # an option with server trouble rests this long before it is tried again
    llm_timeout_s: float = 180.0       # one model call; past it (or a hard ceiling per option) = server trouble

    # the graph (Revision 12): limits per run, all saved in run.sqlite so a resumed run keeps what it spent
    max_workers: int = 6               # graph nodes running at once
    llm_concurrency: int = 3           # LLM calls at once (Gemini limits are per key)
    mcp_concurrency: int = 2           # MCP calls at once (Stage 0: the server answers our calls one at a time)
    mcp_call_timeout_s: float = 90.0   # one MCP call; past it the call fails alone (the transport's own 300 s read
                                       # timeout would end the whole session: price-keystone, 2026-10-04)
    mcp_open_timeout_s: float = 60.0   # opening a session: token check + handshake (a harness batch stalled 8 min
    mcp_close_timeout_s: float = 20.0  # between tasks, 2026-10-04); closing it: the session DELETE
    max_planner_rounds: int = 12
    validate_verdicts: bool = True     # cross-model check of write-causing verdicts before writing (Stage 5)
    max_llm_calls: int = 80
    max_nodes: int = 80                # every node, shards included
    http_timeout_s: float = 60.0
    cache_dir: Path = PROJECT_ROOT / ".cache"
    data_dir: Path = PROJECT_ROOT / "data"
    runs_dir: Path = PROJECT_ROOT / "runs"
    state_dir: Path = PROJECT_ROOT / "state"           # local mailbox copy per instance (Revision 12), not in git
    rules_dir: Path = PROJECT_ROOT / "rules"           # your house rules per instance, <instance>.md (Stage 7), in git
    subscriptions_file: Path = PROJECT_ROOT / "watch" / "subscriptions.yaml"   # the inbox watcher's (Stage 8), in git
    watch_interval_s: float = 120.0    # the watcher polls this often (there is no push from the platform)
    watch_max_runs: int = 2            # runs the watcher starts at once
    # meaning-based search (Stage 9): "fts" = full-text only (default until scripts/agent/eval_search.py says otherwise);
    # "hybrid" = Gemini embeddings + FAISS, mixed with full text
    search: Literal["fts", "hybrid"] = "fts"
    embed_model: str = "gemini-embedding-001"   # one vector per text (gemini-embedding-2 merges a list into one)
    embed_dims: int = 768
    embed_per_minute: int = 90         # texts per minute: the free tier counts each text as a request, 100 a minute
    search_vector_candidates: int = 10 # hybrid judge_threads: at most N conversations added by meaning …
    search_vector_margin: float = 0.04 # … and only those within this of the best meaning score (on the 20 draft
                                       # queries: every expected conversation kept, 0.3 wrong extras per query;
                                       # a plain top 10 added half of Suryodaya's 20 conversations)
    memory_vector_floor: float = 0.65  # hybrid recall without a party: a memory this close counts as matching

    def instance(self, name: str) -> Instance:
        if name not in INSTANCES:
            raise KeyError(f"unknown instance {name!r}; known: {sorted(INSTANCES)}")
        return INSTANCES[name]

    def house_rules(self, instance: str) -> str | None:
        """rules/<instance>.md, if you wrote one: standing instructions for this book (memory layer 1), at most
        HOUSE_RULES_CHARS (the rest is cut, and the cut is said)."""
        path = self.rules_dir / f"{instance}.md"
        if not path.is_file():
            return None
        text = path.read_text().strip()
        if len(text) > HOUSE_RULES_CHARS:
            text = text[:HOUSE_RULES_CHARS] + f"\n[… cut: rules/{instance}.md is longer than {HOUSE_RULES_CHARS} characters]"
        return text or None

    def gemini_keys(self) -> list[tuple[int, SecretStr]]:
        """(slot, key) for every Gemini key that is set, in failover order. Slots are what logs show — never keys."""
        keys = [self.gemini_api_key, self.gemini_api_key_2, self.gemini_api_key_3, self.gemini_api_key_4,
                self.gemini_api_key_5]
        return [(slot, k) for slot, k in enumerate(keys, start=1) if k is not None and k.get_secret_value()]

    def models_for(self, provider: str) -> list[str]:
        """The provider's models in order; MODEL (if set) replaces the primary provider's first one."""
        models = [self.gemini_model] if provider == "gemini" else \
            [m.strip() for m in self.wandb_models.split(",") if m.strip()]
        if provider == self.provider and self.model:
            models = [self.model] + [m for m in models[1:] if m != self.model]
        return models

    def password_for(self, instance_name: str) -> SecretStr:
        pw = getattr(self, f"as_{instance_name}_password", None)
        if pw is None or not pw.get_secret_value():
            raise ValueError(
                f"no password for {instance_name!r}: set AS_{instance_name.upper()}_PASSWORD in .env"
            )
        return pw


def get_settings() -> Settings:
    return Settings()


def with_model(settings: Settings, provider: str | None = None, model: str | None = None,
               fallback: bool | None = None) -> Settings:
    """Settings for one run with another primary provider, first model or fallback choice (.env is not touched).
    Switching provider without naming a model uses that provider's own list (GEMINI_MODEL / WANDB_MODELS)."""
    update: dict = {}
    if provider and provider != settings.provider:
        update["provider"], update["model"] = provider, None
    if model:
        update["model"] = model
    if fallback is not None:
        update["fallback"] = fallback
    return settings.model_copy(update=update) if update else settings
