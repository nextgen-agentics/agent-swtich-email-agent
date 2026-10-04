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
    llm_max_tokens: int = 16000  # reasoning models spend part of this before answering; GLM-5.3-Flash used 8–16k
                                 # thinking on 8-item batches (2026-10-03), so 8000 was too small
    fallback: bool = True              # false = only the first option (comparisons, bake-offs)
    llm_wait_max_s: float = 120.0      # when every option is resting, wait at most this long
    llm_breaker_s: float = 60.0        # an option with server trouble rests this long before it is tried again
    llm_timeout_s: float = 180.0       # one model call; past it (or a hard ceiling per option) = server trouble

    max_steps: int = 20
    http_timeout_s: float = 60.0
    cache_dir: Path = PROJECT_ROOT / ".cache"
    data_dir: Path = PROJECT_ROOT / "data"
    runs_dir: Path = PROJECT_ROOT / "runs"

    def instance(self, name: str) -> Instance:
        if name not in INSTANCES:
            raise KeyError(f"unknown instance {name!r}; known: {sorted(INSTANCES)}")
        return INSTANCES[name]

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
