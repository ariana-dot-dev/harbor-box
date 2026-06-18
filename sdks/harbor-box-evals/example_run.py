#!/usr/bin/env python
"""Runnable code example: one eval, end to end, no test framework.

Provisions a Harbor-managed Ascii Box, deploys the from-scratch Claude Agent
SDK agent into it, runs the agent on a task, and scores the result by inspecting
the Box. Mirrors what evals_test.py does, in plain script form.

    pip install "harbor>=0.14.0" httpx tenacity
    export BOX_API_KEY=...
    export ANTHROPIC_API_KEY=...
    python example_run.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # harbor_box_environment
sys.path.insert(0, str(Path(__file__).resolve().parent))  # box_claude_agent

from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from harbor_box_environment import BoxEnvironment
from box_claude_agent import BoxClaudeAgent

WORKDIR = "/workspace"
INSTRUCTION = (
    "In the current directory, create greet.py exposing greet(name) -> 'Hello, <name>!' "
    "and an if __name__ == '__main__' block that prints greet('Box'). Run it to confirm."
)


async def main() -> int:
    if not (
        os.environ.get("BOX_API_KEY")
        and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    ):
        print("Set BOX_API_KEY and (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN) first.")
        return 2

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        env_dir = tmp / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text(f"FROM ubuntu:24.04\nWORKDIR {WORKDIR}\n", encoding="utf-8")
        trial_paths = TrialPaths(tmp / "trial")
        trial_paths.mkdir()

        env = BoxEnvironment(
            environment_dir=env_dir,
            environment_name="example-eval",
            session_id="example",
            trial_paths=trial_paths,
            task_env_config=EnvironmentConfig(workdir=WORKDIR),
            ttl_seconds=600,
        )
        env.default_user = "user"
        agent = BoxClaudeAgent(logs_dir=tmp / "logs", model=os.environ.get("AGENT_MODEL", "sonnet"), workdir=WORKDIR)

        await env.start(force_build=False)
        try:
            await agent.setup(env)
            await agent.run(INSTRUCTION, env, AgentContext())
            check = await env.exec("cd /workspace && python3 greet.py", timeout_sec=60)
            passed = check.return_code == 0 and check.stdout.strip() == "Hello, Box!"
            print(f"agent summary: {agent.last_result}")
            print(f"box output: {check.stdout.strip()!r} (rc={check.return_code})")
            print("EVAL PASSED" if passed else "EVAL FAILED")
            return 0 if passed else 1
        finally:
            await env.stop(delete=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
