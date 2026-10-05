from __future__ import annotations

import os
from datetime import date

import pytest

from email_agent import agent
from email_agent.config import Settings
from email_agent.contracts.agent import OurMailbox, RunContext
from email_agent.platform import context
from email_agent.watch import watcher
from tests.kit.mail import SALES, Mail
from tests.kit.platform import FakePlatform

TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _no_real_config(monkeypatch):
    """Nothing from the shell or .env reaches a test: no keys, no crash point, no trace exporter."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name in [k for k in os.environ if k.startswith(("OTEL_", "GEMINI_", "WANDB_", "EMAIL_AGENT_"))]:
        monkeypatch.delenv(name)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, runs_dir=tmp_path / "runs", state_dir=tmp_path / "state",
                    cache_dir=tmp_path / ".cache", rules_dir=tmp_path / "rules", validate_verdicts=False)


@pytest.fixture
def platform(monkeypatch) -> FakePlatform:
    book = FakePlatform()
    monkeypatch.setattr(agent, "McpSession", book.session)
    monkeypatch.setattr(agent, "RestClient", book.rest)
    monkeypatch.setattr(context, "RestClient", book.rest)
    monkeypatch.setattr(watcher, "McpSession", book.session)
    return book


@pytest.fixture
def mail(platform) -> Mail:
    return Mail(platform)


@pytest.fixture
def ctx(platform) -> RunContext:
    return RunContext(run_id="test-run", instance="suryodaya", request="What needs my reply today?", today=TODAY,
                      me=platform.user, locale=platform.locale,
                      mailboxes=[OurMailbox(id=SALES["id"], email=SALES["email"])], our_addresses=[SALES["email"]])
