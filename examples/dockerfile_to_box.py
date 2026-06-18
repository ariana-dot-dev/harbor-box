#!/usr/bin/env python
"""Run a Dockerfile-based Harbor environment on Box.

Box runs a ready Ubuntu image and does **not** build Docker images, so a
Dockerfile's `FROM` / `RUN` / `COPY` are not executed. You reproduce them at
runtime instead — outbound network works, so apt/pip/npm installs succeed:

    Dockerfile                          ->  On Box (this script)
    ------------------------------------------------------------------------
    COPY render.py /workspace/           ->  put render.py in the environment
                                             directory; it is uploaded at start()
    RUN apt-get install -y <pkg>         ->  env.exec("apt-get install -y <pkg>",
                                             user="root")
    RUN pip install <pkg>                ->  env.exec("pip3 install
                                             --break-system-packages <pkg>",
                                             user="root")   # Ubuntu is PEP 668
    WORKDIR /workspace                   ->  read from the Dockerfile, or set
                                             EnvironmentConfig(workdir=...)

The one thing Box cannot do is enforce *network isolation* (no-network /
allow-lists). Those are rejected by the adapter, not silently faked — run such
tasks on a backend that enforces them.

The Dockerfile this script reproduces:

    FROM python:3.12
    RUN apt-get update && apt-get install -y cowsay
    RUN pip install pyfiglet
    COPY render.py /workspace/
    WORKDIR /workspace

Usage:

    pip install harbor-box
    export BOX_API_KEY=...           # get one at https://box.ascii.dev
    python examples/dockerfile_to_box.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Running from a clone: make the adapter importable. Not needed after `pip install harbor-box`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdks"))

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor_box_environment import BoxEnvironment

WORKDIR = "/workspace"

# COPY render.py /workspace/ — any file in the environment directory is uploaded at start().
RENDER_PY = """\
import subprocess
import pyfiglet                       # from `pip install pyfiglet`
print(pyfiglet.figlet_format("Box"))
cow = subprocess.run(["/usr/games/cowsay", "hello from box"],
                     capture_output=True, text=True).stdout   # from `apt-get install cowsay`
print(cow)
"""

# The Dockerfile's RUN lines, reproduced as a one-time root setup. Ubuntu's system
# Python is PEP 668 "externally managed", so pip needs --break-system-packages.
SETUP_COMMANDS = [
    "apt-get update -qq && apt-get install -y -qq cowsay",   # RUN apt-get install -y cowsay
    "pip3 install --break-system-packages -q pyfiglet",      # RUN pip install pyfiglet
]


async def main() -> int:
    if not os.environ.get("BOX_API_KEY"):
        print("Set BOX_API_KEY first (get one at https://box.ascii.dev).")
        return 2

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        env_dir = tmp / "environment"
        env_dir.mkdir()
        # Only WORKDIR is read from the Dockerfile; FROM/RUN/COPY are not built.
        (env_dir / "Dockerfile").write_text(f"FROM ubuntu:24.04\nWORKDIR {WORKDIR}\n")
        (env_dir / "render.py").write_text(RENDER_PY)  # COPY render.py /workspace/
        trial_paths = TrialPaths(tmp / "trial")
        trial_paths.mkdir()

        env = BoxEnvironment(
            environment_dir=env_dir,
            environment_name="dockerfile-migration",
            session_id="example",
            trial_paths=trial_paths,
            task_env_config=EnvironmentConfig(workdir=WORKDIR),
            ttl_seconds=600,
        )
        env.default_user = "user"

        await env.start(force_build=False)  # provisions a box and uploads the environment dir
        try:
            print("running setup (the Dockerfile's RUN steps, at runtime)...")
            for cmd in SETUP_COMMANDS:
                result = await env.exec(cmd, user="root", timeout_sec=300)
                if result.return_code != 0:
                    print(f"setup failed: {cmd}\n{result.stderr or result.stdout}")
                    return 1

            run = await env.exec("python3 render.py", timeout_sec=120)
            print(run.stdout)
            ok = run.return_code == 0 and "hello from box" in run.stdout
            print("MIGRATION OK" if ok else "MIGRATION FAILED")
            return 0 if ok else 1
        finally:
            # Harbor would call this for you when a trial ends. delete=True removes the
            # box (the default); pass delete=False to archive a snapshot for resume/fork.
            # Either way the box leaves Box's active set, so compute billing stops.
            await env.stop(delete=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
