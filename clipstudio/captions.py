"""Captions for the Studio page: subtitle lines moved onto a clip's own clock, and the ASS file ffmpeg burns in.

The browser preview draws the same CaptionStyle with CSS, so a style field means the same thing in both places:
sizes and margins are percents of the output frame, never pixels.
"""
from collections.abc import Sequence

from .models import CaptionPosition, CaptionSegment, CaptionStyle

# Fonts the Studio offers. Each is on Windows and macOS, and libass falls back to a close match elsewhere.
FONTS = (
    "Arial", "Arial Black", "Impact", "Verdana", "Tahoma", "Trebuchet MS", "Georgia", "Times New Roman",
    "Courier New", "Comic Sans MS",
)
_ALIGNMENT = {CaptionPosition.BOTTOM: 2, CaptionPosition.MIDDLE: 5, CaptionPosition.TOP: 8}
SIDE_MARGIN = 0.05


def clip_captions(captions: Sequence[CaptionSegment], start: float, end: float) -> list[CaptionSegment]:
    """The lines spoken inside [start, end] of the source video, on the clip's clock and cut to its length."""
    length = end - start
    out = []
    for c in sorted(captions, key=lambda c: c.start):
        if c.end <= start or c.start >= end:
            continue
        out.append(CaptionSegment(
            start=round(max(0.0, c.start - start), 3),
            end=round(min(length, c.end - start), 3),
            text=c.text,
        ))
    return out


def ass_color(hex_color: str, opacity: float = 1.0) -> str:
    """'#rrggbb' as ASS '&HAABBGGRR', where alpha 00 is opaque."""
    r, g, b = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    alpha = round((1.0 - opacity) * 255)
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    h, rest = divmod(centis, 360000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_text(text: str, uppercase: bool) -> str:
    """Caption text that libass draws as written: no override blocks, and line breaks as \\N."""
    clean = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    if uppercase:
        clean = clean.upper()
    return "\\N".join(" ".join(line.split()) for line in clean.splitlines() if line.strip())


def _style_line(name: str, style: CaptionStyle, width: int, height: int) -> str:
    font = style.font if style.font in FONTS else FONTS[0]
    size = max(8, round(style.size / 100 * height))
    outline = round(size * style.outline / 100, 1)
    if style.box:
        # BorderStyle 3 draws an opaque box. libass and VSFilter disagree on which colour fills it, so set both.
        box = ass_color(style.box_color, style.box_opacity)
        border_style, outline_color, back_color, outline = 3, box, box, max(1.0, round(size * 0.18, 1))
    else:
        border_style, outline_color, back_color = 1, ass_color(style.outline_color), "&H00000000"
    fields = [
        name, font, size, ass_color(style.color), ass_color(style.color), outline_color, back_color,
        -1 if style.bold else 0, -1 if style.italic else 0, 0, 0, 100, 100, 0, 0,
        border_style, outline, 0, _ALIGNMENT[style.position],
        round(width * SIDE_MARGIN), round(width * SIDE_MARGIN), round(style.margin / 100 * height), 1,
    ]
    return "Style: " + ",".join(str(f) for f in fields)


def build_ass(
    captions: Sequence[CaptionSegment],
    style: CaptionStyle,
    width: int,
    height: int,
    *,
    title: str = "",
    title_style: CaptionStyle | None = None,
    duration: float = 0.0,
) -> str:
    """A complete ASS script for a frame of width x height. The title, if any, shows for the whole duration."""
    title_style = title_style or CaptionStyle(position=CaptionPosition.TOP)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        _style_line("Caption", style, width, height),
        _style_line("Title", title_style, width, height),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    title_text = ass_text(title, title_style.uppercase)
    if title_text and duration > 0:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(duration)},Title,,0,0,0,,{title_text}")
    for c in sorted(captions, key=lambda c: c.start):
        text = ass_text(c.text, style.uppercase)
        if text and c.end > c.start:
            lines.append(f"Dialogue: 0,{ass_time(c.start)},{ass_time(c.end)},Caption,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"
