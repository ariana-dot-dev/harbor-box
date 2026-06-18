"""Harbor environment adapter backed by the Ascii Box public API.

``BoxEnvironment`` is a Harbor :class:`harbor.environments.base.BaseEnvironment`
implementation that runs Harbor trials inside an Ascii Box. It implements the
Harbor environment surface the public Box API supports (lifecycle, command
execution, environment variables, file/dir upload+download, mount-dir
preparation). Features Box does not provide today — network policies
(no-network/allowlist), Docker image builds/Compose, GPUs/TPUs, and Windows —
are validated and rejected at start, so a task never gets a guarantee the box
cannot keep.

Usage::

    pip install "harbor>=0.14.0" httpx tenacity
    export BOX_API_KEY=...
    harbor task run path/to/task.yaml \\
        --environment-import-path harbor_box_environment:BoxEnvironment

End-to-end self-test (talks to the real Box API, requires BOX_API_KEY)::

    BOX_API_KEY=... python sdks/harbor_box_environment.py
"""

from __future__ import annotations

import base64
import os
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Iterable, Sequence, override

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities, EnvironmentResourceCapabilities
from harbor.environments.definition import require_agent_environment_definition
from harbor.models.task.config import EnvironmentConfig, NetworkMode
from harbor.models.trial.paths import TrialPaths

