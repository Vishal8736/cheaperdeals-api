"""POD designer (v20): makes print designs. Honest scope: these are TEXT designs (slogans in bold lettering, badges, stripes) drawn by code with Pillow,
transparent PNG so they print on dark or light products. They are not AI pictures and not copies of anyone's art. Slogans come from your AI model when one is
connected (told to write original lines, no brands), otherwise from a built-in bank of short generic lines. A blocklist drops anything that names a brand,
a character or a celebrity, because selling those gets stores shut down."""
from __future__ import annotations

import io
import json
import logging
import re
import secrets
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.orm import Session

from ..models import Design
from . import settings_service as S

log = logging.getLogger("cheapedeal.pod")
W, H = 3000, 3600
STYLES = ("stack", "badge", "underline", "retro")
# (main colour, accent colour, outline). The outline is dark so the art stays readable on white AND black products.
PALETTES = {"sunrise": ("#FFB703", "#FB8500", "#14213D"), "lagoon": ("#2EC4B6", "#FF9F1C", "#011627"), "berry": ("#F72585", "#B5179E", "#1B1B2F"),
            "lime": ("#B8F34A", "#38B000", "#10200A"), "sky": ("#4CC9F0", "#4361EE", "#0B132B"), "coral": ("#FF6B6B", "#FFD166", "#2B2D42")}

NICHES = {
    "coffee": ["But First, Coffee", "Powered By Coffee", "Coffee Then Everything", "Brew Crew", "Espresso Yourself", "Decaf Is A Myth"],
    "dogs": ["Dog Mom Energy", "Fur Real Love", "Who Rescued Who", "Dogs Make Me Happy", "Walk More Worry Less", "Paws And Relax"],
    "cats": ["Cat Hair Dont Care", "Purrfectly Imperfect", "Cat Person Club", "Meow Is Enough", "Nap Time Is Sacred", "Whisker Business"],
    "fitness": ["Sweat Now Shine Later", "One More Rep", "Train Insane", "Stronger Every Day", "Gym Hair Dont Care", "Earn Your Rest Day"],
    "gardening": ["Grow Something", "Plant Parent", "Dirt Dont Hurt", "Bloom Where You Are", "Weed Out The Bad Vibes", "Garden Mode On"],
    "teachers": ["Teach Inspire Repeat", "Best Teacher Ever", "Class Is In Session", "Kind Minds Grow", "Teachers Plant Seeds", "Future Is In My Classroom"],
    "travel": ["Wander Often", "Take Me Anywhere", "Lost And Happy", "Pack Light Dream Big", "Passport Ready", "Adventure Mode"],
    "books": ["One More Chapter", "Book Nerd Forever", "Read Dream Repeat", "Lost In A Story", "Bookworm Club", "Reading Is My Cardio"],
    "gaming": ["Pause Mode Activated", "Level Up Daily", "Just One More Game", "Respawn And Retry", "Player Ready", "Born To Play"],
    "positive": ["Good Vibes Only", "Be Kind Always", "Stay Curious", "Small Steps Big Dreams", "Choose Joy", "Begin Again"],
}
# things that must never go on a product (trademarks, characters, famous people): the whole phrase is dropped if any matches
BLOCKED = re.compile(r"\b(nike|adidas|puma|disney|marvel|dc comics|batman|superman|spiderman|pokemon|nintendo|mario|zelda|minecraft|fortnite|roblox|barbie|hello kitty|"
                     r"harry potter|hogwarts|star wars|jedi|yoda|mickey|minnie|frozen|elsa|peppa|paw patrol|coca.?cola|pepsi|starbucks|gucci|louis vuitton|chanel|supreme|"
                     r"apple|iphone|google|amazon|netflix|youtube|tiktok|instagram|facebook|meta|tesla|lego|nfl|nba|fifa|olympics|trump|biden|modi|taylor swift|beyonce|"
                     r"messi|ronaldo|kohli|elon|bts|blackpink|one piece|naruto|goku|dragon ball|squid game|barca|real madrid|ipl|csk|rcb)\b", re.I)
FONTS = ["C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]


def safe_text(t: str) -> bool:
    t = (t or "").strip()
    return 3 <= len(t) <= 48 and not BLOCKED.search(t) and bool(re.fullmatch(r"[A-Za-z0-9 '!?,.&\-]+", t))


def clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", str(t).strip().strip('"\'`.').replace("\u2019", "'"))[:48]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default(size)  # Pillow >= 10.1 ships a scalable font, so this still works on a bare machine


