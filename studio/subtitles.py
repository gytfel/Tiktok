"""Субтитры в стиле TikTok: короткие фразы по 1–5 слов с подсветкой текущего слова.

Генерируется файл ASS, который ffmpeg (libass) «впекает» в видео.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from PIL import ImageFont

from . import config
from .text_utils import Token

SAFE_MARGIN = 80  # отступ текста от краёв кадра, px


@dataclass
class SubtitleStyle:
    font: str = config.DEFAULT_FONT
    size: int = 90
    color: str = "#FFFFFF"
    highlight: str = "#FFE600"
    outline_color: str = "#000000"
    outline: float = 6
    shadow: float = 3
    style: str = "outline"  # outline | box
    box_color: str = "#000000"
    box_opacity: float = 0.6
    position: float = 62  # центр текста, % от высоты кадра
    words_per_chunk: int = 3
    uppercase: bool = True
    highlight_mode: str = "color"  # color | scale | none
    animation: str = "pop"  # pop | none


@dataclass
class TitleStyle:
    text: str = ""
    duration: float = 0  # 0 — весь ролик
    font: str = config.DEFAULT_FONT
    size: int = 66
    color: str = "#000000"
    box_color: str = "#FFFFFF"
    position: float = 15


@dataclass
class Chunk:
    tokens: list[Token]
    start: float
    end: float


# ----------------------------------------------------------- цвета и время


def ass_color(hex_color: str, alpha: float = 1.0) -> str:
    """#RRGGBB + непрозрачность 0..1 -> &HAABBGGRR."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        r, g, b = 255, 255, 255
    a = int(round((1 - max(0.0, min(1.0, alpha))) * 255))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def ass_tag_color(hex_color: str) -> str:
    """#RRGGBB -> &HBBGGRR& для тегов вида \\1c."""
    return ass_color(hex_color)[:2] + ass_color(hex_color)[4:] + "&"


