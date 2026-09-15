#!/bin/zsh
set -e
VOICE_STUDIO_REPO="$(cd -- "$(dirname -- "$0")" && pwd)"
VOICE_BOOTSTRAPS=()
if [[ -n "${VOICE_STUDIO_PYTHON:-}" ]]; then
  VOICE_BOOTSTRAPS+=("$VOICE_STUDIO_PYTHON")
fi
VOICE_BOOTSTRAPS+=(
  "$VOICE_STUDIO_REPO/.venv/bin/python"
  /opt/homebrew/bin/python3
  /usr/local/bin/python3
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
  python3
)
for VOICE_BOOTSTRAP in "${VOICE_BOOTSTRAPS[@]}"; do
  if "$VOICE_BOOTSTRAP" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
    exec "$VOICE_BOOTSTRAP" "$VOICE_STUDIO_REPO/run.py" studio "$@"
  fi
done
print -u2 "Voice Studio needs Python 3.12 or newer to launch. Run uv sync first or set VOICE_STUDIO_PYTHON."
exit 1
