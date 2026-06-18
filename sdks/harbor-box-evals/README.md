# Harbor evals on Ascii Box

Run Harbor evals where the agent works inside a **Harbor-managed Ascii Box**, driven
by a **from-scratch coding agent built on the Claude Agent SDK** (TypeScript). The
agent's whole brain is `agent/agent.mjs` plus the Claude Agent SDK — Box's built-in
agent/prompt endpoint is never used.

## Pieces

| File | Role |
| --- | --- |
| `../harbor_box_environment.py` | `BoxEnvironment`: Harbor `BaseEnvironment` backed by the Ascii Box API |
| `agent/agent.mjs` | From-scratch coding agent using `@anthropic-ai/claude-agent-sdk` |
| `box_claude_agent.py` | `BoxClaudeAgent`: Harbor `BaseAgent` that deploys + runs the TS agent in the Box |
| `evals_test.py` | End-to-end eval use-cases (Box + Claude Agent SDK) |
| `example_run.py` | The same loop as a plain runnable script |

## Flow

```mermaid
sequenceDiagram
    participant E as Eval (Harbor loop)
    participant Env as BoxEnvironment (adapter)
    participant Box as Ascii Box
    participant Ag as agent.mjs (Claude Agent SDK)
    participant LLM as Anthropic API

    E->>Env: start()
    Env->>Box: create + wait ready, upload workdir
    E->>Ag: setup() — upload agent.mjs, npm install (in Box)
    E->>Ag: run(instruction)
    Ag->>Box: node agent.mjs (inside the Box)
    loop agent turns
        Ag->>LLM: messages + tool results
        LLM-->>Ag: next tool use (Bash/Edit/Write/...)
        Ag->>Box: execute tool on Box filesystem
    end
    E->>Box: verify (run check command)
    E->>Env: stop()
```

## Run the evals

```bash
pip install "harbor>=0.14.0" httpx tenacity pytest pytest-asyncio
export BOX_API_KEY=...
# Authenticate the in-Box agent with EITHER an API key...
export ANTHROPIC_API_KEY=...
# ...OR a Claude subscription token (run `claude setup-token` once):
# export CLAUDE_CODE_OAUTH_TOKEN=...

pytest sdks/harbor-box-evals/evals_test.py -m real_box -v
# or the standalone example:
python sdks/harbor-box-evals/example_run.py
```

Credentials can also live in a gitignored `.env` at the repo root (auto-loaded
by the test `conftest.py`) instead of being exported.

The two bundled use-cases: build `fizzbuzz.py` from scratch and prove its output, and
fix a bug so a pre-seeded `unittest` suite passes. Both score by running a check command
inside the Box after the agent finishes.

## Wire it into Harbor directly

```bash
harbor task run task.yaml \
  --environment-import-path harbor_box_environment:BoxEnvironment \
  --agent-import-path box_claude_agent:BoxClaudeAgent
```

## Use a different model

The agent model defaults to `sonnet`. Override per run:

```bash
HARBOR_BOX_EVAL_MODEL=haiku pytest sdks/harbor-box-evals/evals_test.py -m real_box
AGENT_MODEL=opus python sdks/harbor-box-evals/example_run.py
```
