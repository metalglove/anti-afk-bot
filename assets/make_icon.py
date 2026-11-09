"""Draws the app icon (icon.ico and icon.png in this folder). Run: python assets/make_icon.py

A blue rounded square with a white loop arrow around a play triangle: the bot keeps
you playing.
"""
import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024  # drawn large, then scaled down smoothly


def draw():
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    # Background: vertical gradient, rounded square.
    grad = Image.new('RGBA', (S, S))
    top, bottom = (59, 130, 246), (29, 78, 216)
    for y in range(S):
        t = y / (S - 1)
        grad.paste(tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,), (0, y, S, y + 1))
    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((24, 24, S - 24, S - 24), radius=220, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    c, r, w = S / 2, 300, 78
    # Loop arrow: an arc with a gap at the top right, arrowhead at its end.
    start, end = -50, 250  # degrees, clockwise from 3 o'clock (PIL convention)
    d.arc((c - r, c - r, c + r, c + r), start=start, end=end, fill=white, width=w)
    a = math.radians(start)
    mid = r - w / 2
    tip_x, tip_y = c + mid * math.cos(a), c + mid * math.sin(a)
    # Arrowhead pointing along the arc direction at its start (counter-clockwise going up).
    tangent = a - math.pi / 2
    normal = a
    head, half = 150, 120
    p1 = (tip_x + head * math.cos(tangent), tip_y + head * math.sin(tangent))
    p2 = (tip_x + half * math.cos(normal), tip_y + half * math.sin(normal))
    p3 = (tip_x - half * math.cos(normal), tip_y - half * math.sin(normal))
    d.polygon([p1, p2, p3], fill=white)
    # Play triangle in the middle.
    t = 150
    d.polygon([(c - t * 0.6, c - t), (c - t * 0.6, c + t), (c + t * 0.95, c)], fill=white)
    return img


if __name__ == '__main__':
    icon = draw()
    icon.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, 'icon.png'))
    icon.save(os.path.join(HERE, 'icon.ico'), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Wrote assets/icon.ico and assets/icon.png")
