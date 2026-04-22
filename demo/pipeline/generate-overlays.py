#!/usr/bin/env python3
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "overlays"
OUT.mkdir(parents=True, exist_ok=True)
MANIFEST = json.loads((SCRIPT_DIR / "clips.json").read_text())
PAL = MANIFEST["palette"]

LAYOUTS = {"wide": (1920, 1080)}

FONT_REGULAR = [
    str(Path.home() / "Library/Fonts/Inter-Regular.otf"),
    str(Path.home() / "Library/Fonts/Inter-Regular.ttf"),
    str(Path.home() / "Library/Fonts/InterVariable.ttf"),
    "/Library/Fonts/Inter-Regular.otf",
    "/System/Library/Fonts/Supplemental/Helvetica Neue.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
FONT_BOLD = [
    str(Path.home() / "Library/Fonts/Inter-Bold.otf"),
    str(Path.home() / "Library/Fonts/Inter-Bold.ttf"),
    str(Path.home() / "Library/Fonts/InterVariable.ttf"),
    "/Library/Fonts/Inter-Bold.otf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica Neue.ttc",
]
FONT_MONO = [
    str(Path.home() / "Library/Fonts/JetBrainsMono-Regular.ttf"),
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
]


def pick(paths):
    for path in paths:
        if Path(path).exists():
            return path
    return paths[-1]


REG = pick(FONT_REGULAR)
BOLD = pick(FONT_BOLD)
MONO = pick(FONT_MONO)


def font(path, size):
    return ImageFont.truetype(path, size=size)


def rgba(hex_color, alpha=255):
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4)) + (alpha,)


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def wrap_text(draw, text, face, max_width):
    words = text.split()
    lines = []
    line = ""
    for word in words:
        test = word if not line else f"{line} {word}"
        if draw.textbbox((0, 0), test, font=face)[2] <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def draw_center(draw, center_x, top_y, text, face, fill, max_width, spacing=10):
    lines = []
    for part in text.split("\n"):
        lines.extend(wrap_text(draw, part, face, max_width))
    y = top_y
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=face)
        x = center_x - (bbox[2] - bbox[0]) / 2
        draw.text((x, y), line, font=face, fill=fill)
        y += (bbox[3] - bbox[1]) + spacing


def base_image(w, h):
    img = Image.new("RGBA", (w, h), rgba(PAL["bg"]))
    accent = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(accent)
    d.polygon([(0, 0), (w * 0.42, 0), (w * 0.20, h), (0, h)], fill=rgba(PAL["accent"], 24))
    d.polygon([(w, 0), (w, h), (w * 0.82, h), (w * 0.96, 0)], fill=rgba(PAL["accent_2"], 22))
    accent = accent.filter(ImageFilter.GaussianBlur(radius=22))
    img = Image.alpha_composite(img, accent)
    d = ImageDraw.Draw(img)
    for x in range(0, w, 120):
        d.line([(x, 0), (x - 280, h)], fill=rgba(PAL["grid"], 26), width=1)
    d.rectangle((0, 0, w, 5), fill=rgba(PAL["accent"]))
    return img


def draw_badge(draw, text, cx, y):
    face = font(BOLD, 28)
    bbox = draw.textbbox((0, 0), text, font=face)
    bw = bbox[2] - bbox[0] + 54
    bh = bbox[3] - bbox[1] + 28
    x = cx - bw / 2
    rounded(draw, (x, y, x + bw, y + bh), 8, rgba(PAL["surface"], 230), rgba(PAL["accent"], 140), 2)
    draw.text((x + 27, y + 14), text, font=face, fill=rgba(PAL["accent_text"]))


def title_card(name, size):
    w, h = size
    img = base_image(w, h)
    draw = ImageDraw.Draw(img)
    draw_badge(draw, MANIFEST["label"], w / 2, int(h * 0.24))
    draw_center(draw, w / 2, int(h * 0.39), MANIFEST["title"], font(BOLD, 62), rgba(PAL["text"]), w * 0.74, 12)
    draw_center(draw, w / 2, int(h * 0.56), MANIFEST["subtitle"], font(REG, 28), rgba(PAL["muted"]), w * 0.72, 8)
    img.save(OUT / f"title-{name}.png")


def transition_card(clip, index, name, size):
    w, h = size
    img = base_image(w, h)
    draw = ImageDraw.Draw(img)
    draw_badge(draw, f"{index + 1} / {len(MANIFEST['clips'])}", w / 2, int(h * 0.22))
    draw_center(draw, w / 2, int(h * 0.40), clip["caption_title"], font(BOLD, 58), rgba(PAL["text"]), w * 0.72, 10)
    draw_center(draw, w / 2, int(h * 0.56), clip["caption_body"], font(REG, 28), rgba(PAL["muted"]), w * 0.72, 8)
    img.save(OUT / f"transition-{name}-{clip['id']}.png")


def label(name):
    text = MANIFEST["label"]
    face = font(BOLD, 23)
    tmp = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(tmp)
    bbox = draw.textbbox((0, 0), text, font=face)
    w = bbox[2] - bbox[0] + 56
    h = 50
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rounded(draw, (0, 0, w - 1, h - 1), 8, rgba(PAL["surface"], 225), rgba(PAL["accent"], 120), 2)
    draw.text((28, 13), text, font=face, fill=rgba(PAL["accent_text"]))
    img.save(OUT / f"label-{name}.png")


def end_card(name, size):
    w, h = size
    img = base_image(w, h)
    draw = ImageDraw.Draw(img)
    draw_center(draw, w / 2, int(h * 0.32), MANIFEST["end_title"], font(BOLD, 56), rgba(PAL["text"]), w * 0.72, 10)
    draw_center(draw, w / 2, int(h * 0.47), MANIFEST["end_body"], font(REG, 28), rgba(PAL["muted"]), w * 0.72, 8)
    cta = MANIFEST.get("cta", "")
    if cta:
        face = font(MONO, 27)
        bbox = draw.textbbox((0, 0), cta, font=face)
        cw = min(w * 0.82, bbox[2] - bbox[0] + 70)
        ch = bbox[3] - bbox[1] + 42
        x = (w - cw) / 2
        y = int(h * 0.62)
        rounded(draw, (x, y, x + cw, y + ch), 8, rgba(PAL["surface"], 235), rgba(PAL["accent"], 130), 2)
        if bbox[2] - bbox[0] > cw - 70:
            cta = cta[:72] + "..."
        draw.text((x + 35, y + 21), cta, font=face, fill=rgba(PAL["accent_text"]))
    img.save(OUT / f"end-{name}.png")


for layout_name, size in LAYOUTS.items():
    title_card(layout_name, size)
    end_card(layout_name, size)
    label(layout_name)
    for i, clip in enumerate(MANIFEST["clips"]):
        transition_card(clip, i, layout_name, size)

print(f"Wrote overlays to {OUT}")

