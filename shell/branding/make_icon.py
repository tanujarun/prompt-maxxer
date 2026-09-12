"""Generate the Prompt Maxxer app icon.

A Teenage Engineering orange chassis carrying a cassette: two tape reels
joined by the tape run, over a row of grille dots in the Nothing dot-matrix
motif. Black on orange holds up at 16 px in the tray and reads on both light
and dark taskbars.

Regenerate with the engine venv, which has Pillow:

    ..\\engine\\.venv\\Scripts\\python.exe branding\\make_icon.py
    pnpm tauri icon branding\\icon.png
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
SUPERSAMPLE = 4  # draw large, then downscale, for clean edges at every size

BRAND = (255, 95, 31, 255)  # --brand
INK = (0, 0, 0, 255)

REEL_Y = 452
REEL_XS = (340, 684)
REEL_OUTER = 150
REEL_INNER = 94
HUB = 36
TAPE = 56
GRILLE_Y = 780
GRILLE_STEP = 64
GRILLE_DOT = 16


def main() -> None:
    k = SUPERSAMPLE
    img = Image.new("RGBA", (SIZE * k, SIZE * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def circle(cx: int, cy: int, r: int, fill) -> None:
        draw.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)

    # Chassis. Radius stays a proportion of the size so it matches the
    # squircle Windows uses for app tiles.
    draw.rounded_rectangle([40 * k, 40 * k, 984 * k, 984 * k], radius=208 * k, fill=BRAND)

    for cx in REEL_XS:
        circle(cx, REEL_Y, REEL_OUTER, INK)
        circle(cx, REEL_Y, REEL_INNER, BRAND)
        circle(cx, REEL_Y, HUB, INK)

    # The tape run along the bottom tangent turns two circles into a cassette.
    bottom = REEL_Y + REEL_OUTER
    draw.rectangle([REEL_XS[0] * k, (bottom - TAPE) * k, REEL_XS[1] * k, bottom * k], fill=INK)

    for i in range(-2, 3):
        circle(SIZE // 2 + i * GRILLE_STEP, GRILLE_Y, GRILLE_DOT, INK)

    out = Path(__file__).with_name("icon.png")
    img.resize((SIZE, SIZE), Image.LANCZOS).save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
