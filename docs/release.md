# Releasing `harbor-box`

This package ships on PyPI as **`harbor-box`** (it imports as the module
`harbor_box_environment`). The source lives in the public
[`ariana-dot-dev/harbor-box`](https://github.com/ariana-dot-dev/harbor-box)
repository.

Publish from a maintainer's local computer. Do **not** publish from an agent or
CI unless release automation is added later.

## What CI does (and does not)

GitHub Actions (`.github/workflows/ci.yml`) runs on every push/PR to `main`:
imports the adapter, checks test collection, and runs the **real Box adapter
e2e self-test** using the `BOX_API_KEY` repo secret (it self-skips on fork PRs
where the secret is unavailable). CI **does not** publish to PyPI and does not
run the Claude-agent evals (those cost Anthropic tokens; run them locally).

## First public PyPI release

From a fresh local checkout of `main`:

```bash
# 1. Update the local checkout.
git checkout main
git pull --ff-only origin main

# 2. Fresh environment with build + publish tooling.
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt build twine

# 3. Run the real Box adapter e2e self-test (correctness gate).
# Use a real key from the Ascii Box dashboard. Keep it in a gitignored .env or
# your shell only — never commit it.
export BOX_API_KEY=box_your_real_key_here
python sdks/harbor_box_environment.py
# -> Box Harbor adapter e2e self-test PASSED

# 4. (Optional) Run the Claude-agent evals end to end. Needs an Anthropic
# credential and costs tokens.
# export ANTHROPIC_API_KEY=...        # or: export CLAUDE_CODE_OAUTH_TOKEN=...
# pytest sdks/harbor-box-evals -v -m real_box

# 5. Build the sdist + wheel.
python -m build

# 6. Validate the artifacts (metadata + README render).
twine check dist/*

# 7. Packaging safety check: upload to TestPyPI first (optional but recommended).
# twine upload --repository testpypi dist/*

# 8. Publish to PyPI.
twine upload dist/*

# 9. Confirm PyPI serves the released version.
pip index versions harbor-box   # or: pip install harbor-box==<version>
```

## Cut a GitHub release to match

```bash
gh release create vX.Y.Z --title vX.Y.Z --notes "..."
```

## Notes

- Keep the PyPI name exactly `harbor-box` (lowercase). The import name stays
  `harbor_box_environment` so existing
  `--environment-import-path harbor_box_environment:BoxEnvironment` wiring keeps
  working.
- Bump `version` in `pyproject.toml` before every publish; PyPI rejects
  re-uploading an existing version. Keep it in sync with the GitHub release tag.
- The only correctness gate is the real Box self-test (step 3) and, optionally,
  the evals (step 4). `twine check` and a TestPyPI upload are packaging checks,
  not correctness evidence.
- Never commit a real `BOX_API_KEY` / `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`.
  Keep them in a gitignored `.env` or your shell only.
- Do not publish from CI or an agent. Do not rotate secrets or touch live
  infrastructure as part of a release.