def _wrap(text: str) -> list[str]:
    words = text.split()
    if len(words) <= 1:
        return words or [text]
    best = [" ".join(words)]
    n = 3 if len(words) >= 5 else 2 if len(words) >= 2 else 1
    per = -(-len(words) // n)  # ceil
    lines = [" ".join(words[i:i + per]) for i in range(0, len(words), per)]
    return lines if lines else best


def _fit(draw: ImageDraw.ImageDraw, line: str, maxw: int, cap: int, stroke_ratio: float = 0.06) -> tuple[ImageFont.FreeTypeFont, int, tuple[int, int, int, int]]:
    lo, hi = 40, cap
    while lo < hi:  # biggest size whose width fits
        mid = (lo + hi + 1) // 2
        f = _font(mid)
        box = draw.textbbox((0, 0), line, font=f, stroke_width=max(2, int(mid * stroke_ratio)))
        if box[2] - box[0] <= maxw:
            lo = mid
        else:
            hi = mid - 1
    f = _font(lo)
    sw = max(2, int(lo * stroke_ratio))
    return f, sw, draw.textbbox((0, 0), line, font=f, stroke_width=sw)


def render(text: str, style: str = "stack", palette: str = "sunrise") -> bytes:
    main, accent, outline = PALETTES.get(palette, PALETTES["sunrise"])
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lines = _wrap(text.upper())
    maxw = int(W * (0.62 if style == "badge" else 0.88))
    cap = 760 if style != "badge" else 520
    fitted = [_fit(d, ln, maxw, cap) for ln in lines]
    gap = 70
    total = sum(b[3] - b[1] for _, _, b in fitted) + gap * (len(fitted) - 1)
    y = (H - total) // 2
    if style == "badge":
        r = 1350
        d.ellipse((W // 2 - r, H // 2 - r, W // 2 + r, H // 2 + r), outline=accent, width=70)
        d.ellipse((W // 2 - r + 150, H // 2 - r + 150, W // 2 + r - 150, H // 2 + r - 150), outline=outline, width=24)
    if style == "retro":
        band = total + 360
        top = y - 180
        for i, c in enumerate((accent, main, accent)):
            d.rectangle((0, top + i * band // 3, W, top + (i + 1) * band // 3), fill=c)
    for i, (ln, (f, sw, box)) in enumerate(zip(lines, fitted)):
        w, h = box[2] - box[0], box[3] - box[1]
        color = main if (i % 2 == 0 or style == "retro") else accent
        if style == "retro":
            color = "#FFFFFF"
        d.text(((W - w) // 2 - box[0], y - box[1]), ln, font=f, fill=color, stroke_width=sw, stroke_fill=outline)
        y += h + gap
    if style == "underline":
        bar_y = y + 10
        d.rounded_rectangle((W // 2 - 700, bar_y, W // 2 + 700, bar_y + 90), radius=45, fill=accent, outline=outline, width=12)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def ai_slogans(db: Session, niche: str, n: int) -> list[str]:
    from . import assistant
    if not assistant.configured(db):
        return []
    try:
        c = assistant.config(db)
        raw = assistant.call_model(c, "You write short, original t-shirt slogans. Reply ONLY with a JSON list of strings.", [],
                                   f"Write {n} original slogans (2 to 6 words each, no punctuation tricks) for the niche '{niche}'. Never use brand names, characters, celebrities, "
                                   f"song or movie lines, or known catchphrases. Plain English letters only.")
        m = re.search(r"\[.*\]", raw, re.S)
        return [clean_text(x) for x in json.loads(m.group(0)) if isinstance(x, str)] if m else []
    except Exception as e:  # the bank still works
        log.warning("ai slogans failed: %s", e)
        return []


def slogans(db: Session, niche: str, n: int = 3) -> list[tuple[str, str]]:
    """[(text, source)] that are safe and not already designed."""
    have = {r[0].lower() for r in db.query(Design.text).all()}
    out: list[tuple[str, str]] = []
    for src, items in (("ai", ai_slogans(db, niche, n + 3)), ("builtin", NICHES.get(niche.lower(), [t for v in NICHES.values() for t in v]))):
        for t in items:
            t = clean_text(t)
            if safe_text(t) and t.lower() not in have and t.lower() not in {x[0].lower() for x in out}:
                out.append((t, src))
            if len(out) >= n:
                return out
    return out


def make_design(db: Session, niche: str, text: str, source: str = "builtin", style: Optional[str] = None, palette: Optional[str] = None) -> Design:
    text = clean_text(text)
    if not safe_text(text):
        raise ValueError("Ye text design ke liye surakshit nahi hai (brand/character/celebrity naam ya galat akshar).")
    n = db.query(Design).count()
    style = style if style in STYLES else STYLES[n % len(STYLES)]
    pal = palette if palette in PALETTES else list(PALETTES)[(n * 5 + len(text)) % len(PALETTES)]
    png = render(text, style, pal)
    d = Design(token=secrets.token_hex(12), niche=niche[:80], text=text, style=style, palette=pal, width=W, height=H, png=png, source=source)
    db.add(d)
    db.flush()
    return d


def config(db: Session) -> dict:
    kws = [x.strip().lower() for x in S.get(db, "pod_niches", "coffee,dogs,fitness,teachers,books").split(",") if x.strip()]
    return {"niches": kws or ["positive"], "types": [x.strip() for x in S.get(db, "pod_types", "tshirt,mug,phone_case").split(",") if x.strip()],
            "daily": max(1, min(30, int(S.get(db, "pod_daily", "4") or 4))), "batch": max(1, min(5, int(S.get(db, "pod_batch", "2") or 2))),
            "markup": S.get_decimal(db, "pod_markup") if S.get(db, "pod_markup", "") else __import__("decimal").Decimal("2.2")}
