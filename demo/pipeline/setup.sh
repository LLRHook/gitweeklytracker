#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/clips.json"

check_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "  OK: $1"
  else
    echo "  MISSING: $1"
    echo "       $2"
    return 1
  fi
}

echo "Demo pipeline setup"
echo ""

missing=0
check_cmd ffmpeg "Install with: brew install ffmpeg" || missing=1
check_cmd ffprobe "Install with: brew install ffmpeg" || missing=1
check_cmd python3 "Install with: brew install python3" || missing=1

if python3 -c "from PIL import Image" >/dev/null 2>&1; then
  echo "  OK: Pillow"
else
  echo "  MISSING: Pillow"
  echo "       Install with: python3 -m pip install Pillow"
  missing=1
fi

if [ -f "$MANIFEST" ]; then
  while IFS= read -r tool; do
    [ -n "$tool" ] || continue
    case "$tool" in
      vhs) check_cmd vhs "Install with: brew install charmbracelet/tap/vhs" || missing=1 ;;
      agent-browser) check_cmd agent-browser "Install agent-browser before running record-clips.sh" || missing=1 ;;
      npm) check_cmd npm "Install Node.js with npm" || missing=1 ;;
      *) check_cmd "$tool" "Install $tool" || missing=1 ;;
    esac
  done < <(python3 - "$MANIFEST" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
for tool in m.get("capture", {}).get("tools", []):
    print(tool)
PY
)
fi

mkdir -p "$SCRIPT_DIR/clips" "$SCRIPT_DIR/overlays" "$SCRIPT_DIR/output" "$SCRIPT_DIR/subtitles"

if [ "$missing" -ne 0 ]; then
  echo ""
  echo "Setup found missing dependencies."
  exit 1
fi

echo ""
echo "Setup complete."

