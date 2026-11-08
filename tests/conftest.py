"""Shared test setup. Everything runs offline against the mock provider."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pinch.cli import build_agent
from pinch.config import Config
from pinch.mock_provider import MockProvider
from pinch.policy import load_policy
from pinch.session import Session, find_session


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never read or write the real ~/.config/pinch or ~/.local/share/pinch."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    for name in ("PINCH_BASE_URL", "PINCH_MODEL", "PINCH_GATEWAY_KEY", "PINCH_STYLE", "PINCH_MODE", "PINCH_ENV_FILE", "PINCH_CONFIG"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A small git repo with one bug: add() subtracts."""
    path = tmp_path / "work" / "repo"
    path.mkdir(parents=True)
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (path / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"], ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=path, check=True, capture_output=True)
    return path


class FakeGateway:
    """Stands in for the Bifrost admin API in offline tests."""

    def __init__(self, local_ok: bool = True, budget=None):
        self.local_ok, self.budget = local_ok, budget

    def check_local_only(self, vk_id, local_providers, expected_key=""):
        from pinch.gateway import GatewayError
        if not self.local_ok:
            raise GatewayError(f"virtual key '{vk_id}' can reach non-local providers: cloud-a")
        return list(local_providers)

    def tightest_budget(self, vk_id):
        return self.budget


@dataclass
class Rig:
    agent: object
    mock: MockProvider
    session: Session
    cfg: Config
    asked: list = field(default_factory=list)  # every approval prompt shown to the "user"
    said: list = field(default_factory=list)  # every line pinch printed
    budget_prompts: list = field(default_factory=list)  # every budget-limit menu shown

    def sent(self) -> list[dict]:
        """Bodies of the requests the mock received."""
        return self.mock.request_bodies

    def session_events(self, name: str) -> list[dict]:
        return [r for r in self.session.records() if r["type"] == "event" and r["event"] == name]


@pytest.fixture
def make_rig(tmp_path, repo):
    mocks = []

    def make(script, *, answers=("n",) * 20, repo_path=None, resume=None, gateway=None, budget_answers=("q",) * 5,
             **cfg_fields) -> Rig:
        mock = MockProvider(script, tmp_path / f"mock-{len(mocks)}.jsonl").start()
        mocks.append(mock)
        fields = {"base_url": mock.url, "model": "mock-small", "session_dir": tmp_path / "sessions", "max_steps": 10,
                  "retries": 1, "retry_backoff": 0.0, "budget_vk_id": "vk-test", "savings": "none",
                  "spend_dir": tmp_path / "spend", "config_path": tmp_path / "no-config.toml", **cfg_fields}
        cfg = Config(**fields)
        target = repo_path or repo
        policy = load_policy(target, listed=cfg.confidential_repos)
        if gateway is None and (policy.confidential or cfg.confidential_key):
            cfg.confidential_key, gateway = cfg.confidential_key or "local-only-key", FakeGateway()   # a verified local key
        if resume:
            session = Session(find_session(cfg.session_dir, resume))
        else:
            session = Session.create(cfg.session_dir, {"repo": str(policy.repo), "model": cfg.model, "mode": cfg.mode,
                                                       "style": cfg.style, "confidential": policy.confidential})
        rig = Rig(None, mock, session, cfg)
        replies = iter(answers)

        def ask(prompt: str) -> str:
            rig.asked.append(prompt)
            return next(replies, "n")

        budget_replies = iter(budget_answers)
        rig.budget_prompts = []

        def ask_budget(headline: str) -> str:
            rig.budget_prompts.append(headline)
            return next(budget_replies, "q")

        rig.agent = build_agent(cfg, policy, session, ask=ask, out=rig.said.append, resume=bool(resume), gateway=gateway,
                                ask_budget=ask_budget)
        return rig

    yield make
    for m in mocks:
        m.stop()
