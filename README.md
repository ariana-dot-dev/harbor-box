# harbor-box

[![CI](https://github.com/ariana-dot-dev/harbor-box/actions/workflows/ci.yml/badge.svg)](https://github.com/ariana-dot-dev/harbor-box/actions/workflows/ci.yml)

A [Harbor](https://pypi.org/project/harbor/) environment adapter backed by the
**[Ascii Box](https://box.ascii.dev)** public API.

`BoxEnvironment` runs Harbor agent trials and evals inside an Ascii Box instead
of a local Docker container, E2B, or Modal. It implements Harbor's
`BaseEnvironment` surface — lifecycle, command execution, environment variables,
and file/dir upload + download — on top of Box's public HTTP API.

Some Harbor features aren't available on Box today: network policies, Docker
image builds / Compose, and GPUs / TPUs / Windows.

## Layout

| Path | What |
| --- | --- |
| `sdks/harbor_box_environment.py` | `BoxEnvironment` — the Harbor adapter plus a small async Box API client |
| `sdks/harbor-box-evals/` | End-to-end evals: a from-scratch Claude Agent SDK coding agent solving tasks inside a Box |

## Install

```bash
pip install harbor-box                 # imports as: harbor_box_environment
```

Or from a clone of this repo:

```bash
pip install -r requirements.txt        # harbor>=0.14.0, httpx, tenacity
```

## Configure

```bash
cp .env.example .env
# fill in BOX_API_KEY
```

`BOX_API_KEY` is required — get one at <https://box.ascii.dev>. Your `.env` is
gitignored; never commit secrets.

## Use it with Harbor

```bash
export BOX_API_KEY=...
harbor task run path/to/task.yaml \
  --environment-import-path harbor_box_environment:BoxEnvironment
```

## Preparing the environment

Box runs a ready Ubuntu image — it doesn't build Docker images. The adapter
prepares each box from your Harbor environment directory instead:

- **Files** in the environment directory are uploaded into the box's working
  directory at start, so fixtures, configs, and scripts are in place before the
  agent runs.
- The **`Dockerfile`** is read only for its `WORKDIR`; build steps
  (`RUN` / `COPY` / …) are not executed. Install dependencies from a setup
  command (`await env.exec(...)`) or your agent, not the image.
- **Environment variables** come from the Harbor environment config and are
  forwarded into the box; per-command env is passed through `exec`.

```python
task_env_config = EnvironmentConfig(
    workdir="/workspace",
    env={"DATABASE_URL": "postgres://..."},
)
```

## Isolation

Boxes are **no-env by default**: a trial gets none of your Box account's secrets,
credentials, or cloned repos and can't act on your account or other boxes. Pass
`no_env=False` to use your account's environment instead, configured in the
[Box dashboard](https://box.ascii.dev/box/dashboard). See
[Secrets & Setup](https://docs.ascii.dev/box/secrets).

## Verify the adapter

A self-contained end-to-end self-test exercises start → persistent/per-command
env vars → binary upload/download → nested directory upload/download → stop:

```bash
export BOX_API_KEY=...
python sdks/harbor_box_environment.py
# -> Box Harbor adapter e2e self-test PASSED
```

CI runs this self-test on every push.

## Evals

A from-scratch [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-typescript)
coding agent solving tasks inside a Box, scored by inspecting the result. See
[`sdks/harbor-box-evals/README.md`](sdks/harbor-box-evals/README.md).

## Releasing

Maintainers publish `harbor-box` to PyPI from a local checkout — see
[`docs/release.md`](docs/release.md). CI never publishes.

## License

[MIT](LICENSE).
