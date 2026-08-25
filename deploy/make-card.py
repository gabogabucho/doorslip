"""Draw the link-preview card, so it can be redrawn rather than found again.

    uv run --with pillow python deploy/make-card.py

A social card is the one image of a project most people see before anything
else, and the usual fate of one is a PNG somebody exported once from a design
tool nobody still has. This is ninety lines and the same palette as the
landing page, so changing the wordmark or the colour is an edit rather than an
archaeology problem.

1200x630 because that is what the platforms crop against. Rendered at 2x and
resampled, since text drawn straight at this size has visibly hard edges.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "card.png"
W, H = 1200, 630
SCALE = 2

CANVAS = (21, 19, 15)
INK = (236, 230, 219)
INK_2 = (167, 158, 143)
INK_3 = (123, 115, 101)
RULE = (42, 37, 30)
MARK = (217, 164, 65)

# Whatever serif and mono this machine has. Named explicitly per platform
# rather than left to a default, because PIL's default is a bitmap face that
# would make this look like a placeholder.
SERIF = ["C:/Windows/Fonts/georgia.ttf",
         "/System/Library/Fonts/Supplemental/Georgia.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"]
MONO = ["C:/Windows/Fonts/consola.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"]


def font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    sys.exit(f"no usable font among: {', '.join(candidates)}")


def warm_light(size: tuple[int, int]) -> Image.Image:
    """The hero's radial, built small and scaled up.

    Computing it at full size costs a million square roots for a wash nobody
    is meant to notice; at 1/20 the resampling does the smoothing for free.
    """
    w, h = size[0] // 20, size[1] // 20
    small = Image.new("L", (w, h), 0)
    pixels = small.load()
    cx, cy, radius = w * 0.24, h * -0.05, w * 0.62
    for y in range(h):
        for x in range(w):
            d = (((x - cx) ** 2 + (y - cy) ** 2) ** 0.5) / radius
            pixels[x, y] = max(0, int(255 * (1 - d) ** 2)) if d < 1 else 0
    return small.resize(size, Image.LANCZOS)


def draw_card() -> Image.Image:
    size = (W * SCALE, H * SCALE)
    card = Image.new("RGB", size, CANVAS)

    glow = Image.new("RGB", size, MARK)
    card = Image.composite(
        Image.blend(card, glow, 0.07), card, warm_light(size)
    )

    d = ImageDraw.Draw(card)
    s = SCALE

    # The mark: a door, and the slot that names it.
    x, y, w, h = 92 * s, 196 * s, 88 * s, 116 * s
    d.rounded_rectangle([x, y, x + w, y + h], radius=14 * s, outline=INK_3,
                        width=4 * s)
    slot_w, slot_h = 50 * s, 13 * s
    slot_x = x + (w - slot_w) // 2
    d.rounded_rectangle(
        [slot_x, y + h - 34 * s, slot_x + slot_w, y + h - 34 * s + slot_h],
        radius=slot_h // 2, fill=MARK,
    )

    d.text((212 * s, 186 * s), "Doorslip", font=font(SERIF, 116 * s), fill=INK)
    d.text((94 * s, 356 * s), "Signed mailboxes for personal agents.",
           font=font(SERIF, 42 * s), fill=INK_2)

    # The rule and the footer sit near the bottom edge rather than under the
    # tagline. Centring the whole block instead leaves a band of dead canvas
    # below it, which reads as a crop that went wrong.
    d.line([(94 * s, 500 * s), (1106 * s, 500 * s)], fill=RULE, width=2 * s)

    small = font(MONO, 24 * s)
    d.text((94 * s, 530 * s), "doorslip.org", font=small, fill=MARK)
    right = "an open protocol · v0"
    d.text((1106 * s - d.textlength(right, font=small), 530 * s), right,
           font=small, fill=INK_3)

    return card.resize((W, H), Image.LANCZOS)


if __name__ == "__main__":
    draw_card().save(OUT, "PNG", optimize=True)
    print(f"{OUT}  {OUT.stat().st_size // 1024} KB  {W}x{H}")
