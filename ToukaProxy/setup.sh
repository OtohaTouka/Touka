#!/bin/bash
# Developer dependencies only. Product installation: ../README_QUICKSTART.md.
set -eu

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd -P)"
for name in uv.lock pyproject.toml .python-version; do
    if [ ! -f "$REPO_ROOT/$name" ]; then
        printf 'Incomplete workspace: %s is required.\n' "$name" >&2
        exit 1
    fi
done
EXPECTED_VERSION="$(tr -d '\r\n' < "$REPO_ROOT/.python-version")"
if [[ ! "$EXPECTED_VERSION" =~ ^[0-9]+\.[0-9]+$ ]]; then
    printf 'Invalid workspace Python version.\n' >&2
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    printf 'uv is required; see CONTRIBUTING.md.\n' >&2
    exit 1
fi
cd -- "$REPO_ROOT"
uv sync --locked --all-packages || exit "$?"
PYTHON="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    printf 'Workspace Python is missing after uv sync.\n' >&2
    exit 1
fi
ACTUAL_VERSION="$("$PYTHON" -I -S -B -c 'import sys; print(str(sys.version_info.major) + "." + str(sys.version_info.minor))')"
if [ "$ACTUAL_VERSION" != "$EXPECTED_VERSION" ]; then
    printf 'Workspace Python must match .python-version (%s).\n' "$EXPECTED_VERSION" >&2
    exit 1
fi
printf 'Developer dependencies are ready. No data, configuration or services were initialized.\n'
printf 'For product installation, follow README_QUICKSTART.md at the repository root.\n'
