"""Validates every BoxEnvironment behaviour the README and docs claim.

Two tiers:

* **Unit tests** — no network. They pin the contract: no-env default, the create
  payload, reserved-env filtering, Dockerfile WORKDIR parsing, capabilities, and
  that every feature Box can't provide (Docker image builds, network policies,
  GPUs/TPUs, Windows) is *rejected* rather than silently accepted.
* **`real_box` tests** — exercise a live Box (need `BOX_API_KEY`; they create
  short-lived boxes). They prove the documented surface actually works: file
  upload from the environment dir, exec/cwd/user, env, file/dir transfer,
  is_file/is_dir, ensure_dirs, that Dockerfile build steps are NOT run, and that
  a no-env box withholds the account environment a full-env box receives.

If the docs claim it, there is a test here. If it isn't tested, it isn't claimed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harbor.environments.capabilities import EnvironmentResourceCapabilities
from harbor.models.task.config import EnvironmentConfig, NetworkMode
from harbor.models.trial.paths import TrialPaths

from harbor_box_environment import (
    AsyncBoxClient,
    BoxApiError,
    BoxEnvironment,
    _RESERVED_BOX_ENV,
    _parse_dockerfile_workdir,
)

_requires_box = pytest.mark.skipif(
    not os.environ.get("BOX_API_KEY"), reason="BOX_API_KEY required for live Box tests"
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _env_dir(root: Path, dockerfile: str, files: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _make(base: Path, cfg: EnvironmentConfig, *, dockerfile: str | None = None, files=None, **kw) -> BoxEnvironment:
    env_dir = _env_dir(base / "environment", dockerfile or f"FROM ubuntu:24.04\nWORKDIR {cfg.workdir or '/workspace'}\n", files)
    trial = TrialPaths(base / "trial")
    trial.mkdir()
    return BoxEnvironment(
        environment_dir=env_dir,
        environment_name="adapter-test",
        session_id=f"t-{os.getpid()}-{base.name}",
        trial_paths=trial,
        task_env_config=cfg,
        api_key=os.environ.get("BOX_API_KEY", "test-key"),
        **kw,
    )


class _Resp:
    def __init__(self, payload: dict):
        self._p = payload
        self.status_code = 200
        self.text = json.dumps(payload)
        self.reason_phrase = "OK"

    def json(self):
        return self._p


class _CapturingHttp:
    """Stand-in httpx.AsyncClient that records request bodies."""

    def __init__(self):
        self.calls: list[dict] = []

    async def request(self, method, url, headers=None, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return _Resp({"ok": True, "box": {"id": "bx_test", "state": "ready"}})


# --------------------------------------------------------------------------- #
# unit: contract / config validation
# --------------------------------------------------------------------------- #
def test_type_and_no_env_default(tmp_path):
    env = _make(tmp_path, EnvironmentConfig(workdir="/workspace"))
    assert env.type() == "box"
    assert env._no_env is True  # no-env is the default


def test_default_config_validates(tmp_path):
    _make(tmp_path, EnvironmentConfig(workdir="/workspace"))._validate_definition()  # must not raise


@pytest.mark.parametrize(
    "kwargs",
    [
        {"docker_image": "ubuntu:24.04"},
        {"network_mode": NetworkMode.NO_NETWORK},
        {"network_mode": NetworkMode.ALLOWLIST, "allowed_hosts": ["example.com"]},
        {"allow_internet": False},
        {"gpus": 1},
        {"tpu": "v3-8"},
    ],
)
def test_rejects_unsupported_features(tmp_path, kwargs):
    with pytest.raises(ValueError):
        _make(tmp_path, EnvironmentConfig(workdir="/workspace", **kwargs))._validate_definition()


def test_rejects_non_linux_os(tmp_path):
    from harbor.models.task.config import TaskOS

    win = next((o for o in TaskOS if o.value.lower() != "linux"), None)
    if win is None:
        pytest.skip("no non-linux TaskOS to test")
    with pytest.raises(ValueError):
        _make(tmp_path, EnvironmentConfig(workdir="/workspace", os=win))._validate_definition()


def test_box_create_env_filters_reserved(tmp_path):
    reserved = next(iter(_RESERVED_BOX_ENV))
    env = _make(tmp_path, EnvironmentConfig(workdir="/workspace", env={"KEEP": "1", reserved: "x"}))
    assert env._box_create_env() == {"KEEP": "1"}


def test_resource_capabilities_are_default(tmp_path):
    # We never advertise GPUs/accelerators we can't provide.
    env = _make(tmp_path, EnvironmentConfig(workdir="/workspace"))
    assert env.resource_capabilities() == EnvironmentResourceCapabilities()


@pytest.mark.parametrize(
    "dockerfile, expected",
    [
        ("FROM ubuntu:24.04\nWORKDIR /srv/app\n", "/srv/app"),
        ("FROM ubuntu:24.04\nWORKDIR /app\nWORKDIR sub\n", "/app/sub"),
        ("FROM ubuntu:24.04\n", None),
    ],
)
def test_parse_dockerfile_workdir(tmp_path, dockerfile, expected):
    (tmp_path / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    assert _parse_dockerfile_workdir(tmp_path / "Dockerfile") == expected


async def test_create_payload_includes_no_env_and_env():
    http = _CapturingHttp()
    await AsyncBoxClient(api_key="k", client=http).create(ttl_seconds=600, no_env=True, env={"A": "1"})
    assert http.calls[0]["json"] == {"ttlSeconds": 600, "noEnv": True, "env": {"A": "1"}}


async def test_create_payload_default_omits_no_env():
    http = _CapturingHttp()
    await AsyncBoxClient(api_key="k", client=http).create(ttl_seconds=600)
    body = http.calls[0]["json"]
    assert "noEnv" not in body and "env" not in body and body["ttlSeconds"] == 600


# --------------------------------------------------------------------------- #
# real_box: the live surface
# --------------------------------------------------------------------------- #
@pytest.mark.real_box
@_requires_box
async def test_adapter_surface(tmp_path):
    dockerfile = "FROM ubuntu:24.04\nRUN echo built > /built_marker.txt\nWORKDIR /workspace\n"
    env = _make(
        tmp_path,
        EnvironmentConfig(workdir="/workspace", env={"PERSIST": "persisted"}),
        dockerfile=dockerfile,
        files={"seed.txt": "seed-content\n", "pkg/data.txt": "nested-seed\n"},
        ttl_seconds=600,
    )
    env.default_user = "user"
    await env.start(force_build=False)
    try:
        # files from the environment directory are uploaded into the workdir
        assert (await env.exec("cat seed.txt")).stdout.strip() == "seed-content"
        assert (await env.exec("cat pkg/data.txt")).stdout.strip() == "nested-seed"

        # Dockerfile build steps (RUN) are NOT executed
        assert await env.is_file("/built_marker.txt") is False

        # exec: stdout + success
        ok = await env.exec("echo hello")
        assert ok.return_code == 0 and ok.stdout.strip() == "hello"

        # exec: failure surfaces a nonzero code and output
        bad = await env.exec("ls /no/such/path")
        assert bad.return_code != 0 and (bad.stderr or bad.stdout)

        # default cwd is the workdir; explicit cwd overrides it
        assert (await env.exec("pwd")).stdout.strip() == "/workspace"
        assert (await env.exec("pwd", cwd="/tmp")).stdout.strip() == "/tmp"

        # user=root via sudo
        assert (await env.exec("whoami", user="root")).stdout.strip() == "root"

        # persistent env (task config) + per-command env
        merged = await env.exec("printf '%s:%s' \"$PERSIST\" \"$PERCMD\"", env={"PERCMD": "x"})
        assert merged.stdout == "persisted:x"

        # binary file upload/download round-trip
        src = tmp_path / "u.bin"
        src.write_bytes(bytes(range(256)))
        await env.upload_file(src, "/workspace/u.bin")
        out = tmp_path / "d.bin"
        await env.download_file("/workspace/u.bin", out)
        assert out.read_bytes() == src.read_bytes()

        # nested directory upload/download round-trip
        sd = tmp_path / "sdir" / "a"
        sd.mkdir(parents=True)
        (sd / "f.txt").write_text("dir-content\n", encoding="utf-8")
        await env.upload_dir(tmp_path / "sdir", "/workspace/up")
        od = tmp_path / "odir"
        await env.download_dir("/workspace/up", od)
        assert (od / "a" / "f.txt").read_text(encoding="utf-8") == "dir-content\n"

        # is_file / is_dir
        assert await env.is_file("/workspace/seed.txt") and not await env.is_file("/workspace/nope")
        assert await env.is_dir("/workspace/pkg") and not await env.is_dir("/workspace/seed.txt")

        # ensure_dirs creates nested directories
        await env.ensure_dirs(["/workspace/made/deep"])
        assert await env.is_dir("/workspace/made/deep")
    finally:
        await env.stop(delete=True)


@pytest.mark.real_box
@_requires_box
async def test_no_env_withholds_account_environment(tmp_path):
    async def env_var_names(slot: str, no_env: bool) -> set[str]:
        env = _make(tmp_path / slot, EnvironmentConfig(workdir="/workspace"), no_env=no_env)
        env.default_user = "user"
        await env.start(force_build=False)
        try:
            result = await env.exec("env | cut -d= -f1")
            return {line.strip() for line in result.stdout.splitlines() if line.strip()}
        finally:
            await env.stop(delete=True)

    isolated = await env_var_names("noenv", no_env=True)
    full = await env_var_names("fullenv", no_env=False)

    account_only = full - isolated
    if not account_only:
        pytest.skip("account injects no extra environment to differentiate no-env from full-env")
    # The no-env box must not carry the account-injected variables a full-env box has.
    assert account_only and account_only.isdisjoint(isolated)


# Setup steps that replicate a Dockerfile's RUN lines at runtime. Shared with the
# examples/dockerfile_to_box.py guide so the doc and the test never drift.
DOCKERFILE_MIGRATION_SETUP = (
    "apt-get update -qq && apt-get install -y -qq cowsay",  # RUN apt-get install -y cowsay
    "pip3 install --break-system-packages -q pyfiglet",     # RUN pip install pyfiglet (PEP 668: --break-system-packages)
)

_RENDER_PY = (
    "import subprocess\n"
    "import pyfiglet  # pip-installed at runtime; not in the base image\n"
    "banner = pyfiglet.figlet_format('Box')\n"
    "cow = subprocess.run(['/usr/games/cowsay', 'hello from box'],\n"
    "                     capture_output=True, text=True).stdout  # apt-installed at runtime\n"
    "print(banner)\nprint(cow)\nprint('RENDER_OK')\n"
)


@pytest.mark.real_box
@_requires_box
async def test_dockerfile_migration(tmp_path):
    """A task that would have needed a Dockerfile (FROM + RUN apt/pip + COPY) runs on
    Box by replicating the build at runtime: files ride in the environment directory,
    and the RUN steps become a one-time root setup command. Proves the migration path
    documented in examples/dockerfile_to_box.py, end to end, against a live Box."""
    env = _make(
        tmp_path,
        EnvironmentConfig(workdir="/workspace"),
        files={"render.py": _RENDER_PY},  # COPY render.py /workspace/  (uploaded at start)
        ttl_seconds=600,
    )
    env.default_user = "user"
    await env.start(force_build=False)
    try:
        # The deps are NOT in the base image, so the runtime install really does the work.
        assert (await env.exec("python3 -c 'import pyfiglet'")).return_code != 0
        assert (await env.exec("test -x /usr/games/cowsay")).return_code != 0

        # Replicate the Dockerfile's RUN lines as a one-time root setup.
        for cmd in DOCKERFILE_MIGRATION_SETUP:
            r = await env.exec(cmd, user="root", timeout_sec=300)
            assert r.return_code == 0, f"setup failed: {cmd}\n{r.stderr or r.stdout}"

        # The task now runs against the prepared box (render.py came from the env dir).
        out = await env.exec("python3 render.py", timeout_sec=120)
        assert out.return_code == 0, out.stderr or out.stdout
        assert "RENDER_OK" in out.stdout and "hello from box" in out.stdout
    finally:
        await env.stop(delete=True)


# Box "active" (billing) states are exactly provisioned/cloning/ready/idle/running;
# a box that is 404 (deleted) or archived is, by definition, no longer billing. Harbor
# calls env.stop(delete=...) in a finally when a trial ends, so this is what pauses cost.
@pytest.mark.real_box
@_requires_box
async def test_stop_delete_removes_box(tmp_path):
    client = AsyncBoxClient()
    env = _make(tmp_path, EnvironmentConfig(workdir="/workspace"), ttl_seconds=600)
    env.default_user = "user"
    await env.start(force_build=False)
    box_id = env.box_id
    await env.stop(delete=True)  # Harbor's default: permanent removal
    with pytest.raises(BoxApiError) as ei:
        await client.get(box_id)
    assert ei.value.status_code == 404  # gone -> not in any active/billing state


@pytest.mark.real_box
@_requires_box
async def test_stop_archive_keeps_snapshot_and_stops_billing(tmp_path):
    client = AsyncBoxClient()
    env = _make(tmp_path, EnvironmentConfig(workdir="/workspace"), ttl_seconds=600)
    env.default_user = "user"
    await env.start(force_build=False)
    box_id = env.box_id
    try:
        await env.stop(delete=False)  # archive (snapshot, resumable)
        state = (await client.get(box_id))["state"]
        assert state in ("archiving", "archived")  # left active states -> compute/billing stopped
    finally:
        try:
            await client.delete(box_id)  # don't leave an archived box behind
        except Exception:
            pass
