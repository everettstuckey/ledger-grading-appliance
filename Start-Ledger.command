#!/bin/bash
set -euo pipefail
umask 077

if [[ $# -gt 1 ]]; then
    printf 'Usage: %s [--install-only]\n' "$0" >&2
    exit 2
fi
case "${1:-}" in
    --help|-h) printf 'Usage: %s [--install-only]\n' "$0"; exit 0 ;;
    ''|--install-only) ;;
    *) printf 'Usage: %s [--install-only]\n' "$0" >&2; exit 2 ;;
esac

if [[ "$(uname -s)" != Darwin ]]; then
    printf 'This launcher requires macOS.\n' >&2
    exit 1
fi

root=$(cd "$(dirname "$0")" && pwd -P)
state="$HOME/Library/Application Support/Ledger"
venv="$state/venv"
python="$venv/bin/python"

if [[ -n "${LEDGER_PYTHON:-}" ]]; then
    bootstrap="$LEDGER_PYTHON"
elif [[ -x "$python" ]]; then
    bootstrap="$python"
else
    bootstrap=''
    for candidate in "$state"/python/cpython-3.12-macos-*/bin/python3.12 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
        "$(command -v python3.12 || true)"; do
        if [[ -x "$candidate" ]]; then
            bootstrap="$candidate"
            break
        fi
    done
fi

if [[ ! -x "$bootstrap" ]] || ! "$bootstrap" -c \
    'import _tkinter, sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
    printf 'Install Python 3.12 with Tk from python.org, or set LEDGER_PYTHON to its executable.\n' >&2
    exit 1
fi

mkdir -p "$state"
"$bootstrap" - "$venv" "$root/requirements.txt" "${1:-}" <<'PY'
import fcntl
import hashlib
from pathlib import Path
import subprocess
import sys

venv = Path(sys.argv[1])
requirements = Path(sys.argv[2])
install_only = sys.argv[3] == '--install-only'
python = venv / 'bin' / 'python'
manifest = venv / 'ledger-requirements.sha256'
digest = hashlib.sha256(requirements.read_bytes()).hexdigest()

with (venv.parent / 'install.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if not python.is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
    if install_only or not manifest.is_file() or manifest.read_text().strip() != digest:
        manifest.unlink(missing_ok=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '--requirement', str(requirements)], check=True)
        subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
        manifest.write_text(digest + '\n', encoding='ascii')
PY

if [[ "${1:-}" != --install-only ]]; then
    exec "$python" "$root/engine/ledger-main-minimus.py"
fi
