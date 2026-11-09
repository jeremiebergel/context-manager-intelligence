#!/usr/bin/env bash
# Export HEAD as a fresh single-commit repository (no development history) and verify it.
#   scripts/export_public.sh [dest-dir]        default: ./dist-public
# Nothing is pushed. Checks: hygiene test, full test suite standalone, offline benchmark, proxy e2e,
# wheel build — all run inside the export, from a clean venv.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$ROOT/dist-public}"
cd "$ROOT"
if [ -n "$(git status --porcelain)" ]; then echo "working tree not clean; commit first" >&2; exit 1; fi
rm -rf "$DEST" && mkdir -p "$DEST"
git archive HEAD | tar -x -C "$DEST"
cd "$DEST"
git init -q -b main
git add -A
GIT_AUTHOR_NAME="Mistogan7JB" GIT_AUTHOR_EMAIL="mistogan7jb@users.noreply.github.com" \
GIT_COMMITTER_NAME="Mistogan7JB" GIT_COMMITTER_EMAIL="mistogan7jb@users.noreply.github.com" \
  git commit -q -m "CMI $(python3 -c 'import re;print(re.search(r"__version__ = \"(.+?)\"",open("cmi/__init__.py").read())[1])') — first public release"
# Local interpreters only (no implicit download): uv if present, else python3 -m venv.
if command -v uv >/dev/null 2>&1; then
  uv venv -q --python-preference only-system --python ">=3.10" .venv-export
  . .venv-export/bin/activate
  uv pip install -q -e ".[dev]" build
else
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || { echo "need uv or python3 >= 3.10 (found $(python3 -V))" >&2; exit 1; }
  python3 -m venv .venv-export
  . .venv-export/bin/activate
  pip install -q --upgrade pip && pip install -q -e ".[dev]" build
fi
export CMI_STANDALONE=1 CMI_REQUIRE_PRIVATE_TERMS=1
python -m pytest -q -p no:cacheprovider tests/test_public_hygiene.py
python -m pytest -q -p no:cacheprovider
python bench/public_demo.py
python bench/proxy_e2e.py
python -m build -q --outdir .dist-check . >/dev/null
ls .dist-check/*.whl >/dev/null
deactivate
rm -rf .venv-export .dist-check ./*.egg-info
echo "EXPORT OK: $DEST ($(git rev-list --count HEAD) commit, $(git ls-files | wc -l | tr -d ' ') files)"
