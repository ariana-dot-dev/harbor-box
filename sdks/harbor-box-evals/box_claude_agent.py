"""Harbor agent that runs a from-scratch Claude Agent SDK agent inside an Ascii Box.

This is the thin Harbor :class:`~harbor.agents.base.BaseAgent` integration. The
*brain* is the TypeScript program in ``agent/agent.mjs`` (built on the Claude
Agent SDK); this wrapper only deploys it into a Harbor-managed Box and runs it
there. It never touches Box's built-in agent/prompt endpoint.

Wiring (Harbor):

    harbor task run task.yaml \\
        --environment-import-path harbor_box_environment:BoxEnvironment \\
        --agent-import-path box_claude_agent:BoxClaudeAgent
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import override

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_AGENT_SRC_DIR = Path(__file__).resolve().parent / "agent"
_AGENT_FILES = ("agent.mjs", "package.json", "package-lock.json")


class BoxClaudeAgent(BaseAgent):
    """Deploys ``agent/agent.mjs`` into the Box and runs it on the instruction."""

    SUPPORTS_WINDOWS = False

    def __init__(
        self,
        logs_dir: Path,
        *,
        model: str = "sonnet",
        max_turns: int = 40,
        anthropic_api_key: str | None = None,
        oauth_token: str | None = None,
        agent_src_dir: Path = _AGENT_SRC_DIR,
        remote_dir: str | None = None,
        workdir: str = "/workspace",
        agent_timeout_sec: int = 900,
        **kwargs,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model, **kwargs)
        self._model = model
        self._max_turns = max_turns
        # Either an Anthropic API key or a Claude subscription token
        # (`claude setup-token` -> CLAUDE_CODE_OAUTH_TOKEN) authenticates the SDK.
        self._api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._oauth_token = oauth_token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        self._agent_src_dir = Path(agent_src_dir)
        self._remote_dir = remote_dir  # resolved against $HOME in setup() if None
        self._workdir = workdir
        self._agent_timeout_sec = agent_timeout_sec
        self._resolved_remote_dir: str | None = None
        self.last_result: dict | None = None

    @staticmethod
    @override
    def name() -> str:
        return "box-claude-agent"

    @override
    def version(self) -> str:
        return "0.1.0"

    async def _resolve_remote_dir(self, environment: BaseEnvironment) -> str:
        if self._remote_dir:
            return self._remote_dir
        home = (await environment.exec("printf %s \"$HOME\"", timeout_sec=30)).stdout.strip() or "/home/user"
        return f"{home}/.harbor-box-agent"

    def _credential_env(self) -> dict[str, str]:
        """Anthropic credentials forwarded into the Box for the agent SDK."""
        env: dict[str, str] = {}
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        if self._oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self._oauth_token
        return env

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        if not (self._api_key or self._oauth_token):
            raise RuntimeError(
                "Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN for BoxClaudeAgent."
            )
        remote_dir = await self._resolve_remote_dir(environment)
        self._resolved_remote_dir = remote_dir

        # Upload only the agent sources (never node_modules) and install in-box.
        await environment.exec(f"mkdir -p {shlex.quote(remote_dir)}", timeout_sec=30)
        for filename in _AGENT_FILES:
            src = self._agent_src_dir / filename
            if not src.exists():
                raise FileNotFoundError(f"Missing agent source file: {src}")
            await environment.upload_file(src, f"{remote_dir}/{filename}")

        install = await environment.exec(
            "npm install --omit=dev --no-audit --no-fund",
            cwd=remote_dir,
            timeout_sec=420,
        )
        if install.return_code != 0:
            raise RuntimeError(f"npm install failed in Box: {install.stderr or install.stdout}")
        self.logger.info("BoxClaudeAgent installed at %s", remote_dir)

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        remote_dir = self._resolved_remote_dir or await self._resolve_remote_dir(environment)
        log_path = f"{remote_dir}/agent-trajectory.jsonl"
        await environment.exec(f"mkdir -p {shlex.quote(self._workdir)}", timeout_sec=30)

        result = await environment.exec(
            f"node {shlex.quote(remote_dir + '/agent.mjs')} {shlex.quote(instruction)}",
            env={
                **self._credential_env(),
                "AGENT_CWD": self._workdir,
                "AGENT_MODEL": self._model,
                "AGENT_MAX_TURNS": str(self._max_turns),
                "AGENT_LOG": log_path,
            },
            timeout_sec=self._agent_timeout_sec,
        )

        summary = self._parse_result(result.stdout)
        self.last_result = summary
        context.cost_usd = (summary or {}).get("total_cost_usd")
        context.metadata = {
            "agent": self.name(),
            "model": self._model,
            "exit_code": result.return_code,
            "result": summary,
        }

        # Best-effort: pull the trajectory back to the host logs for inspection.
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            await environment.download_file(log_path, self.logs_dir / "agent-trajectory.jsonl")
        except Exception as exc:  # noqa: BLE001 - logging only, never fail the run
            self.logger.warning("Could not download agent trajectory: %s", exc)

    @staticmethod
    def _parse_result(stdout: str) -> dict | None:
        for line in reversed((stdout or "").splitlines()):
            if line.startswith("AGENT_RESULT "):
                try:
                    return json.loads(line[len("AGENT_RESULT "):])
                except json.JSONDecodeError:
                    return None
        return None
