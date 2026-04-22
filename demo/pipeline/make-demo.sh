#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAYOUT="${1:-wide}"
PROJECT_NAME="$(python3 - "$SCRIPT_DIR/clips.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["project"])
PY
)"

echo ""
echo "== ${PROJECT_NAME} demo pipeline =="
echo ""

echo "Step 1: generating overlays"
python3 "$SCRIPT_DIR/generate-overlays.py"

echo ""
echo "Step 2: stitching demo"
bash "$SCRIPT_DIR/stitch.sh" "$LAYOUT"

echo ""
echo "Done. Outputs are in demo/pipeline/output/"
