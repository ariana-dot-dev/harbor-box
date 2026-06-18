"""End-to-end evals: a from-scratch Claude Agent SDK agent solving real tasks
inside a Harbor-managed Ascii Box.

Each eval is the canonical Harbor loop, exercised against live services:

    BoxEnvironment.start()            # provision a Harbor-managed Box (adapter)
    BoxClaudeAgent.setup(env)         # deploy the TS agent + npm install in-box
    BoxClaudeAgent.run(task, env)     # run the agent inside the Box
    verify(env) -> pass/fail          # score by inspecting the Box

Requires BOX_API_KEY and ANTHROPIC_API_KEY. Skipped otherwise. These call real
APIs and cost real Anthropic tokens, so they are marked ``real_box``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from harbor.environments.base import ExecResult
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from harbor_box_environment import BoxEnvironment
from box_claude_agent import BoxClaudeAgent

pytestmark = pytest.mark.real_box

_WORKDIR = "/workspace"
_MODEL = os.environ.get("HARBOR_BOX_EVAL_MODEL", "sonnet")

_requires_keys = pytest.mark.skipif(
    not (
        os.environ.get("BOX_API_KEY")
        and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    ),
    reason="BOX_API_KEY and (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN) are required for end-to-end Box evals",
)


@dataclass
class Eval:
    name: str
    instruction: str
    seed_files: dict[str, str]  # path relative to workdir -> contents
    verify: Callable[[BoxEnvironment], Awaitable[tuple[bool, str]]]


def _make_env(tmp_path: Path, seed_files: dict[str, str]) -> BoxEnvironment:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    # A Dockerfile is required by Harbor's environment-definition check. Box runs
    # its default image, but the WORKDIR drives the agent's working directory.
    (environment_dir / "Dockerfile").write_text(
        f"FROM ubuntu:24.04\nWORKDIR {_WORKDIR}\n", encoding="utf-8"
    )
    # Seed files are uploaded to the workdir by BoxEnvironment.start().
    for rel, content in seed_files.items():
        dest = environment_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    trial_paths = TrialPaths(tmp_path / "trial")
    trial_paths.mkdir()
    return BoxEnvironment(
        environment_dir=environment_dir,
        environment_name=f"eval-{os.getpid()}",
        session_id="harbor-box-eval",
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(workdir=_WORKDIR),
        ttl_seconds=900,
    )


async def _run_eval(tmp_path: Path, ev: Eval) -> None:
    env = _make_env(tmp_path, ev.seed_files)
    env.default_user = "user"
    agent = BoxClaudeAgent(
        logs_dir=tmp_path / "agent-logs",
        model=_MODEL,
        max_turns=40,
        workdir=_WORKDIR,
        agent_timeout_sec=900,
    )
    from harbor.models.agent.context import AgentContext

    await env.start(force_build=False)
    try:
        await agent.setup(env)
        await agent.run(ev.instruction, env, AgentContext())
        ok, detail = await ev.verify(env)
        assert ok, (
            f"[{ev.name}] verifier failed: {detail}\n"
            f"agent summary: {agent.last_result}"
        )
    finally:
        await env.stop(delete=True)


# --------------------------------------------------------------------------- #
# Eval 1: build a program from scratch and prove it runs.
# --------------------------------------------------------------------------- #
async def _verify_fizzbuzz(env: BoxEnvironment) -> tuple[bool, str]:
    result: ExecResult = await env.exec(
        "cd /workspace && python3 fizzbuzz.py", timeout_sec=60
    )
    if result.return_code != 0:
        return False, f"fizzbuzz.py did not run cleanly: {result.stderr or result.stdout}"
    expected = "\n".join(
        "FizzBuzz" if n % 15 == 0 else "Fizz" if n % 3 == 0 else "Buzz" if n % 5 == 0 else str(n)
        for n in range(1, 16)
    )
    got = result.stdout.strip()
    return (got == expected), f"output mismatch.\nexpected:\n{expected}\ngot:\n{got}"


FIZZBUZZ = Eval(
    name="fizzbuzz",
    instruction=(
        "In the current directory, create a Python file named fizzbuzz.py. It must define "
        "fizzbuzz(n) returning 'Fizz' for multiples of 3, 'Buzz' for multiples of 5, "
        "'FizzBuzz' for multiples of both, and str(n) otherwise. Add an "
        "if __name__ == '__main__' block that prints fizzbuzz(1) through fizzbuzz(15), "
        "one value per line. Run it to confirm it works."
    ),
    seed_files={},
    verify=_verify_fizzbuzz,
)


# --------------------------------------------------------------------------- #
# Eval 2: fix a bug so a pre-seeded failing test suite passes.
# --------------------------------------------------------------------------- #
_BUGGY_MODULE = '''\
def is_palindrome(s):
    # BUG: should ignore case and non-alphanumeric characters.
    return s == s[::-1]
'''

_TEST_MODULE = '''\
import unittest
from stringutils import is_palindrome


class IsPalindromeTest(unittest.TestCase):
    def test_simple(self):
        self.assertTrue(is_palindrome("racecar"))

    def test_case_insensitive(self):
        self.assertTrue(is_palindrome("RaceCar"))

    def test_ignores_punctuation_and_spaces(self):
        self.assertTrue(is_palindrome("A man, a plan, a canal: Panama"))

    def test_negative(self):
        self.assertFalse(is_palindrome("hello world"))


if __name__ == "__main__":
    unittest.main()
'''


async def _verify_palindrome(env: BoxEnvironment) -> tuple[bool, str]:
    result = await env.exec("cd /workspace && python3 -m unittest -v", timeout_sec=120)
    return (result.return_code == 0), f"unittest still failing:\n{result.stderr or result.stdout}"


PALINDROME_FIX = Eval(
    name="palindrome-bugfix",
    instruction=(
        "The current directory contains stringutils.py and a failing unittest suite "
        "test_stringutils.py. Run `python3 -m unittest -v` to see the failures, then fix "
        "stringutils.py so every test passes. The is_palindrome function must ignore "
        "letter case and any non-alphanumeric characters. Do not modify the test file. "
        "Re-run the tests to confirm they all pass."
    ),
    seed_files={"stringutils.py": _BUGGY_MODULE, "test_stringutils.py": _TEST_MODULE},
    verify=_verify_palindrome,
)


@_requires_keys
@pytest.mark.asyncio
async def test_eval_fizzbuzz_from_scratch(tmp_path):
    await _run_eval(tmp_path, FIZZBUZZ)


@_requires_keys
@pytest.mark.asyncio
async def test_eval_palindrome_bugfix(tmp_path):
    await _run_eval(tmp_path, PALINDROME_FIX)
