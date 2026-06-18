import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# harbor_box_environment.py lives one level up in sdks/; box_claude_agent.py is here.
for path in (_HERE.parent, _HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Load a local (gitignored) .env so BOX_API_KEY / ANTHROPIC_API_KEY can live in a
# file instead of the shell. Best-effort: python-dotenv is a dev dependency and a
# missing .env is fine. Searches from this dir up to the repo root.
try:
    from dotenv import load_dotenv

    for _dir in (_HERE, *_HERE.parents):
        _env = _dir / ".env"
        if _env.is_file():
            load_dotenv(_env, override=False)
            break
except ImportError:
    pass
