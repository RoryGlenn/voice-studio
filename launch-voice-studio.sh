#!/bin/sh
set -eu
VOICE_STUDIO_REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VOICE_BOOTSTRAP=${VOICE_STUDIO_PYTHON:-"$VOICE_STUDIO_REPO/.venv/bin/python"}
if [ ! -x "$VOICE_BOOTSTRAP" ]; then
  echo 'Voice Studio runtime is missing. Run uv sync --locked --extra ubuntu first.' >&2
  exit 1
fi
exec "$VOICE_BOOTSTRAP" "$VOICE_STUDIO_REPO/run.py" studio "$@"
