# Gateway: Bifrost in front of the mock providers

`config.json` is the whole gateway setup, as code. It is loaded by the pinned image `maximhq/bifrost:v2.5.0`
(never `latest`).

| What | Where in `config.json` |
|---|---|
| Providers `cloud-a`, `cloud-b`, `local` (mock servers on 127.0.0.1:8101, :8102, :8103, OpenAI-compatible custom providers) | `providers` |
| Users `alice` ($1.00), `bob` ($0.25), `penny` ($0.01 demo user) | `governance.virtual_keys` + `governance.budgets` |
| Fallback chain cloud-a, then cloud-b, then local, one rule per user key | `governance.routing_rules` (scope `virtual_key`) |
| `confidential` key, allowed to reach **only** `local` | `governance.virtual_keys` (`vk-confidential`, one provider config, `allow_all_providers: false`) |
| Mock model price ($1 / $4 per million tokens), so budgets move | `governance.pricing_overrides` |
| Admin API login, needed to read budgets and check the confidential key | `governance.auth_config` |

Real providers (Gemini, Groq, OpenRouter, Anthropic) and a local Gemma 4 E2B on Ollama are in
`providers.real.example.json` as disabled templates. Their key names are in `../.env.example`.

## Run it

```sh
cd pinch
uv run pinch prove                   # starts mocks and the gateway for you, runs 4 scenarios, cleans up
# or by hand:
docker compose -f gateway/docker-compose.yml up -d       # gateway on http://127.0.0.1:8190 (takes ~15 s)
uv run pinch-mock examples/fix_calc.json --port 8101 &   # likewise 8102 and 8103
set -a; . gateway/demo.env; set +a
PINCH_BASE_URL=http://127.0.0.1:8190/v1 PINCH_GATEWAY_KEY=$PINCH_VK_ALICE uv run pinch "Fix the failing test"
```

`demo.env` holds demo-only values for a throwaway gateway. They are not secrets.
The mock stack listens on **8190** and the real gateway (below) on **8180**, so `pinch prove` and `python -m pinch.bench smoke` keep working
while the real gateway runs. If something that is not the demo gateway answers on the mock port, they stop with a plain message and touch nothing.

## What was checked against Bifrost v2.5.0

- Virtual key header: `x-bf-vk` (also `Authorization: Bearer sk-bf-...`). Keys loaded from config.json must start `sk-bf-` to be accepted in `Authorization`.
- Who served a request: `extra_fields.routing_info.{provider,model,is_fallback,primary_provider}` in the body and
  `x-bifrost-routing-info-*` headers.
- Budget exceeded: HTTP 402, type `budget_exceeded`. Usage is added to the key a moment after the call.
- A key limited to `local` gets HTTP 403 `provider_blocked` for anything else, even when a global routing rule or the request's `fallbacks` name a cloud provider.
- The admin API (`/api/governance/...`) is locked until `governance.auth_config` and a setup token exist; then it takes HTTP Basic.

Sources: docs.getbifrost.ai and github.com/maximhq/bifrost (docs/ folder): features/governance/virtual-keys,
features/governance/budget-and-limits, features/governance/routing, features/retries-and-fallbacks,
providers/custom-providers, providers/custom-pricing, providers/response-headers, providers/routing-rules,
deployment-guides/config-json/governance.

## Real providers

`config.real.json` + `docker-compose.real.yml` put the same Bifrost image (`maximhq/bifrost:v2.5.0`) in front of real models. The config holds only
`env.NAME` references; the values live in `../.env` (gitignored). Models: small `gemini-3.5-flash-lite`, big `gemini-3.6-flash`, failover `groq` `qwen/qwen3.8-27b`, local `gemma4` on LM Studio (`http://127.0.0.1:1234`, which you run yourself; pinch never starts or loads a model).

| Virtual key (id, env name) | Budget / month | What it can reach | Used for |
|---|---|---|---|
| `vk-me` (`PINCH_GATEWAY_KEY`) | $2.00 | gemini, then groq qwen3.8-27b, then local gemma4 | everyday pinch use |
| `vk-bench` (`PINCH_BENCH_KEY`) | $15.00 | gemini only, **no fallback** | `pinch.bench run`: a benchmark row never silently changes model |
| `vk-confidential` (`PINCH_CONFIDENTIAL_KEY`) | none | **local only** (`allow_all_providers: false`) | repos with a `.pinch-confidential` marker |
| `vk-demo-low` (`PINCH_DEMO_LOW_KEY`) | $0.01 | gemini only | showing the budget stop on camera |
| `vk-failover-demo` (`PINCH_FAILOVER_DEMO_KEY`) | $0.50 | `gemini-down` (a custom provider pointing at the closed port 127.0.0.1:9), then groq qwen3.8-27b | showing a real failover without breaking your own key |

### Start, check, stop

