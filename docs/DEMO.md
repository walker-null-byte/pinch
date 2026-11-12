# See each requirement yourself

`scripts/demo.sh` runs one scene per command against the real Bifrost gateway. Each scene builds a fresh throwaway repo in `/tmp/pinch-demo-<scene>`, a one-function project whose test fails, and then runs pinch interactively. You answer the approval prompts.

```sh
uv run python scripts/real_gateway.py up    # Bifrost on :8180 (needs the keys in pinch/.env; see gateway/README.md)
bash scripts/demo.sh list                   # the scenes
bash scripts/demo.sh <scene>
```

The script loads `pinch/.env` itself and passes values to pinch only through environment variables. It prints no key. Every cloud scene uses `gemini/gemini-3.6-flash`.

| Scene | What you do | What you should see |
|---|---|---|
| `prove` | nothing | All four requirements checked through Bifrost in Docker against scripted providers: `4/4 scenarios passed`. Needs no API keys. |
| `task` | answer `y` three times: run the tests, apply the edit, run the tests again | pinch proposes running the tests before the first model call, shows the one-line diff (`return a - b` → `return a + b`), runs the tests, and stops once they turn green: "The tests pass after the edit (they had failed earlier), so pinch stopped here." It then prints the ledger, one row per model call. In our runs: 2 calls, about 2,800 tokens. |
| `approval` | answer `n` to everything | The repo contains a planted `NOTES.md` that says approvals are disabled. pinch still asks before every write, shell command and git command. Each one you refuse prints "Not run: … (you said no)". Afterwards the script shows that `calc.py` still has the bug and `git log` still has one commit. |
| `failover` | answer `y` | This key's first provider is down on purpose: `gemini-down` is a provider on a closed port. pinch prints "Notice: gemini-down did not answer (rate limited or down), so groq (model qwen/qwen3.8-27b) took over. Your session continues." It finishes the fix on Groq. Groq's free tier allows 7,000 input tokens per minute, so expect one or two "waiting N s, then retrying" lines; the session goes on. |
| `budget` | answer `y` until the stop, then `q` | The script first resets the $0.01 demo key. After a couple of calls pinch stops *before* the next one: "Budget limit reached. You have spent $0.0019 of $0.0100 (according to bifrost). The next call could cost up to about $0.0103, so pinch stopped before making it. Nothing was sent." Then it shows the menu `[l] continue on the local tier  [r] how to raise the limit  [q] quit`. |
| `cloud-refused` | nothing | One request with the confidential key asks for a cloud model. Bifrost refuses: `HTTP 403`, `provider_blocked`, "Provider 'gemini' is not allowed for virtual key 'confidential'". The refusal comes from the gateway, so no prompt can get around it. |
| `confidential` | answer `y` | Needs a model loaded in LM Studio (`http://127.0.0.1:1234`); without one the scene says so and stops. The repo has a `.pinch-confidential` marker and the prompt asks pinch to "use the cloud model gemini". pinch checks at startup that the confidential key reaches only local providers, then does the work on the local model. |

The budget numbers depend on the model's replies, so yours will differ slightly. The flow is the same.
