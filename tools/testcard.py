#!/usr/bin/env python3
"""Write a 1240x1864 test card: colour bars, grey ramp, nested edge borders.

The five nested rectangles at the edge show how much the printer crops on
each side; TOP/BOTTOM show orientation.

Usage: testcard.py out.png
"""

import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1240, 1864


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)
    except OSError:
        return ImageFont.load_default()


def main(out):
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    cols = ["#ff0000", "#00ff00", "#0000ff", "#00ffff", "#ff00ff", "#ffff00", "#000000", "#808080"]
    bw = W // len(cols)
    for i, c in enumerate(cols):
        d.rectangle([i * bw, 300, (i + 1) * bw - 1, 900], fill=c)
    for x in range(W):
        v = int(255 * x / (W - 1))
        d.line([x, 950, x, 1150], fill=(v, v, v))
    for i in range(0, 60, 12):
        d.rectangle([i, i, W - 1 - i, H - 1 - i], outline="black", width=3)
    d.text((W // 2, 150), "TOP", fill="black", font=font(110, True), anchor="mm")
    d.text((W // 2, 1350), "Kodak PD460", fill="black", font=font(110, True), anchor="mm")
    d.text((W // 2, 1750), "BOTTOM", fill="black", font=font(50), anchor="mm")
    im.save(out)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
