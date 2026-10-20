#!/usr/bin/env python3
"""Write a 1240x1864 calibration card with rulers on all four edges.

Each ruler has a tick every 10 px and a number every 50 px, counting inward
from that edge. On the printed sheet, the first number visible on each side is
how many pixels the printer trims there.

Usage: calibrate.py out.png
"""

import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1240, 1864
RULER = 200  # how far each ruler reaches in from its edge


def font(size):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def main(out):
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f = font(22)
    colours = {"left": "#d00000", "right": "#0050d0", "top": "#008000", "bottom": "#a000a0"}

    for n in range(0, RULER + 1, 10):
        long = n % 50 == 0
        size = 60 if long else 30
        # Left and right: vertical ticks along the middle band.
        for side, x in (("left", n), ("right", W - 1 - n)):
            c = colours[side]
            d.line([x, H // 2 - size, x, H // 2 + size], fill=c, width=2)
            if long:
                d.text((x, H // 2 + 70), str(n), fill=c, font=f, anchor="mt")
        # Top and bottom: horizontal ticks along the middle band.
        for side, y in (("top", n), ("bottom", H - 1 - n)):
            c = colours[side]
            d.line([W // 2 - size, y, W // 2 + size, y], fill=c, width=2)
            if long:
                d.text((W // 2 + 70, y), str(n), fill=c, font=f, anchor="lm")

    # Labels and a centre cross so the sheet reads unambiguously.
    big = font(40)
    d.text((RULER + 20, H // 2 - 120), "LEFT", fill=colours["left"], font=big, anchor="lm")
    d.text((W - RULER - 20, H // 2 - 120), "RIGHT", fill=colours["right"], font=big, anchor="rm")
    d.text((W // 2 - 70, RULER - 20), "TOP", fill=colours["top"], font=big, anchor="rm")
    d.text((W // 2 - 70, H - RULER + 20), "BOTTOM", fill=colours["bottom"], font=big, anchor="rm")
    d.line([W // 2 - 40, H // 2, W // 2 + 40, H // 2], fill="black", width=2)
    d.line([W // 2, H // 2 - 40, W // 2, H // 2 + 40], fill="black", width=2)
    d.text((W // 2, H // 2 + 200), f"{W}x{H} calibration", fill="black", font=font(30), anchor="mm")
    im.save(out)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
