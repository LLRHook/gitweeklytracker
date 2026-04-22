#!/usr/bin/env python3
import json
import math
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST = json.loads((SCRIPT_DIR / "clips.json").read_text())
OUT = SCRIPT_DIR / "subtitles"
OUT.mkdir(parents=True, exist_ok=True)


def srt_time(seconds):
    ms_total = int(math.floor(max(0, seconds) * 1000 + 0.5))
    ms = ms_total % 1000
    total_s = ms_total // 1000
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


fmt = MANIFEST["format"]
xfade = fmt.get("transition_ms", 0) / 1000
segments = []
if fmt.get("title_hold_s", 0) > 0:
    segments.append(("title", None, fmt["title_hold_s"]))
for clip in MANIFEST["clips"]:
    segments.append(("transition", clip, fmt.get("transition_card_s", 1.2)))
    segments.append(("clip", clip, clip["hold_s"]))
segments.append(("end", None, fmt.get("end_hold_s", 2.4)))

cues = []
t = 0.0
idx = 1
for seg_idx, (kind, clip, duration) in enumerate(segments):
    start = t
    end = t + duration
    if clip and kind in {"transition", "clip"}:
        text = clip["caption_title"] if kind == "transition" else clip["caption_body"]
        cues.append(f"{idx}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
        idx += 1
    t = end - xfade if seg_idx < len(segments) - 1 else end

path = OUT / f"{MANIFEST['output_name']}.srt"
path.write_text("\n".join(cues))
print(f"Wrote {path}")

