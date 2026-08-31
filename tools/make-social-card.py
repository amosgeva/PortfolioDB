"""Render docs/images/social-preview.png — the repo's GitHub social card.

    pip install -r tools/requirements.txt
    python tools/make-social-card.py

Committed because a review found "no source file for the card in the repo — the
PNG is the only artefact", which makes every future tweak a guess. It lives here
rather than in the notes repo because both the screenshot it reads and the card it
writes are in this one.

Upload is manual: Settings → General → Social preview. There is no API and no
`gh` equivalent, which is exactly why the card has looked done since the day it
was committed while GitHub still served its own fallback image.

Two constraints that are not cosmetic:

* **1280×640.** GitHub's own recommendation, and the aspect every consumer crops
  to.
* **The provenance line stays in frame.** Unlike the README, a social card cannot
  carry a caption underneath it, and an unlabelled dollar figure on a financial
  page reads as a claim about someone's real returns.
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
BG, SURFACE = (26, 27, 38), (33, 35, 48)
FG, MUTED, ACCENT, CODE = (233, 235, 245), (150, 155, 175), (99, 102, 241), (150, 220, 180)

ROOT = pathlib.Path(__file__).resolve().parents[1]
HERO = ROOT / "docs" / "images" / "portfolio.webp"
OUT = ROOT / "docs" / "images" / "social-preview.png"

# The install line has to match what the README actually tells people to run.
# It said `make init && make up` while `make up` is nothing but `compose up -d`
# — so the card promised a dashboard against a database with no tables.
INSTALL = "$ make init && make up && make schema"
PROVENANCE = "Fictional demo data — not a real portfolio, not advice"


def font(px: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def mono(px: int) -> ImageFont.FreeTypeFont:
    for name in ("consola.ttf", "CascadiaMono.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return font(px)


def render() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Right: the KPI band, cropped past the left rail. Proof that software exists.
    shot = Image.open(HERO).convert("RGB")
    crop = shot.crop((248, 130, shot.width - 40, 560))
    width = 640
    crop = crop.resize((width, int(crop.height * width / crop.width)), Image.LANCZOS)
    px, py = W - width - 56, 150
    d.rounded_rectangle([px - 10, py - 10, px + width + 10, py + crop.height + 10], 14, fill=SURFACE)
    img.paste(crop, (px, py))
    d.text((px, py + crop.height + 20), PROVENANCE, font=font(15), fill=MUTED)

    # Left: mark, the three-beat headline, the mechanism, the install line.
    x = 72
    d.rounded_rectangle([x, 96, x + 44, 140], 12, fill=ACCENT)
    d.text((x + 11, 104), "P", font=font(26, bold=True), fill=(255, 255, 255))
    d.text((x + 60, 100), "PortfolioDB", font=font(38, bold=True), fill=FG)

    d.text((x, 206), "Your portfolio ledger.", font=font(44, bold=True), fill=FG)
    d.text((x, 260), "Your database.", font=font(44, bold=True), fill=FG)
    d.text((x, 314), "Nobody else's server.", font=font(44, bold=True), fill=ACCENT)

    d.text((x, 396), "Append-only lots on Postgres. FIFO and", font=font(20), fill=MUTED)
    d.text((x, 424), "average-cost recomputed on every read.", font=font(20), fill=MUTED)

    d.rounded_rectangle([x, 480, x + 560, 530], 10, fill=SURFACE)
    d.text((x + 18, 494), INSTALL, font=mono(18), fill=CODE)
    d.text((x, 562), "AGPL-3.0  ·  self-hosted  ·  no accounts, no telemetry",
           font=font(17), fill=MUTED)
    return img


if __name__ == "__main__":
    card = render()
    card.save(OUT, optimize=True)
    print(f"{OUT.relative_to(ROOT)}: {card.width}x{card.height}, {OUT.stat().st_size // 1024}KB")
    print("Upload it at Settings -> General -> Social preview (no API for this).")
