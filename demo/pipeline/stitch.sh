#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/clips.json"
OUTPUT_DIR="$SCRIPT_DIR/output"
TEMP_DIR="$SCRIPT_DIR/.tmp"
LAYOUT="${1:-wide}"

if [ "$LAYOUT" != "wide" ] && [ "$LAYOUT" != "twitter" ] && [ "$LAYOUT" != "all" ]; then
  echo "Unsupported layout: $LAYOUT"
  exit 1
fi
LAYOUT="wide"

json_get() {
  python3 - "$MANIFEST" "$1" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
expr = sys.argv[2]
print(eval(expr, {}, {"m": m}))
PY
}

OUT_NAME="$(json_get "m['output_name']")"
BG="$(json_get "m['palette']['bg']")"
SURFACE="$(json_get "m['palette']['surface']")"
ACCENT="$(json_get "m['palette']['accent']")"
FPS="$(json_get "m['format']['fps']")"
CRF="$(json_get "m['format']['crf']")"
TITLE_HOLD="$(json_get "m['format'].get('title_hold_s', 1.2)")"
END_HOLD="$(json_get "m['format'].get('end_hold_s', 2.4)")"
TRANSITION_HOLD="$(json_get "m['format'].get('transition_card_s', 1.2)")"
TRANSITION_MS="$(json_get "m['format'].get('transition_ms', 220)")"
TRANSITION_S="$(python3 - <<PY
print(round(float("$TRANSITION_MS") / 1000, 3))
PY
)"
NUM_CLIPS="$(json_get "len(m['clips'])")"
PREVIEW_S="$(json_get "m.get('preview_s', 4.0)")"

CW=1920
CH=1080
VIEW_W=1600
VIEW_H=860
VIEW_X=160
VIEW_Y=140
BOX_PAD=18
BOX_X=$((VIEW_X - BOX_PAD))
BOX_Y=$((VIEW_Y - BOX_PAD))
BOX_W=$((VIEW_W + BOX_PAD * 2))
BOX_H=$((VIEW_H + BOX_PAD * 2))
SHADOW_X=$((BOX_X + 16))
SHADOW_Y=$((BOX_Y + 20))

mkdir -p "$OUTPUT_DIR"
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"

echo "Generating subtitles"
python3 "$SCRIPT_DIR/generate-subtitles.py"

find_clip() {
  local rel="$1"
  local p="$SCRIPT_DIR/$rel"
  if [ -f "$p" ]; then
    echo "$p"
    return
  fi
  local alt="${p%.mov}.mp4"
  if [ -f "$alt" ]; then
    echo "$alt"
  fi
}

missing=0
for i in $(seq 0 $((NUM_CLIPS - 1))); do
  rel="$(json_get "m['clips'][$i]['file']")"
  if [ -z "$(find_clip "$rel")" ]; then
    echo "Missing clip: $rel"
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "Record clips first: bash demo/pipeline/record-clips.sh"
  exit 1
fi

if [ ! -f "$SCRIPT_DIR/overlays/title-wide.png" ]; then
  python3 "$SCRIPT_DIR/generate-overlays.py"
fi

seg_index=0
add_segment() {
  local src="$1"
  local tag="$2"
  local out="$TEMP_DIR/seg-$(printf '%03d' "$seg_index")-${tag}.mp4"
  cp "$src" "$out"
  seg_index=$((seg_index + 1))
}

if python3 - <<PY
import sys
sys.exit(0 if float("$TITLE_HOLD") > 0.01 else 1)
PY
then
  ffmpeg -y -loglevel warning \
    -loop 1 -t "$TITLE_HOLD" -i "$SCRIPT_DIR/overlays/title-wide.png" \
    -vf "scale=${CW}:${CH},fps=${FPS},format=yuv420p" \
    -c:v libx264 -crf "$CRF" -preset medium \
    "$TEMP_DIR/title.mp4"
  add_segment "$TEMP_DIR/title.mp4" title
fi

for i in $(seq 0 $((NUM_CLIPS - 1))); do
  id="$(json_get "m['clips'][$i]['id']")"
  rel="$(json_get "m['clips'][$i]['file']")"
  hold="$(json_get "m['clips'][$i]['hold_s']")"
  start="$(json_get "m['clips'][$i].get('start_s', 0)")"
  speed="$(json_get "m['clips'][$i].get('speed', 1.0)")"
  clip_path="$(find_clip "$rel")"

  echo "Processing $id"

  ffmpeg -y -loglevel warning \
    -loop 1 -t "$TRANSITION_HOLD" -i "$SCRIPT_DIR/overlays/transition-wide-${id}.png" \
    -vf "scale=${CW}:${CH},fps=${FPS},format=yuv420p" \
    -c:v libx264 -crf "$CRF" -preset medium \
    "$TEMP_DIR/transition-${id}.mp4"
  add_segment "$TEMP_DIR/transition-${id}.mp4" "transition-${id}"

  duration="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$clip_path")"
  available="$(python3 - <<PY
print(max(0.0, float("$duration") - float("$start")))
PY
)"
  adjusted="$(python3 - <<PY
print(float("$available") / float("$speed"))
PY
)"
  pad="$(python3 - <<PY