def ass_time(seconds: float) -> str:
    cs = int(round(max(0.0, seconds) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _q(seconds: float) -> float:
    """Квантование до сотых — одинаково для конца и начала соседних событий."""
    return round(seconds * 100) / 100


def ass_escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


# --------------------------------------------------------- измерение текста


def _font_path(font_key: str) -> str:
    info = config.FONTS.get(font_key) or config.FONTS[config.DEFAULT_FONT]
    return str(config.FONTS_DIR / info["file"])


@lru_cache(maxsize=16)
def font_height_ratio(font_key: str) -> float:
    """Во сколько раз размер шрифта в libass (высота ascent+descent) больше кегля (em)."""
    ascent, descent = ImageFont.truetype(_font_path(font_key), 100).getmetrics()
    return max(1, ascent + descent) / 100


@lru_cache(maxsize=32)
def _font(font_key: str, ass_size: int) -> ImageFont.FreeTypeFont:
    em = max(1, int(round(ass_size / font_height_ratio(font_key))))
    return ImageFont.truetype(_font_path(font_key), em)


def text_width(text: str, font_key: str, size: int) -> float:
    return _font(font_key, size).getlength(text)


def layout_lines(words: list[str], font_key: str, size: int, max_width: float, max_lines: int = 2) -> tuple[list[list[int]], float]:
    """Разбивает слова на минимальное число ровных строк (индексы слов) и
    возвращает масштаб, если даже так текст не помещается по ширине."""
    space = text_width(" ", font_key, size)
    widths = [text_width(w, font_key, size) for w in words]
    n = len(words)
    best: tuple[list[list[int]], float] = ([list(range(n))], 0.0)
    for k in range(1, max(1, min(max_lines, n)) + 1):
        best = _balanced_partition(widths, space, k)
        if best[1] <= max_width:
            return best[0], 1.0
    lines, widest = best
    return lines, (max_width / widest if widest > 0 else 1.0)


def _balanced_partition(widths: list[float], space: float, k: int) -> tuple[list[list[int]], float]:
    """Делит слова на k строк так, чтобы самая широкая строка была минимальной."""
    n = len(widths)
    prefix = [0.0]
    for w in widths:
        prefix.append(prefix[-1] + w)

    def line_w(i: int, j: int) -> float:
        return prefix[j] - prefix[i] + space * (j - i - 1)

    inf = float("inf")
    # cost[m][j] — лучшая ширина для первых j слов в m строках.
    cost = [[inf] * (n + 1) for _ in range(k + 1)]
    cut = [[0] * (n + 1) for _ in range(k + 1)]
    cost[0][0] = 0.0
    for m in range(1, k + 1):
        for j in range(m, n + 1):
            for i in range(m - 1, j):
                c = max(cost[m - 1][i], line_w(i, j))
                if c < cost[m][j]:
                    cost[m][j], cut[m][j] = c, i
    lines: list[list[int]] = []
    j = n
    for m in range(k, 0, -1):
        i = cut[m][j]
        lines.append(list(range(i, j)))
        j = i
    lines.reverse()
    return lines, cost[k][n]


# -------------------------------------------------------------- фразы


def build_chunks(tokens: list[Token], words_per_chunk: int, max_width: float, style: SubtitleStyle, total: float) -> list[Chunk]:
    """Группирует слова в короткие фразы с учётом пунктуации и пауз."""
    words_per_chunk = max(1, min(8, int(words_per_chunk)))
    max_chunk_width = max_width * 2  # до двух строк
    groups: list[list[Token]] = []
    cur: list[Token] = []
    for tok in tokens:
        if cur:
            gap = (tok.start or 0) - (cur[-1].end or 0)
            candidate = " ".join(_display(t.text, style) for t in cur + [tok])
            too_wide = text_width(candidate, style.font, style.size) > max_chunk_width
            if len(cur) >= words_per_chunk or tok.paragraph != cur[-1].paragraph or gap > 0.45 or too_wide:
                groups.append(cur)
                cur = []
        cur.append(tok)
        if tok.ends_sentence or (tok.ends_clause and words_per_chunk > 1):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    chunks: list[Chunk] = []
    for i, g in enumerate(groups):
        start = g[0].start or 0.0
        last_end = g[-1].end or start + 0.3
        if i + 1 < len(groups):
            nxt = groups[i + 1][0].start or last_end
            # Короткие паузы не показываем пустым экраном.
            end = nxt if nxt - last_end < 0.8 else last_end + 0.25
        else:
            end = min(total, last_end + 0.6) if total else last_end + 0.6
        chunks.append(Chunk(g, start, max(end, start + 0.1)))
    return chunks


def _display(text: str, style: SubtitleStyle) -> str:
    return text.upper() if style.uppercase else text


# ------------------------------------------------------------- ASS файл


def _style_line(name: str, font_key: str, size: int, primary: str, outline_c: str, back_c: str, border_style: int, outline: float, shadow: float) -> str:
    info = config.FONTS.get(font_key) or config.FONTS[config.DEFAULT_FONT]
    bold = -1 if info.get("bold") else 0
    return (
        f"Style: {name},{info['family']},{size},{primary},{primary},{outline_c},{back_c},"
        f"{bold},0,0,0,100,100,0,0,{border_style},{outline:g},{shadow:g},5,{SAFE_MARGIN},{SAFE_MARGIN},0,1"
    )


def build_ass(tokens: list[Token], style: SubtitleStyle, total: float, title: TitleStyle | None = None, enabled: bool = True) -> str:
    max_width = config.WIDTH - 2 * SAFE_MARGIN
    if style.highlight_mode == "scale":
        max_width *= 0.9
    y = int(config.HEIGHT * max(5.0, min(95.0, style.position)) / 100)

    if style.style == "box":
        caption_style = _style_line(
            "Caption", style.font, style.size, ass_color(style.color),
            ass_color(style.box_color, style.box_opacity), ass_color(style.box_color, style.box_opacity),
            3, max(8, style.outline * 2), 0,
        )
    else:
        caption_style = _style_line(
            "Caption", style.font, style.size, ass_color(style.color),
            ass_color(style.outline_color), ass_color("#000000", 0.55),
            1, style.outline, style.shadow,
        )

    lines = [
        "[Script Info]",
        "; Сгенерировано TikTok Studio",
        "ScriptType: v4.00+",
        f"PlayResX: {config.WIDTH}",
        f"PlayResY: {config.HEIGHT}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        caption_style,
    ]
    if title and title.text.strip():
        lines.append(
            _style_line(
                "Title", title.font, title.size, ass_color(title.color),
                ass_color(title.box_color), ass_color(title.box_color), 3, 18, 0,
            )
        )
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    if title and title.text.strip():
        lines += _title_events(title, total)

    if enabled:
        for chunk in build_chunks(tokens, style.words_per_chunk, max_width, style, total):
            lines += _chunk_events(chunk, style, max_width, y)
    return "\n".join(lines) + "\n"


def _chunk_events(chunk: Chunk, style: SubtitleStyle, max_width: float, y: int) -> list[str]:
    words = [ass_escape(_display(t.text, style)) for t in chunk.tokens]
    rows, scale = layout_lines(words, style.font, style.size, max_width)
    base_scale = int(round(scale * 100))
    hl = ass_tag_color(style.highlight)
    normal = ass_tag_color(style.color)
    events = []

    def render(active: int | None, first: bool) -> str:
        head = f"{{\\pos({config.WIDTH // 2},{y})"
        if base_scale != 100:
            head += f"\\fscx{base_scale}\\fscy{base_scale}"
        if first and style.animation == "pop":
            s0 = int(base_scale * 0.82)
            head += f"\\fscx{s0}\\fscy{s0}\\t(0,110,\\fscx{base_scale}\\fscy{base_scale})"
        head += "}"
        parts = []
        for row in rows:
            row_parts = []
            for i in row:
                w = words[i]
                if active == i and style.highlight_mode != "none":
                    tag = f"\\1c{hl}"
                    reset = f"\\1c{normal}"
                    if style.highlight_mode == "scale":
                        big = int(base_scale * 1.12)
                        tag += f"\\fscx{big}\\fscy{big}"
                        reset += f"\\fscx{base_scale}\\fscy{base_scale}"
                    row_parts.append(f"{{{tag}}}{w}{{{reset}}}")
                else:
                    row_parts.append(w)
            parts.append(" ".join(row_parts))
        return head + "\\N".join(parts)

    if style.highlight_mode == "none":
        events.append(_dialogue(chunk.start, chunk.end, "Caption", render(None, True)))
        return events

    n = len(chunk.tokens)
    for i, tok in enumerate(chunk.tokens):
        start = chunk.start if i == 0 else (tok.start or chunk.start)
        end = chunk.end if i == n - 1 else (chunk.tokens[i + 1].start or chunk.end)
        start, end = _q(start), _q(end)
        if end <= start:
            continue
        events.append(_dialogue(start, end, "Caption", render(i, i == 0)))
    return events


def _title_events(title: TitleStyle, total: float) -> list[str]:
    words = [ass_escape(w) for w in title.text.split()]
    max_width = config.WIDTH - 2 * SAFE_MARGIN - 40
    rows, scale = layout_lines(words, title.font, title.size, max_width, max_lines=3)
    text ="\\N".join(" ".join(words[i] for i in row) for row in rows)
    end = total if not title.duration or title.duration <= 0 else min(total, title.duration)
    y = int(config.HEIGHT * max(5.0, min(95.0, title.position)) / 100)
    sc = int(round(scale * 100))
    head = f"{{\\pos({config.WIDTH // 2},{y})\\fscx{sc}\\fscy{sc}\\fad(250,250)}}"
    return [_dialogue(0, end, "Title", head + text, layer=1)]


def build_srt(tokens: list[Token], style: SubtitleStyle, total: float) -> str:
    """Те же фразы в формате SRT — пригодится для YouTube Shorts, Reels и т.п."""
    max_width = config.WIDTH - 2 * SAFE_MARGIN
    out = []
    for i, chunk in enumerate(build_chunks(tokens, style.words_per_chunk, max_width, style, total), 1):
        text = " ".join(t.text for t in chunk.tokens)
        out.append(f"{i}\n{_srt_time(chunk.start)} --> {_srt_time(chunk.end)}\n{text}\n")
    return "\n".join(out)


def _srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _dialogue(start: float, end: float, style: str, text: str, layer: int = 0) -> str:
    return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}"