_DEFAULT_BASE_URL = "https://ascii.dev/api/box/v1"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_BOX_TTL_SECONDS = 86_400
# Box-internal names the create API rejects with `invalid_env`; filtered out of
# any per-box env the adapter forwards so a stray task var can't fail start().
_RESERVED_BOX_ENV: frozenset[str] = frozenset(
    {"ASCII_TOKEN", "ASCII_API_URL", "AGENT_ID", "PRODUCT_MODE", "ENVIRONMENT_ID", "BOX_ID", "SERVICE_PREVIEW_TOKEN", "BOX_CLI_TOKEN"}
)
_TRANSIENT_HTTPX_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class BoxApiError(RuntimeError):
    """Raised when the Box HTTP API returns an error response."""

    def __init__(self, status_code: int, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


class AsyncBoxClient:
    """Small async client for the Ascii Box public API used by Harbor.

    It intentionally exposes only runtime substrate operations: box lifecycle,
    commands, and files. It does not call Box's built-in agent/prompt endpoint.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("BOX_API_KEY")
        if not self._api_key:
            raise RuntimeError("Box requires BOX_API_KEY to be set, or api_key=... to be passed.")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self._api_key}"
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")
        if self._client is not None:
            response = await self._client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)
        text = response.text
        payload: dict[str, Any] = response.json() if text else {}
        if response.status_code >= 400 or payload.get("ok") is False:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
            code = str(error.get("code") or payload.get("code") or "box_api_error")
            message = str(error.get("message") or payload.get("message") or response.reason_phrase)
            raise BoxApiError(response.status_code, code, message, error.get("details") or payload.get("details"))
        return payload

    async def create(
        self,
        *,
        name: str | None = None,
        ttl_seconds: int | None = _DEFAULT_BOX_TTL_SECONDS,
        no_env: bool = False,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds
        if no_env:
            # Withhold every account secret/credential/cloned repo from the box and
            # confine it to itself. The right default for boxes you hand to others.
            body["noEnv"] = True
        if env:
            body["env"] = env
        payload = await self._request("POST", "/boxes", json=body)
        box = payload["box"]
        if name:
            box = await self.update(box["id"], name=name, ttl_seconds=ttl_seconds)
        return box

    async def get(self, box_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/boxes/{box_id}"))["box"]

    async def update(self, box_id: str, *, name: str | None = None, ttl_seconds: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds
        return (await self._request("PATCH", f"/boxes/{box_id}", json=body))["box"]

    async def resume(self, box_id: str) -> dict[str, Any]:
        payload = await self._request("POST", f"/boxes/{box_id}/resume")
        return payload.get("box") or {"id": box_id, "ok": payload.get("ok", True)}

    async def stop(self, box_id: str) -> dict[str, Any]:
        payload = await self._request("POST", f"/boxes/{box_id}/stop")
        return payload.get("box") or {"id": box_id, "ok": payload.get("ok", True)}

    async def command(
        self,
        box_id: str,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ExecResult:
        if env:
            prefix = "".join(f"export {key}={shlex.quote(value)}; " for key, value in env.items())
            command = f"{prefix}{command}"
        body = {
            "command": command,
            "cwd": cwd,
            "timeoutSeconds": timeout_seconds,
        }
        payload = await self._request("POST", f"/boxes/{box_id}/commands", json=body)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        return ExecResult(
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            return_code=int(result.get("exitCode", result.get("returnCode", 0))),
        )

    async def read_file(self, box_id: str, path: str, *, encoding: str = "utf8") -> str:
        if path.startswith("/"):
            data = await self.read_file_binary(box_id, path)
            if encoding in {"base64", "binary"}:
                return base64.b64encode(data).decode("ascii")
            return data.decode("utf-8", errors="replace")
        params = {"path": path, "encoding": encoding}
        payload = await self._request("GET", f"/boxes/{box_id}/files", params=params)
        file_payload = payload.get("file") if isinstance(payload.get("file"), dict) else {}
        return str(payload.get("content", file_payload.get("content", "")))

    async def write_file(self, box_id: str, path: str, content: str, *, encoding: str = "utf8") -> None:
        if path.startswith("/"):
            if encoding == "base64":
                data = base64.b64decode(content)
            else:
                data = content.encode("utf-8")
            await self.write_file_binary(box_id, path, data)
            return
        await self._request("PUT", f"/boxes/{box_id}/files", json={"path": path, "content": content, "encoding": encoding})

    async def read_file_binary(self, box_id: str, path: str) -> bytes:
        if path.startswith("/"):
            result = await self.command(box_id, f"test -f {shlex.quote(path)} && base64 -w0 {shlex.quote(path)}", timeout_seconds=120)
            if result.return_code != 0:
                raise FileNotFoundError(path)
            return base64.b64decode(result.stdout or "")
        try:
            content = await self.read_file(box_id, path, encoding="base64")
        except BoxApiError as exc:
            if exc.status_code == 404:
                raise FileNotFoundError(path) from exc
            raise
        return base64.b64decode(content)

    async def write_file_binary(self, box_id: str, path: str, content: bytes) -> None:
        encoded = base64.b64encode(content).decode("ascii")
        if path.startswith("/"):
            parent = str(PurePosixPath(path).parent)
            result = await self.command(
                box_id,
                f"mkdir -p {shlex.quote(parent)} && printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}",
                timeout_seconds=120,
            )
            if result.return_code != 0:
                raise BoxApiError(0, "box_write_failed", f"Failed to write {path!r} to Box: {result.stderr or result.stdout}".strip())
            return
        await self.write_file(box_id, path, encoded, encoding="base64")


def _parse_dockerfile_workdir(dockerfile_path: Path) -> str | None:
    """Lightweight final-stage WORKDIR parser.

    Harbor's helper depends on the optional ``dockerfile_parse`` extra used by
    image-building providers. Box does not build images, so keep this adapter
    importable without that extra while preserving the common WORKDIR behavior
    needed for command cwd defaults.
    """
    if not dockerfile_path.exists():
        return None
    current: PurePosixPath | None = None
    for raw_line in dockerfile_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        instruction = parts[0].upper()
        value = parts[1].strip() if len(parts) > 1 else ""
        if instruction == "FROM":
            current = None
        elif instruction == "WORKDIR" and value:
            value = value.strip('"\'')
            if value.startswith("/"):
                current = PurePosixPath(value)
            else:
                current = (current or PurePosixPath("/")) / value
    return str(current) if current is not None else None


def _quote(path: str | Path) -> str:
    return shlex.quote(str(path))


def _parents(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        parent = str(PurePosixPath(raw).parent)
        if parent not in (".", "") and parent not in seen:
            out.append(parent)
            seen.add(parent)
    return out


class BoxEnvironment(BaseEnvironment):
    """Harbor ``BaseEnvironment`` backed by Ascii Box.

    This mirrors Harbor's E2B environment surface as closely as Box's current
    public API allows: lifecycle, command execution, environment variables,
    file upload/download, directory transfer, and mount-directory preparation.
    Network allowlists/no-network, GPUs/TPUs, Windows, Docker Compose, and
    provider-side Docker image builds are rejected rather than silently ignored.

    Isolation: boxes are created **no-env by default** (``no_env=True``) so a
    trial gets none of your Box account's secrets, secret files, credentials, or
    cloned private repos, and cannot act on your account or other boxes — the
    right default for evals and for boxes you hand to others. Pass
    ``no_env=False`` to opt a box into your account's personal environment, which
    you configure in the Box dashboard (Secrets and Repositories tabs). Either
    way, ``task_env_config.env`` is forwarded to the box as explicit per-box env.
    """

    _provider_label: ClassVar[str] = "box"

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        *,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        request_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        ttl_seconds: int | None = _DEFAULT_BOX_TTL_SECONDS,
        no_env: bool = True,
        client: AsyncBoxClient | None = None,
        **kwargs: Any,
    ) -> None:
        self._client = client or AsyncBoxClient(api_key=api_key, base_url=base_url, timeout_seconds=request_timeout_seconds)
        self._ttl_seconds = ttl_seconds
        self._no_env = no_env
        self._box: dict[str, Any] | None = None
        self._box_id: str | None = None
        self._dockerfile_workdir = _parse_dockerfile_workdir(environment_dir / "Dockerfile")
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            **kwargs,
        )

    @classmethod
    @override
    def preflight(cls) -> None:
        if not os.environ.get("BOX_API_KEY"):
            raise SystemExit("Box requires BOX_API_KEY to be set. Please set this environment variable and try again.")

    @staticmethod
    @override
    def type() -> str:
        return "box"

    @classmethod
    @override
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities()

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities()

    @property
    def box_id(self) -> str:
        if not self._box_id:
            raise RuntimeError("Box not found. Please start the environment first.")
        return self._box_id

    @property
    def _environment_definition_path(self) -> Path:
        return self.environment_dir / "Dockerfile"

    @override
    def _validate_definition(self) -> None:
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
        )
        cfg = self.task_env_config
        if cfg.docker_image:
            raise ValueError(
                "BoxEnvironment cannot build or select Docker images through the current Box API. "
                "Use a Dockerfile/environment directory that can run in the default Box image, or use E2B/Modal for image-backed Harbor tasks."
            )
        # Reject — never silently accept — what Box can't enforce, so a task never
        # gets a false sense of a guarantee the box doesn't provide.
        if cfg.network_mode != NetworkMode.PUBLIC or cfg.allowed_hosts or getattr(cfg, "allow_internet", None) is False:
            raise ValueError(
                f"BoxEnvironment cannot enforce network policies (network_mode={cfg.network_mode.value}, "
                f"allowed_hosts={cfg.allowed_hosts!r}); a Box runs with public network access. "
                "Use a provider that enforces network isolation if your task requires it."
            )
        box_os = getattr(getattr(cfg, "os", None), "value", None)
        if box_os is not None and str(box_os).lower() != "linux":
            raise ValueError(f"BoxEnvironment only runs Linux boxes; os={box_os!r} is not available through the Box API.")
        if cfg.gpus or cfg.gpu_types or cfg.tpu:
            raise ValueError("BoxEnvironment cannot provide GPUs or TPUs through the current Box API.")

    async def _wait_until_ready(self, timeout_seconds: float = 300.0) -> None:
        import time
        started = time.monotonic()
        while True:
            if self._box and self._box.get("state") in {"ready", "idle", "running"}:
                return
            if self._box and self._box.get("state") == "error":
                raise RuntimeError(f"Box {self.box_id} entered error state while starting")
            if time.monotonic() - started > timeout_seconds:
                state = self._box.get("state") if self._box else "unknown"
                raise TimeoutError(f"Timed out waiting for Box {self.box_id} to become ready; last state was {state}")
            import asyncio
            await asyncio.sleep(2)
            self._box = await self._client.get(self.box_id)

    def _box_create_env(self) -> dict[str, str] | None:
        """Per-box env to forward at creation: the task's configured env, minus
        Box-internal reserved names. For a no-env box this is the only way to hand
        it a secret of its own; the API merges it over any account env otherwise."""
        configured = getattr(self.task_env_config, "env", None) or {}
        forwarded = {k: str(v) for k, v in configured.items() if k not in _RESERVED_BOX_ENV}
        return forwarded or None

    @retry(retry=retry_if_exception_type(_TRANSIENT_HTTPX_ERRORS), stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)
    async def _create_box(self) -> dict[str, Any]:
        return await self._client.create(
            name=f"harbor-{self.environment_name}-{self.session_id}"[:120],
            ttl_seconds=self._ttl_seconds,
            no_env=self._no_env,
            env=self._box_create_env(),
        )

    @override
    async def ensure_dirs(self, dirs: Sequence[Any], *, chmod: bool = True) -> ExecResult | None:
        if not dirs:
            return None
        # Harbor calls ensure_dirs() during start() to create the configured
        # workdir. Do not route that command through exec(), because exec()
        # intentionally applies the configured workdir and would try to cd into
        # the directory before it exists.
        return await self._client.command(
            self.box_id,
            f"sudo -n bash -lc {shlex.quote(self._ensure_dirs_command(dirs, chmod=chmod))}",
            cwd=None,
            env=None,
            timeout_seconds=120,
        )

    @override
    async def start(self, force_build: bool = False) -> None:
        if force_build:
            self.logger.debug("force_build=True has no effect for BoxEnvironment because Box does not expose build templates yet.")
        self._box = await self._create_box()
        self._box_id = str(self._box["id"])
        await self._wait_until_ready()
        bootstrap_dirs = self._mount_targets(writable_only=True)
        workdir = self.task_env_config.workdir or self._dockerfile_workdir
        if workdir:
            bootstrap_dirs.append(workdir)
        await self.ensure_dirs(bootstrap_dirs)
        if workdir:
            await self.upload_dir(self.environment_dir, workdir)
        else:
            await self._upload_environment_dir_after_start()

    @retry(retry=retry_if_exception_type(_TRANSIENT_HTTPX_ERRORS), stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)
    async def _stop_box(self) -> None:
        await self._client.stop(self.box_id)

    @override
    async def stop(self, delete: bool = True) -> None:
        if not self._box_id:
            return
        try:
            await self._stop_box()
        finally:
            self._box = None
            self._box_id = None

    @retry(retry=retry_if_exception_type(_TRANSIENT_HTTPX_ERRORS), stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)
    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        effective_user = self._resolve_user(user)
        if effective_user is not None:
            user_str = str(effective_user)
            if user_str == "root":
                command = f"sudo -n bash -lc {shlex.quote(command)}"
            elif user_str != "user":
                command = f"sudo -n -u {shlex.quote(user_str)} bash -lc {shlex.quote(command)}"
        effective_cwd = cwd or self.task_env_config.workdir or self._dockerfile_workdir
        box_cwd: str | None = effective_cwd
        if effective_cwd and effective_cwd.startswith("/"):
            command = f"cd {shlex.quote(effective_cwd)} && {command}"
            box_cwd = None
        return await self._client.command(
            self.box_id,
            command,
            cwd=box_cwd,
            env=self._merge_env(env),
            timeout_seconds=timeout_sec,
        )

    @retry(retry=retry_if_exception_type(_TRANSIENT_HTTPX_ERRORS), stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)
    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        # Parents are created as the default Box user (no user="root"): files are
        # written as that same user, so root-owned dirs would make the write fail.
        parents = _parents([target_path])
        if parents:
            await self.exec("mkdir -p " + " ".join(_quote(p) for p in parents))
        await self._client.write_file_binary(self.box_id, target_path, Path(source_path).read_bytes())

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir)
        files = [path for path in source.rglob("*") if path.is_file()]
        remote_paths = [str(PurePosixPath(target_dir) / path.relative_to(source).as_posix()) for path in files]
        parents = _parents(remote_paths)
        # See upload_file: create dirs as the default user, not root.
        await self.exec("mkdir -p " + " ".join(_quote(p) for p in [target_dir, *parents]))
        for path, remote_path in zip(files, remote_paths, strict=True):
            await self._client.write_file_binary(self.box_id, remote_path, path.read_bytes())

    @retry(retry=retry_if_exception_type(_TRANSIENT_HTTPX_ERRORS), stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)
    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await self._client.read_file_binary(self.box_id, source_path))

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        list_result = await self.exec(f"cd {_quote(source_dir)} && find . -type f", timeout_sec=120, user="root")
        if list_result.return_code != 0:
            raise RuntimeError(f"Failed to list Box directory {source_dir!r}: {list_result.stderr or list_result.stdout}")
        for rel in (line.strip() for line in (list_result.stdout or "").splitlines()):
            if not rel:
                continue
            rel_path = rel[2:] if rel.startswith("./") else rel
            await self.download_file(str(PurePosixPath(source_dir) / rel_path), target / rel_path)

    @override
    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        result = await self.exec(f"test -d {_quote(path)}", timeout_sec=30, user=user or "root")
        return result.return_code == 0

    @override
    async def is_file(self, path: str, user: str | int | None = None) -> bool:
        result = await self.exec(f"test -f {_quote(path)}", timeout_sec=30, user=user or "root")
        return result.return_code == 0


async def _run_e2e() -> None:
    """End-to-end self-test against the real Box API. Requires BOX_API_KEY.

    Exercises start -> persistent/per-command env -> binary upload/download ->
    nested directory upload/download -> stop, asserting round-trip fidelity.
    """
    import tempfile

    if not os.environ.get("BOX_API_KEY"):
        raise SystemExit("BOX_API_KEY is required to run the Box Harbor adapter e2e self-test.")

    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        environment_dir = tmp / "environment"
        environment_dir.mkdir()
        (environment_dir / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /workspace\n", encoding="utf-8")
        (environment_dir / "seed.txt").write_text("seed from environment dir\n", encoding="utf-8")
        trial_paths = TrialPaths(tmp / "trial")
        trial_paths.mkdir()
        env = BoxEnvironment(
            environment_dir=environment_dir,
            environment_name="harbor-box-e2e",
            session_id=f"selftest-{os.getpid()}",
            trial_paths=trial_paths,
            task_env_config=EnvironmentConfig(workdir="/workspace", env={"HARBOR_BOX_PERSISTENT": "persisted"}),
            ttl_seconds=300,
        )
        await env.start(force_build=False)
        try:
            result = await env.exec(
                "printf '%s:%s' \"$HARBOR_BOX_PERSISTENT\" \"$HARBOR_BOX_LOCAL\" > /tmp/harbor-box.txt && cat /tmp/harbor-box.txt",
                env={"HARBOR_BOX_LOCAL": "local"},
                timeout_sec=30,
            )
            assert result.return_code == 0, result.stderr
            assert result.stdout == "persisted:local", result.stdout

            source = tmp / "upload.bin"
            source.write_bytes(bytes([0, 1, 2, 253, 254, 255]))
            await env.upload_file(source, "/tmp/harbor-upload.bin")
            assert await env.is_file("/tmp/harbor-upload.bin")

            downloaded = tmp / "download.bin"
            await env.download_file("/tmp/harbor-upload.bin", downloaded)
            assert downloaded.read_bytes() == source.read_bytes()

            src_dir = tmp / "srcdir"
            (src_dir / "nested").mkdir(parents=True)
            (src_dir / "nested" / "hello.txt").write_text("hello box harbor\n", encoding="utf-8")
            await env.upload_dir(src_dir, "/tmp/harbor-dir")
            out_dir = tmp / "outdir"
            await env.download_dir("/tmp/harbor-dir", out_dir)
            assert (out_dir / "nested" / "hello.txt").read_text(encoding="utf-8") == "hello box harbor\n"
        finally:
            await env.stop(delete=True)
    print("Box Harbor adapter e2e self-test PASSED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run_e2e())