print(max(0.0, float("$hold") - float("$adjusted")))
PY
)"
  speed_filter=""
  if [ "$speed" != "1" ] && [ "$speed" != "1.0" ]; then
    speed_filter="setpts=PTS/${speed},"
  fi
  pad_filter=""
  if python3 - <<PY
import sys
sys.exit(0 if float("$pad") > 0.001 else 1)
PY
  then
    pad_filter="tpad=stop_mode=clone:stop_duration=${pad},"
  fi

  ffmpeg -y -loglevel warning \
    -ss "$start" -i "$clip_path" \
    -loop 1 -t "$hold" -i "$SCRIPT_DIR/overlays/label-wide.png" \
    -filter_complex "\
color=c=${BG}:s=${CW}x${CH}:d=${hold}:r=${FPS}[bg]; \
[0:v]${speed_filter}scale=${VIEW_W}:${VIEW_H}:force_original_aspect_ratio=decrease:flags=lanczos,pad=${VIEW_W}:${VIEW_H}:(ow-iw)/2:(oh-ih)/2:color=${SURFACE},fps=${FPS},${pad_filter}setsar=1[screen]; \
[bg]drawbox=x=${SHADOW_X}:y=${SHADOW_Y}:w=${BOX_W}:h=${BOX_H}:color=black@0.30:t=fill[shadow]; \
[shadow]drawbox=x=${BOX_X}:y=${BOX_Y}:w=${BOX_W}:h=${BOX_H}:color=${SURFACE}@0.88:t=fill[stage]; \
[stage]drawbox=x=${BOX_X}:y=${BOX_Y}:w=${BOX_W}:h=3:color=${ACCENT}@0.80:t=fill[stageLine]; \
[stageLine][screen]overlay=${VIEW_X}:${VIEW_Y}:shortest=1[framed]; \
[framed][1:v]overlay=${BOX_X}:62:shortest=1[out]" \
    -map "[out]" -an -t "$hold" \
    -c:v libx264 -crf "$CRF" -preset medium -pix_fmt yuv420p \
    "$TEMP_DIR/clip-${id}.mp4"
  add_segment "$TEMP_DIR/clip-${id}.mp4" "clip-${id}"
done

ffmpeg -y -loglevel warning \
  -loop 1 -t "$END_HOLD" -i "$SCRIPT_DIR/overlays/end-wide.png" \
  -vf "scale=${CW}:${CH},fps=${FPS},format=yuv420p" \
  -c:v libx264 -crf "$CRF" -preset medium \
  "$TEMP_DIR/end.mp4"
add_segment "$TEMP_DIR/end.mp4" end

segments=("$TEMP_DIR"/seg-*.mp4)
silent="$TEMP_DIR/video-silent.mp4"

if python3 - <<PY
import sys
sys.exit(0 if float("$TRANSITION_S") > 0 and ${#segments[@]} > 1 else 1)
PY
then
  inputs=()
  filter=""
  for idx in "${!segments[@]}"; do
    inputs+=(-i "${segments[$idx]}")
    filter="${filter}[${idx}:v]settb=AVTB[v${idx}]; "
  done
  cumulative="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "${segments[0]}")"
  last="v0"
  for ((idx = 1; idx < ${#segments[@]}; idx++)); do
    offset="$(python3 - <<PY
print(round(float("$cumulative") - float("$TRANSITION_S"), 3))
PY
)"
    out="x${idx}"
    filter="${filter}[${last}][v${idx}]xfade=transition=fade:duration=${TRANSITION_S}:offset=${offset}[${out}]; "
    dur="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "${segments[$idx]}")"
    cumulative="$(python3 - <<PY
print(round(float("$cumulative") + float("$dur") - float("$TRANSITION_S"), 3))
PY
)"
    last="$out"
  done
  ffmpeg -y -loglevel warning "${inputs[@]}" \
    -filter_complex "$filter" \
    -map "[${last}]" -an \
    -c:v libx264 -crf "$CRF" -preset medium -pix_fmt yuv420p -movflags +faststart \
    "$silent"
else
  list="$TEMP_DIR/concat.txt"
  : > "$list"
  for seg in "${segments[@]}"; do
    echo "file '$seg'" >> "$list"
  done
  ffmpeg -y -loglevel warning -f concat -safe 0 -i "$list" \
    -c:v libx264 -crf "$CRF" -preset medium -pix_fmt yuv420p -movflags +faststart \
    "$silent"
fi

ffmpeg -y -loglevel warning \
  -i "$silent" -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 \
  -shortest -c:v copy -c:a aac -b:a 96k -movflags +faststart \
  "$OUTPUT_DIR/${OUT_NAME}.mp4"

ffmpeg -y -loglevel warning \
  -ss "$PREVIEW_S" -i "$OUTPUT_DIR/${OUT_NAME}.mp4" \
  -frames:v 1 -update 1 -q:v 2 "$OUTPUT_DIR/preview.png"

rm -rf "$TEMP_DIR"

echo ""
echo "Output: $OUTPUT_DIR/${OUT_NAME}.mp4"
echo "Preview: $OUTPUT_DIR/preview.png"