Run these from the `pinch/` folder. First, once: `uv run python scripts/init_env.py` fills the empty virtual keys, admin password and setup token in `.env` with random values (it prints names only; provider keys are never touched).

```sh
# start (needs the Docker Compose plugin)
docker compose --env-file .env -f gateway/docker-compose.real.yml up -d
# start without the plugin: the same container, built from the same compose file
uv run python scripts/real_gateway.py up

uv run python scripts/real_gateway.py status        # container, image tag, /health
curl -s http://127.0.0.1:8180/health                # {"status":"ok"}-style answer when it is up

docker compose --env-file .env -f gateway/docker-compose.real.yml down      # stop (spend is kept)
uv run python scripts/real_gateway.py down                                   # the same without the plugin
uv run python scripts/real_gateway.py down --volume                          # stop AND forget all spend
uv run python scripts/real_gateway.py reset vk-demo-low                      # zero one key's spend (repeat the budget demo)
```

Only the variables listed under `environment:` in the compose file reach the container (`TAVILY_API_KEY` does not: pinch calls Tavily itself). A test checks that every `env.NAME` in `config.real.json` is passed there and listed in `.env.example`.

### Choices worth knowing

- **Fallbacks.** A routing rule per key (scope `virtual_key`) names the provider and leaves out the model, so the model pinch asked for (small or big) is kept; its `fallbacks` must name a model: `groq/qwen/qwen3.8-27b`, `local/gemma4` (the mock config's `"cloud-b/"` form means "same model name", which does not work across Gemini, Groq and a local model). Verified on the running gateway, including the third hop to local. A rule that names a provider also **re-routes a request for another provider's model** to that provider first (asking `vk-me` for a Groq model goes to Gemini, fails, then falls back to Groq): ask for the model you mean on its own provider's key, or use the failover key.
- **Bifrost reports the first error.** When the whole chain fails, the error body is the primary provider's (the failover demo shows `502 connection refused` even when Groq answered 429 or 400). The Bifrost request log (`/api/logs`) shows each attempt.
- **Spend survives restarts.** `/app/data` is a named volume (`pinch-bifrost-real-data`), so a restart or `down` keeps each key's budget usage (checked). A `down --volume` starts them from zero. Chosen because a restart should not quietly hand back a $2 limit; budgets reset by themselves every month.
- **Prices.** `pricing_overrides` use the list prices (input, output, and for Gemini cached input via `cache_read_input_token_cost`); `src/pinch/prices.toml` holds the same table (a test compares them), so pinch's pre-call estimate and Bifrost's accounting agree. Local gemma4 costs $0.
- **Free tiers cost $0 in reality.** The Gemini and Groq keys are free-tier, so nothing is billed. The budgets count **list-price** cost anyway, so limits and the budget stop behave as they would on paid keys.
- **Failover model: `qwen/qwen3.8-27b` ($0.80 in, $4.00 out per 1M).** Same task through the failover key (gemini-down, then Groq, lazy tools): qwen finished it twice (4 and 5 calls, tests pass); `openai/gpt-oss-120b` failed in all four runs, each time with a malformed or unknown tool call (HTTP 400 `tool_use_failed`) after 1 to 5 good calls. Both stay allowed on the Groq provider config, so you can compare by changing the fallback in `config.real.json`.
- **Limits seen.** Groq's free plan (8K tokens per minute per model) is only good as a failover; Gemini's free-tier limits are shown in AI Studio, so expect 429s on long runs. The bench harness waits them out. pinch retries a provider 400 `tool_use_failed` up to 2 more times and records `tool_use_failed_retry`, but behind the gateway that body is not visible when the failover chain fails (Bifrost returns the first provider's error), so there pinch's ordinary gateway retries apply.
- **Local model.** `PINCH_LOCAL_MODEL=local/gemma4` is the model name pinch sends on the confidential key and after "continue on the local tier" (the cloud model names are blocked there). On that tier pinch also (1) offers a small direct tool set in full mode (`PINCH_LOCAL_TOOLS`, default `read_file,list_directory,grep_search,edit_file,write_file,run_test,run_command`) instead of the lazy meta-tools, (2) adds a two-sentence hint to the system prompt, (3) asks the model once more (at most 3 times a session) when it sends an empty reply or call-like text that is not a valid call, and (4) reads tool calls the model writes as text. LM Studio returns Gemma 4's call as text when its own parser does not accept it, for example `edit_file{file_path:<|"|>calc.py<|"|>,...}<tool_call|>`. `src/pinch/texttools.py` turns that into a normal `tool_calls` message only if the **whole** reply is a call (no prose, quote or code fence around it), the tool was offered, the arguments parse completely and fit the tool's schema. It then goes through the same policy check and approval gate as any call, and the session records `parsed_text_tool_call`. With this, gemma4 fixed the demo repo in 3 to 4 calls (twice in two tries). Its first edit guesses the file's text without reading it and can break the file; it then reads the file and repairs it.
