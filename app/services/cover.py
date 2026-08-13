"""Designed cover images for channel posts.

WhatsApp channels do not render document attachments well, and a bare link in a
text message gets scraped into whatever card the destination happens to expose -
a Drive spreadsheet link renders as a blurry "Loading Google Sheets" tile. A
media message cannot carry a preview card at all, so leading the post with a
generated image both looks deliberate and suppresses the scraped one.

The cover is drawn with reportlab and rasterised by pymupdf, which are already
dependencies. `fitz.Story` was tried and rejected: it renders `border-radius`
as square paths, so the pills and buttons come out as rectangles.

Covers must not all look alike. The LAYOUT is chosen from the shape of the
content - a big number gets the stat treatment, several short points get the
list treatment, a standalone line gets the quote treatment - and the PALETTE is
derived from the text, so the same post always renders identically while
consecutive different posts do not.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as pdfcanvas

from . import assets

log = logging.getLogger(__name__)

# 4:5 portrait - the largest shape WhatsApp shows without cropping. At 144 dpi
# this rasterises to exactly 1080x1350.
COVER_W, COVER_H = 540.0, 675.0
COVER_DPI = 144
PAD = 38.0

INK = HexColor("#0B1220")
MUTED = HexColor("#5B6474")
WHITE = HexColor("#FFFFFF")

# (accent, soft wash, deep ground). Same family as the dashboard, so a post
# still reads as coming from this app.
PALETTES: dict[str, tuple[str, str, str]] = {
    "indigo": ("#4F46E5", "#EEF0FE", "#312E81"),
    "blue": ("#2563EB", "#EFF4FF", "#1E3A8A"),
    "teal": ("#0D9488", "#ECFDF9", "#134E4A"),
    "violet": ("#7C3AED", "#F5F0FF", "#4C1D95"),
    "amber": ("#D97706", "#FFF7ED", "#7C2D12"),
    "rose": ("#E11D48", "#FFF1F3", "#881337"),
}
PALETTE_ORDER = list(PALETTES)
LAYOUTS = ("hero", "stat", "list", "quote")

LOGO_PATH = Path(__file__).resolve().parents[1] / "static" / "img" / "upsurge-icon.png"


@dataclass
class CoverSpec:
    """Everything the picture needs. Every field is optional but `headline`."""

    headline: str = ""
    brand: str = ""
    tagline: str = ""
    badge: str = ""
    eyebrow: str = ""
    highlight: str = ""       # phrase pulled into a solid accent box
    body: str = ""
    chips: list[str] = field(default_factory=list)   # short pills
    points: list[str] = field(default_factory=list)  # numbered list
    stat: str = ""
    stat_label: str = ""
    quote: str = ""
    attribution: str = ""
    cta: str = ""
    footer: str = ""
    layout: str = ""          # force one, else inferred
    palette: str = ""         # force one, else derived
    avoid_palettes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Choosing how it looks
# ---------------------------------------------------------------------------


def pick_palette(seed: str, avoid: list[str] | None = None) -> str:
    """Stable for the same text, but never one of the last two used."""
    avoid = [a for a in (avoid or []) if a in PALETTES][:2]
    digest = hashlib.sha1(seed.encode("utf-8", "ignore")).digest()
    start = digest[0] % len(PALETTE_ORDER)
    for step in range(len(PALETTE_ORDER)):
        name = PALETTE_ORDER[(start + step) % len(PALETTE_ORDER)]
        if name not in avoid:
            return name
    return PALETTE_ORDER[start]


def pick_layout(spec: CoverSpec) -> str:
    """From the shape of the content, not at random."""
    if spec.layout in LAYOUTS:
        return spec.layout
    if spec.quote:
        return "quote"
    if len(spec.points) >= 3:
        return "list"
    if spec.stat.strip() and re.search(r"\d", spec.stat):
        return "stat"
    return "hero"


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    words, lines, cur = (text or "").split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if pdfmetrics.stringWidth(trial, font, size) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit(text: str, font: str, start: float, width: float, floor: float = 18) -> float:
    size = start
    while size > floor and pdfmetrics.stringWidth(text, font, size) > width:
        size -= 1
    return size


def _shrink_to_lines(text: str, font: str, start: float, width: float,
                     max_lines: int, floor: float = 20) -> tuple[float, list[str]]:
    size = start
    lines = _wrap(text, font, size, width)
    while len(lines) > max_lines and size > floor:
        size -= 2
        lines = _wrap(text, font, size, width)
    return size, lines


def _pill(c, x, y, label, font, size, *, fill=None, stroke=None,
          text_color=INK, pad=12.0, h=24.0) -> float:
    width = pdfmetrics.stringWidth(label, font, size) + pad * 2
    radius = min(h / 2, width / 2)
    c.setLineWidth(1.2)
    if fill is not None:
        c.setFillColor(fill)
        c.roundRect(x, y, width, h, radius, stroke=0, fill=1)
    if stroke is not None:
        c.setStrokeColor(stroke)
        c.roundRect(x, y, width, h, radius, stroke=1, fill=0)
    c.setFillColor(text_color)
    c.setFont(font, size)
    c.drawString(x + pad, y + (h - size) / 2 + 1.5, label)
    return width


def _clean(text: str, font: str) -> str:
    """Anything the drawing font cannot render becomes something readable."""
    return assets._fit_to_font(text or "", font)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_cover(spec: CoverSpec, *, dpi: int = COVER_DPI) -> bytes:
    assets._register_fonts()
    reg, bold = assets.FONT_REGULAR, assets.FONT_BOLD
    display = assets.FONT_DISPLAY

    layout = pick_layout(spec)
    seed = f"{spec.headline}|{spec.body}|{spec.quote}"
    palette = spec.palette if spec.palette in PALETTES else pick_palette(
        seed, spec.avoid_palettes
    )
    accent, soft, deep = (HexColor(x) for x in PALETTES[palette])

    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=(COVER_W, COVER_H))
    c.setFillColor(WHITE)
    c.rect(0, 0, COVER_W, COVER_H, stroke=0, fill=1)

    # Each layout gets its own ground, so two posts sharing a palette still do
    # not look like the same picture.
    on_dark = layout == "quote"
    if layout == "hero":
        c.setFillColor(soft)
        c.circle(COVER_W - 26, COVER_H - 36, 110, stroke=0, fill=1)
        c.circle(28, 74, 72, stroke=0, fill=1)
    elif layout == "stat":
        c.setFillColor(soft)
        c.rect(0, COVER_H - 320, COVER_W, 320, stroke=0, fill=1)
    elif layout == "list":
        c.setFillColor(soft)
        c.rect(0, 0, 14, COVER_H, stroke=0, fill=1)
        c.circle(COVER_W - 40, COVER_H - 46, 86, stroke=0, fill=1)
    elif on_dark:
        c.setFillColor(deep)
        c.rect(0, 0, COVER_W, COVER_H, stroke=0, fill=1)

    y = COVER_H - PAD - 28
    _draw_brand(c, spec, accent, y, on_dark, reg, bold)
    y -= 54

    if layout == "hero":
        y = _draw_hero(c, spec, y, accent, soft, reg, bold, display)
    elif layout == "stat":
        y = _draw_stat(c, spec, y, accent, reg, bold, display)
    elif layout == "list":
        y = _draw_list(c, spec, y, accent, soft, reg, bold, display)
    else:
        y = _draw_quote(c, spec, accent, deep, reg, bold, display)

    _draw_footer(c, spec, accent, deep, on_dark, reg, bold)

    c.showPage()
    c.save()

    doc = pymupdf.open(stream=buf.getvalue(), filetype="pdf")
    try:
        return doc[0].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()


def _draw_brand(c, spec: CoverSpec, accent, y, on_dark: bool, reg, bold) -> None:
    brand = _clean(spec.brand, bold).upper()
    if on_dark:
        if brand:
            c.setFillColor(WHITE)
            c.setFont(bold, 14)
            c.drawString(PAD, y + 14, brand)
        return

    text_x = PAD
    if LOGO_PATH.exists():
        try:
            c.drawImage(str(LOGO_PATH), PAD, y - 2, width=28, height=28,
                        mask="auto", preserveAspectRatio=True)
            text_x = PAD + 38
        except Exception:
            text_x = PAD
    if text_x == PAD and brand:
        c.setFillColor(accent)
        c.circle(PAD + 14, y + 12, 14, stroke=0, fill=1)
        text_x = PAD + 38

    if brand:
        c.setFillColor(INK)
        c.setFont(bold, 14)
        c.drawString(text_x, y + 14, brand)
    if spec.tagline:
        c.setFillColor(MUTED)
        c.setFont(reg, 7)
        c.drawString(text_x, y + 4, _clean(spec.tagline, reg).upper())

    if spec.badge:
        label = _clean(spec.badge, bold).upper()[:22]
        width = pdfmetrics.stringWidth(label, bold, 8.5) + 22
        c.setFillColor(accent)
        c.roundRect(COVER_W - PAD - width, y + 4, width, 20, 10, stroke=0, fill=1)
        c.setFillColor(WHITE)
        c.setFont(bold, 8.5)
        c.drawString(COVER_W - PAD - width + 11, y + 10, label)


def _draw_hero(c, spec, y, accent, soft, reg, bold, display) -> float:
    inner = COVER_W - PAD * 2

    # Measure first so leftover space becomes breathing room between elements
    # rather than one dead gap above the button.
    head = _clean(spec.headline, display)
    probe_size, probe_lines = _shrink_to_lines(head, display, 38, inner, 3, 24)
    hi = _clean(spec.highlight, display)
    hi_size = _fit(hi, display, 32, inner - 32, 20) if hi else 0
    body_lines = _wrap(_clean(spec.body, reg), reg, 12.5, inner)[:3] if spec.body else []

    used = sum((
        40 if spec.eyebrow else 0,
        len(probe_lines) * probe_size * 1.08,
        (hi_size + 42) if hi else 0,
        (44 + len(body_lines) * 18) if body_lines else 0,
        (16 + 28) if spec.chips else 0,
    ))
    floor = (PAD + 22 + 40 + 34) if spec.cta else (PAD + 44)
    slack = max(0.0, (y - floor) - used)
    gaps = 1 + bool(spec.eyebrow) + bool(hi) + bool(body_lines) + bool(spec.chips)
    air = min(slack / max(gaps, 1), 30.0)
    # Whatever the gaps could not absorb is pushed above the block, so a short
    # post sits optically centred instead of clinging to the top with a dead
    # half-page beneath it. 0.42 rather than 0.5 reads better than true centre.
    y -= (air * 0.5) + max(0.0, slack - air * gaps) * 0.42

    if spec.eyebrow:
        _pill(c, PAD, y, _clean(spec.eyebrow, bold).upper()[:40], bold, 8,
              fill=soft, text_color=accent, pad=10, h=20)
        y -= 40 + air

    c.setFillColor(INK)
    for line in probe_lines:
        y -= probe_size * 1.08
        c.setFont(display, probe_size)
        c.drawString(PAD, y, line)

    if hi:
        width = pdfmetrics.stringWidth(hi, display, hi_size)
        y -= hi_size + 42 + air
        c.setFillColor(accent)
        c.roundRect(PAD, y, width + 32, hi_size + 20, 10, stroke=0, fill=1)
        c.setFillColor(WHITE)
        c.setFont(display, hi_size)
        c.drawString(PAD + 16, y + 15, hi)

    if body_lines:
        y -= 44 + air
        c.setFillColor(MUTED)
        c.setFont(reg, 12.5)
        for line in body_lines:
            c.drawString(PAD, y, line)
            y -= 18

    if spec.chips:
        y -= 16 + air
        x = PAD
        tones = [accent, HexColor("#0D9488"), HexColor("#D97706")]
        for i, chip in enumerate(spec.chips[:3]):
            tone = tones[i % len(tones)]
            label = _clean(chip, bold)[:22]
            x += _pill(c, x, y, label, bold, 11, stroke=tone,
                       text_color=tone, pad=14, h=28) + 10
    return y


def _draw_stat(c, spec, y, accent, reg, bold, display) -> float:
    inner = COVER_W - PAD * 2
    if spec.eyebrow:
        _pill(c, PAD, y, _clean(spec.eyebrow, bold).upper()[:40], bold, 8,
              fill=WHITE, text_color=accent, pad=10, h=20)
        y -= 42

    stat = _clean(spec.stat, display)[:12]
    size = _fit(stat, display, 128, inner, 52)
    y -= size * 0.94
    c.setFillColor(accent)
    c.setFont(display, size)
    c.drawString(PAD, y, stat)

    if spec.stat_label:
        y -= 30
        c.setFillColor(INK)
        c.setFont(bold, 19)
        c.drawString(PAD, y, _clean(spec.stat_label, bold)[:46])

    y -= 52
    head_size, lines = _shrink_to_lines(
        _clean(spec.headline, bold), bold, 26, inner, 3, 17
    )
    c.setFillColor(INK)
    for line in lines:
        c.setFont(bold, head_size)
        c.drawString(PAD, y, line)
        y -= head_size * 1.16

    if spec.body:
        y -= 14
        c.setFillColor(MUTED)
        c.setFont(reg, 12.5)
        for line in _wrap(_clean(spec.body, reg), reg, 12.5, inner)[:3]:
            c.drawString(PAD, y, line)
            y -= 18
    return y


def _draw_list(c, spec, y, accent, soft, reg, bold, display) -> float:
    inner = COVER_W - PAD * 2 - 20
    if spec.eyebrow:
        _pill(c, PAD + 12, y, _clean(spec.eyebrow, bold).upper()[:40], bold, 8,
              fill=soft, text_color=accent, pad=10, h=20)
        y -= 40

    size, lines = _shrink_to_lines(_clean(spec.headline, display), display,
                                  31, inner, 3, 20)
    c.setFillColor(INK)
    for line in lines:
        y -= size * 1.1
        c.setFont(display, size)
        c.drawString(PAD + 12, y, line)

    points = spec.points[:5]
    y -= 34
    # Space the rows to fill what is left rather than clumping under the title.
    room = y - (PAD + 22 + 40 + 26)
    step = max(30.0, min(44.0, room / max(len(points), 1)))
    for i, point in enumerate(points):
        c.setFillColor(accent)
        c.circle(PAD + 26, y + 5, 13, stroke=0, fill=1)
        c.setFillColor(WHITE)
        c.setFont(bold, 12)
        c.drawCentredString(PAD + 26, y + 1, str(i + 1))
        c.setFillColor(INK)
        label = _clean(point, reg)
        c.setFont(reg, _fit(label, reg, 15, COVER_W - PAD * 2 - 66, 10))
        c.drawString(PAD + 48, y, label)
        y -= step
    return y


def _draw_quote(c, spec, accent, deep, reg, bold, display) -> float:
    inner = COVER_W - PAD * 2
    c.setFillColor(accent)
    c.setFont(display, 92)
    c.drawString(PAD - 4, COVER_H - 196, '"')

    text = _clean(spec.quote or spec.headline, display)
    size, lines = _shrink_to_lines(text, display, 29, inner, 6, 17)

    # Centre the block in the space between the mark and the button.
    top, floor = COVER_H - 236, PAD + 100
    block = len(lines) * size * 1.32
    y = top - max(0.0, ((top - floor) - block) / 2)

    c.setFillColor(WHITE)
    for line in lines:
        c.setFont(display, size)
        c.drawString(PAD, y, line)
        y -= size * 1.32

    if spec.attribution:
        y -= 10
        c.setFillColor(HexColor("#CBD5F5"))
        c.setFont(reg, 13)
        c.drawString(PAD, y, f"- {_clean(spec.attribution, reg)[:44]}")
    return y


def _draw_footer(c, spec, accent, deep, on_dark: bool, reg, bold) -> None:
    if spec.cta:
        label = _clean(spec.cta, bold)[:34]
        width = pdfmetrics.stringWidth(label, bold, 13.5) + 50
        width = min(width, COVER_W - PAD * 2)
        c.setFillColor(WHITE if on_dark else accent)
        c.roundRect(PAD, PAD + 22, width, 40, 11, stroke=0, fill=1)
        c.setFillColor(deep if on_dark else WHITE)
        c.setFont(bold, 13.5)
        c.drawString(PAD + 25, PAD + 36, label)

    if spec.footer and not on_dark:
        c.setFillColor(MUTED)
        c.setFont(reg, 7.5)
        for i, line in enumerate(spec.footer.split("\n")[:2]):
            c.drawRightString(COVER_W - PAD, PAD + 42 - i * 11,
                              _clean(line, reg).upper()[:40])


# ---------------------------------------------------------------------------
# Writing it out
# ---------------------------------------------------------------------------


def build_cover(spec: CoverSpec, *, stem: str = "cover") -> tuple[Path, str]:
    """Render and save. Returns (path, filename)."""
    png = render_cover(spec)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{assets.slugify(stem, 'cover')[:48]}-cover-{stamp}.png"
    path = assets.ASSET_DIR / name
    path.write_bytes(png)
    log.info("Cover written: %s (%.0f KB)", name, len(png) / 1024)
    return path, name


# ---------------------------------------------------------------------------
# Turning generated content into a cover
# ---------------------------------------------------------------------------


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# A short quantified phrase is what belongs in the highlight box.
_PUNCHY = re.compile(
    r"\b(\d+[\d,.]*\s*(?:%|x|hours?|mins?|minutes?|days?|weeks?|tools?|steps?|"
    r"prompts?|points?|crore|lakh|bn|billion|million)\b[^.!?,]{0,18})",
    re.IGNORECASE,
)
_LEADING_NUMBER = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")
# "shed 600 points", "up 12%", "Rs. 2,600 crore" - the number a cover can lead
# with, plus the words that say what it counts.
_STAT = re.compile(
    r"\b(\d[\d,]*(?:\.\d+)?%?)\s+"
    r"((?:points?|percent|crore|lakh|million|billion|bps|stocks?|tools?|"
    r"prompts?|steps?|hours?|minutes?|days?|weeks?|months?)"
    r"(?:\s+\w+){0,2})",
    re.IGNORECASE,
)


# Where a captured stat label stops being about the number.
_LABEL_STOP = {
    "as", "after", "and", "but", "while", "with", "on", "in", "at", "to",
    "for", "from", "of", "amid", "over", "since", "when", "that", "which",
}


def _trim_label(unit: str) -> str:
    """"points as crude" -> "points". Keep what the number counts, drop the rest."""
    words: list[str] = []
    for word in (unit or "").split():
        if word.lower().strip(",.;:") in _LABEL_STOP:
            break
        words.append(word)
    return " ".join(words).strip(" ,.;:")[:40] or "and counting"


def _strip_markup(text: str) -> str:
    out = re.sub(r"https?://\S+", "", text or "")
    out = re.sub(r"[*_~`#]+", "", out)
    return re.sub(r"\s+", " ", out).strip()


def spec_from_content(
    content: str,
    *,
    brand: str = "",
    tagline: str = "",
    badge: str = "",
    payload: dict[str, Any] | None = None,
    has_link: bool = False,
    avoid_palettes: list[str] | None = None,
) -> CoverSpec:
    """Build a cover from whatever the model produced.

    Works from a structured payload when there is one (PDF/Excel campaigns) and
    falls back to reading the message text, so a plain message-only channel
    post still gets a real cover instead of nothing.
    """
    payload = payload or {}
    lines = [l.strip() for l in (content or "").splitlines() if l.strip()]
    plain = _strip_markup(content)

    headline = _strip_markup(str(payload.get("title") or ""))
    if not headline and lines:
        headline = _strip_markup(lines[0])
    headline = headline[:120] or "An update for you"

    # Bullet-ish lines make a numbered list; otherwise the prose becomes body.
    bullets = [
        _strip_markup(_LEADING_NUMBER.sub("", l))
        for l in lines[1:]
        if _LEADING_NUMBER.match(l)
    ]
    points = [b for b in bullets if 3 <= len(b) <= 58][:5]

    rest = plain[len(headline):].strip(" -–—:") if plain.startswith(headline[:40]) else plain
    sentences = [s.strip() for s in _SENTENCE.split(rest) if s.strip()]
    body = ""
    for sentence in sentences:
        if len(sentence) > 24:
            body = sentence[:150]
            break

    highlight = ""
    match = _PUNCHY.search(plain)
    if match and len(match.group(1)) <= 26:
        highlight = match.group(1).strip()
        # A phrase already carrying the headline adds nothing.
        if highlight.lower() in headline.lower():
            highlight = ""

    chips = [
        _strip_markup(str(c))[:20]
        for c in (payload.get("chips") or [])
        if str(c).strip()
    ][:3]

    spec = CoverSpec(
        headline=headline,
        brand=brand,
        tagline=tagline,
        badge=badge,
        eyebrow=_strip_markup(str(payload.get("eyebrow") or ""))[:38],
        highlight=highlight,
        body=body,
        chips=chips,
        points=points if not chips else [],
        cta="Download - link below" if has_link else "",
        footer="Save · Share · Use",
        avoid_palettes=avoid_palettes or [],
    )

    # A prominent number is the strongest thing a cover can lead with, so it
    # gets pulled out and the rest becomes supporting copy.
    if not points and not chips:
        stat = _STAT.search(plain)
        if stat:
            value, unit = stat.group(1), (stat.group(2) or "").strip()
            # Only worth the stat treatment when the number is not already the
            # whole point of the headline's first few words.
            if len(value) <= 7:
                spec.stat = value
                spec.stat_label = _trim_label(unit)
                if spec.highlight.startswith(value):
                    spec.highlight = ""

    # An aphorism reads best as a quote - but it has to actually be one. The
    # earlier test caught almost everything, because most short posts have no
    # body and a headline over 60 characters.
    if (not points and not chips and not spec.stat and not highlight
            and 60 <= len(headline) <= 170
            and not re.search(r"\d", headline)
            and len(_SENTENCE.split(plain)) == 1
            and ":" not in headline):
        spec.quote = headline
        spec.headline = ""
    return spec
