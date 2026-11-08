"""Command-line entry point.

    pinch "fix the failing test"            run one task in the current repo
    pinch                                   interactive session (/style <name>, /help, /quit)
    pinch --resume <id>                     continue a saved session
    pinch ledger <id>                       token and cost summary for a session
    pinch prove                             run the 4 mandatory-requirement scenarios through Bifrost
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .agent import Agent
from .approval import Gate
from .budget import BudgetGuard
from .client import ModelClient
from .config import MODES, STYLES, Config, load_config
from .gateway import Gateway, GatewayError
from .ledger import summarize
from .savings import TECHNIQUES, parse_savings
from .savings.cascade import Cascade
from .policy import Policy, load_policy
from .session import Session, find_session, missing_tool_results
from .spend import SpendFile
from .tavily import DocsLookup
from .tools import ToolBox

HELP = f"Commands: /style <{'|'.join(STYLES)}>   /help   /quit"


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def terminal_ask(prompt: str) -> str:
    """Ask the person at the keyboard. No answer (closed input) means no."""
    print(prompt, file=sys.stderr, flush=True)
    try:
        return input("  > ")
    except EOFError:
        return "n"


def terminal_ask_budget(headline: str) -> str:
    try:
        return input("  > ")
    except EOFError:
        return "q"


def build_agent(cfg: Config, policy: Policy, session: Session, ask=terminal_ask, out=say, resume: bool = False,
                gateway: Gateway | None = None, ask_budget=terminal_ask_budget) -> Agent:
    """Wire everything together. Tests use this with a fake `ask` and the mock provider."""
    if gateway is None and cfg.admin_user:
        gateway = Gateway(cfg.admin_url, cfg.admin_user, cfg.admin_password)

    # Requirement 3: a confidential repo may only use a key the gateway confirms can reach local providers only.
    key = cfg.gateway_key
    local_key = ""
    if cfg.confidential_key:
        try:
            if gateway is None:
                raise GatewayError("no gateway admin credentials are set (PINCH_GATEWAY_ADMIN_USER / PINCH_GATEWAY_ADMIN_PASSWORD)")
            reachable = gateway.check_local_only(cfg.confidential_vk_id, cfg.local_providers, cfg.confidential_key)
            local_key = cfg.confidential_key
            if policy.confidential:
                out(f"Confidential repo: virtual key '{cfg.confidential_vk_id}' can reach only local providers ({', '.join(reachable)}).")
        except GatewayError as e:
            if policy.confidential:
                raise SystemExit(f"pinch: this repo is confidential, so it may only use a key that can reach local models only, "
                                 f"and pinch could not confirm that: {e}. Nothing was sent.")
            out(f"The local-tier key could not be verified ({e}), so the 'continue on the local tier' choice is off.")
    elif policy.confidential:
        raise SystemExit("pinch: this repo is confidential, so it may only use a local-only virtual key, but none is set. "
                         "Set PINCH_CONFIDENTIAL_KEY (and PINCH_CONFIDENTIAL_VK_ID) to a key the gateway limits to local providers.")
    if policy.confidential:
        key = cfg.confidential_key

    # Token-saving techniques. A confidential repo never uses the response cache or the web lookup.
    on = parse_savings(cfg.savings)
    if policy.confidential:
        on = on - {"cache", "docs"}
        if cfg.local_model:
            on = on - {"cascade"}  # the cascade's tiers are cloud models; the local-only key may use only the local one
    if not cfg.tavily_key:
        on = on - {"docs"}  # nothing to switch on without a key
    if not (cfg.small_model and cfg.big_model):
        on = on - {"cascade"}  # needs a model name for each tier
    # Bifrost's cache is opt-in per request (x-bf-cache-key). The namespace is per run, so benchmark runs never share answers.
    headers = {"x-bf-cache-key": cfg.cache_key or session.id, "x-bf-cache-type": "direct"} if "cache" in on else {}
    docs = DocsLookup(cfg.tavily_key, cfg.tavily_url, enabled="docs" in on,
                      on_call=lambda **kw: session.append("tavily", **kw))
    tools = ToolBox(policy.repo, cfg.mode, small_edits="smalledits" in on, docs=docs)
    # A configured local model (PINCH_LOCAL_MODEL) means a real small model, so the local tier gets a small direct tool set.
    local_tools = cfg.local_tools if cfg.local_model else ()
    if policy.confidential and local_tools:
        tools.restrict(local_tools)
    cascade = Cascade(cfg.small_model, cfg.big_model, cfg.escalate_after_failed_tests, cfg.escalate_after_tool_errors) \
        if "cascade" in on else None
    model = cfg.small_model if cascade and cascade.active else cfg.model
    if policy.confidential and cfg.local_model:
        model = cfg.local_model  # the local-only key may use only local models
    client = ModelClient(cfg.base_url, model, key, cfg.key_header,
                         cfg.max_tokens, retries=cfg.retries, backoff=cfg.retry_backoff, extra_headers=headers,
                         min_call_interval=cfg.min_call_interval, rate_limit_retries=cfg.rate_limit_retries)
    spend = SpendFile(cfg.spend_dir, cfg.budget_vk_id)
    client.on_event = lambda name, **data: agent.on_client_event(name, **data)  # `agent` exists by the time a call is made
    gate = Gate(repo=policy.repo, ask=ask, auto_approve=cfg.auto_approve, allowed_in_config=frozenset(cfg.allow_tools))
    agent = Agent(client, tools, gate, policy, session, cfg.style, cfg.max_steps, say=out, ask_budget=ask_budget,
                  local_key=local_key, local_providers=cfg.local_providers, vk_id=cfg.budget_vk_id, techniques=on, cascade=cascade,
                  local_model=cfg.local_model, local_tools=local_tools,
                  trim_after=cfg.trim_after, test_command=cfg.test_command, spend_file=spend)
    agent.emit("savings", techniques=sorted(on), requested=cfg.savings)  # which techniques this run used, for the ledger
    if not policy.confidential:  # the local tier is free, so there is nothing to budget
        agent.budget = BudgetGuard(gateway, cfg.budget_vk_id, cfg.budget_usd, agent.prices, lambda: agent.spent, spend.total)
    if resume:
        agent.messages = session.messages()
        agent.step = session.next_step()
        agent.last_served = session.last_served()
        for message in missing_tool_results(agent.messages):
            agent.add(message)
    return agent


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "ledger":
        return ledger_main(argv[1:])
    if argv and argv[0] == "prove":
        from .prove import main as prove_main
        return prove_main(argv[1:])

    p = argparse.ArgumentParser(prog="pinch", description="A coding agent that finishes tasks on the fewest tokens.")
    p.add_argument("task", nargs="?", help="what to do; leave out for an interactive session")
    p.add_argument("--repo", help="repository to work in (default: current directory)")
    p.add_argument("--resume", metavar="ID", help="continue a saved session")
    p.add_argument("--style", choices=STYLES, help="how the model words its replies (default: brief)")
    p.add_argument("--mode", choices=MODES, help="tool schemas: 'lazy' sends 3 meta-tools, 'full' sends all of them")
    p.add_argument("--model", help="model name")
    p.add_argument("--base-url", help="OpenAI-compatible endpoint, e.g. http://127.0.0.1:8089/v1")
    p.add_argument("--savings", metavar="LIST", help="token-saving techniques: all, none, or a comma list of "
                   + ", ".join(TECHNIQUES) + " (default: all)")
    p.add_argument("--max-steps", type=int, help="most model calls per task")
    p.add_argument("--session-dir", type=Path, help="where session files are kept")
    p.add_argument("--config", type=Path, help="config file (default: ~/.config/pinch/config.toml)")
    p.add_argument("--auto-approve", action="store_true",
                   help="approve writes, shell and git without asking. For benchmark runs only. Every approval is logged.")
    p.add_argument("--version", action="version", version=f"pinch {__version__}")
    args = p.parse_args(argv)

    cfg = load_config(args.config, {
        "style": args.style, "mode": args.mode, "model": args.model, "savings": args.savings, "base_url": args.base_url,
        "max_steps": args.max_steps, "session_dir": args.session_dir, "auto_approve": args.auto_approve or None,
    })

    if args.resume:
        session = Session(find_session(cfg.session_dir, args.resume))
        meta = session.meta()
        repo = Path(meta["repo"])
        cfg.mode = args.mode or meta.get("mode", cfg.mode)
        cfg.style = args.style or session.last_style() or cfg.style
    else:
        repo = Path(args.repo or ".").expanduser().resolve()
        if not repo.is_dir():
            raise SystemExit(f"pinch: {repo} is not a folder")
        session = None

    policy = load_policy(repo, listed=cfg.confidential_repos)  # read once, here, at startup
    if session is None:
        session = Session.create(cfg.session_dir, {
            "repo": str(policy.repo), "model": cfg.model, "mode": cfg.mode, "style": cfg.style,
            "confidential": policy.confidential, "confidential_reasons": list(policy.reasons), "savings": cfg.savings,
        })
    agent = build_agent(cfg, policy, session, resume=bool(args.resume))

    say(f"pinch session {session.id}  repo {repo}  model {agent.client.model}  tools {cfg.mode}  style {cfg.style}")
    if agent.policy.confidential:
        say("This repo is confidential: " + "; ".join(agent.policy.reasons))
    if cfg.auto_approve:
        say("--auto-approve is on: writes, shell and git will run without asking. Each one is logged.")

    try:
        task = args.task
        resuming_unfinished = bool(args.resume) and agent.messages and agent.messages[-1]["role"] in ("user", "tool")
        if task or resuming_unfinished:
            result = agent.run(task)
            print(result.text)
            if task or not sys.stdin.isatty():
                return 0 if result.stop == "done" else 1
        return repl(agent)
    except KeyboardInterrupt:
        say(f"\nStopped. The session is saved. Resume it with: pinch --resume {session.id}")
        return 130


def repl(agent: Agent) -> int:
    say(HELP)
    while True:
        try:
            line = input("pinch> ").strip()
        except EOFError:
            return 0
        if not line:
            continue
        if line.startswith("/"):
            command, _, rest = line[1:].partition(" ")
            if command in ("quit", "exit"):
                return 0
            elif command == "style":
                name = rest.strip()
                if name in STYLES:
                    agent.set_style(name)
                    say(f"Style is now {name}.")
                else:
                    say(f"Style is {agent.style}. Choose one of: {', '.join(STYLES)}")
            else:
                say(HELP)
            continue
        print(agent.run(line).text)


def ledger_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pinch ledger", description="Token and cost summary for a session.")
    p.add_argument("session", help="session id (or the start of one) or a path to its .jsonl file")
    p.add_argument("--session-dir", type=Path)
    args = p.parse_args(argv)
    session_dir = args.session_dir or load_config().session_dir
    print(summarize(Session(find_session(session_dir, args.session))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
