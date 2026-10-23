"""Фон ролика: анимированный градиент или нарезка из картинок/видео.

Модуль строит входы и filter_complex для ffmpeg; сам рендер — в pipeline.py.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import config
from .text_utils import Token, sentence_start_indices

W, H, FPS = config.WIDTH, config.HEIGHT, config.FPS
MAX_SEGMENTS = 24
MIN_SEGMENT = 1.2

GRADIENT_PRESETS = {
    "sunset": ["#ff512f", "#dd2476", "#5f0a87"],
    "ocean": ["#0f2027", "#2c5364", "#00b4db"],
    "neon": ["#12c2e9", "#c471ed", "#f64f59"],
    "forest": ["#0b3d2e", "#1d976c", "#2b5876"],
    "night": ["#0f0c29", "#302b63", "#24243e"],
    "peach": ["#ff9966", "#de6262", "#6a3093"],
    "mono": ["#101010", "#2c2c2c", "#1a1a2e"],
}


@dataclass
class MediaItem:
    path: Path
    kind: str  # image | video
    duration: float | None = None  # для видео


@dataclass
class Segment:
    item: MediaItem
    start: float
    end: float
    offset: float = 0.0  # с какого места видео начинать
    variant: int = 0  # направление движения камеры для картинок

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class BackgroundGraph:
    inputs: list[str] = field(default_factory=list)  # аргументы ffmpeg с -i
    filters: list[str] = field(default_factory=list)
    output: str = ""
    input_count: int = 0


# ------------------------------------------------------------- градиент


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 40, 40, 60


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def make_gradient_image(colors: list[str], path: Path, seed: int = 7) -> Path:
    """Рисует бесшовную по вертикали «мягкую» градиентную текстуру 1080x3840.
    При прокрутке сверху вниз она выглядит как плавно движущийся фон."""
    cols = [_hex_to_rgb(c) for c in (colors or GRADIENT_PRESETS["night"])][:5]
    if len(cols) == 1:
        cols = cols * 2
    w, h = W // 2, H // 2  # рисуем в половинном разрешении — градиент гладкий
    canvas = Image.new("RGB", (w, h * 3))
    draw = ImageDraw.Draw(canvas)
    n = len(cols)
    for y in range(h * 3):
        pos = (y % h) / h * n
        i = int(pos) % n
        draw.line([(0, y), (w, y)], fill=_mix(cols[i], cols[(i + 1) % n], pos - int(pos)))

    rng = random.Random(seed)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for k in range(7):
        cx = rng.uniform(0, w)
        cy = rng.uniform(0, h)
        r = rng.uniform(0.25, 0.55) * w
        base = cols[k % n]
        tint = _mix(base, (255, 255, 255), 0.25) if k % 2 == 0 else _mix(base, (0, 0, 0), 0.3)
        for rep in range(3):
            y0 = cy + rep * h
            ld.ellipse([cx - r, y0 - r, cx + r, y0 + r], fill=(*tint, 150))
    layer = layer.filter(ImageFilter.GaussianBlur(radius=90))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")
    tile = canvas.crop((0, h, w, 2 * h)).resize((W, H), Image.LANCZOS)
    # Лёгкое зерно против «полос» на градиенте после сжатия видео.
    grain = Image.effect_noise((W, H), 24).convert("RGB")
    tile = Image.blend(tile, grain, 0.035)
    full = Image.new("RGB", (W, H * 2))
    full.paste(tile, (0, 0))
    full.paste(tile, (0, H))
    path.parent.mkdir(parents=True, exist_ok=True)
    full.save(path)
    return path


def _still(length: float) -> str:
    """Повторяет один кадр нужное время (картинка декодируется один раз)."""
    return f"loop=loop=-1:size=1:start=0,settb=1/{FPS},setpts=N,trim=duration={length:.3f}"


def gradient_graph(image: Path, total: float, speed: float = 30.0) -> BackgroundGraph:
    g = BackgroundGraph()
    g.inputs = ["-i", str(image)]
    g.input_count = 1
    g.filters.append(
        f"[0:v]format=rgb24,{_still(total)},crop={W}:{H}:0:'mod(t*{speed:.1f},{H})',"
        f"format=yuv420p,setsar=1[bg]"
    )
    g.output = "bg"
    return g


# --------------------------------------------------------- нарезка медиа


def plan_segments(
    items: list[MediaItem],
    tokens: list[Token],
    total: float,
    mode: str = "auto",
    max_segment: float = 6.0,
) -> list[Segment]:
    """Распределяет медиа по времени ролика. Смены кадра по возможности
    совпадают с началом предложений (или абзацев в режиме paragraphs)."""
    if not items:
        raise ValueError("Не выбрано ни одного файла для фона")
    n = len(items)
    cuts: list[float] = []

    if mode == "paragraphs":
        seen = set()
        for tok in tokens:
            if tok.paragraph not in seen:
                seen.add(tok.paragraph)
                if tok.paragraph > 0 and tok.start is not None:
                    cuts.append(max(0.0, tok.start - 0.08))
    if mode != "paragraphs" or not cuts:
        if max_segment and max_segment > 0:
            count = max(n, math.ceil(total / max_segment))
        else:
            count = n
        if n == 1 and items[0].kind == "video":
            count = 1
        count = max(1, min(MAX_SEGMENTS, count, int(total // MIN_SEGMENT) or 1))
        sentence_times = [tokens[i].start for i in sentence_start_indices(tokens)[1:] if tokens[i].start]
        step = total / count
        cuts = []
        for k in range(1, count):
            target = k * step
            best = min(sentence_times, key=lambda s: abs(s - target), default=None)
            cut = target
            if best is not None and abs(best - target) <= 0.35 * step:
                cut = max(0.0, best - 0.08)
            cuts.append(cut)

    # Убираем слишком короткие и неупорядоченные куски.
    bounds = [0.0]
    for c in sorted(cuts):
        if c - bounds[-1] >= MIN_SEGMENT and total - c >= MIN_SEGMENT:
            bounds.append(c)
    bounds.append(total)

    segments: list[Segment] = []
    used: dict[int, float] = {}
    for k in range(len(bounds) - 1):
        idx = k % n
        item = items[idx]
        seg = Segment(item=item, start=bounds[k], end=bounds[k + 1], variant=k % 4)
        if item.kind == "video":
            offset = used.get(idx, 0.0)
            if item.duration and item.duration > 0.5:
                offset %= item.duration
            seg.offset = offset
            used[idx] = used.get(idx, 0.0) + seg.length
        segments.append(seg)
    return segments


def plan_scene_segments(items: list[MediaItem], start_times: list[float], total: float) -> list[Segment]:
    """Один файл на сцену: каждый кадр начинается вместе со своей сценой озвучки.
    Сцены короче MIN_SEGMENT не получают отдельного кадра — остаётся предыдущий."""
    if not items:
        raise ValueError("Нет кадров для сцен")
    bounds: list[tuple[float, MediaItem]] = []
    for t, item in zip(start_times, items):
        t = 0.0 if not bounds else max(0.0, t - 0.08)
        if bounds and (t - bounds[-1][0] < MIN_SEGMENT or total - t < MIN_SEGMENT):
            continue
        bounds.append((t, item))
    segments = []
    for k, (t, item) in enumerate(bounds):
        end = bounds[k + 1][0] if k + 1 < len(bounds) else total
        segments.append(Segment(item=item, start=t, end=end, variant=k % 4))
    return segments


def _fit_chain(label_in: str, label_out: str, fit: str, w: int, h: int, uid: str) -> list[str]:
    """Вписывает кадр в w x h: cover — обрезка по краям, blur — целиком на размытом фоне."""
    if fit == "blur":
        return [
            f"[{label_in}]split=2[fa{uid}][fb{uid}]",
            f"[fa{uid}]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,"
            f"crop={w // 4}:{h // 4},boxblur=12:3,scale={w}:{h},eq=brightness=-0.06[fbg{uid}]",
            f"[fb{uid}]scale={w}:{h}:force_original_aspect_ratio=decrease[ffg{uid}]",
            f"[fbg{uid}][ffg{uid}]overlay=(W-w)/2:(H-h)/2,setsar=1[{label_out}]",
        ]
    return [f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[{label_out}]"]


_ZOOM = 0.15


def _zoompan(variant: int, frames: int) -> str:
    f = max(1, frames)
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    if variant == 0:  # наезд
        z, x, y = f"1+{_ZOOM}*on/{f}", center_x, center_y
    elif variant == 1:  # отъезд
        z, x, y = f"{1 + _ZOOM}-{_ZOOM}*on/{f}", center_x, center_y
    elif variant == 2:  # панорама слева направо
        z, x, y = f"{1 + _ZOOM}", f"(iw-iw/zoom)*on/{f}", center_y
    else:  # панорама сверху вниз
        z, x, y = f"{1 + _ZOOM}", center_x, f"(ih-ih/zoom)*on/{f}"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={f}:s={W}x{H}:fps={FPS}"


def media_graph(segments: list[Segment], fit: str = "cover", motion: bool = True, transition: float = 0.3) -> BackgroundGraph:
    g = BackgroundGraph()
    n = len(segments)
    transition = max(0.0, min(1.0, transition)) if n > 1 else 0.0
    if transition and min(s.length for s in segments) <= transition + 0.2:
        transition = 0.0
    labels = []
    for i, seg in enumerate(segments):
        length = seg.length + (transition if i < n - 1 else 0.0)
        frames = int(math.ceil(length * FPS)) + 1
        if seg.item.kind == "image":
            g.inputs += ["-i", str(seg.item.path)]
        else:
            g.inputs += [
                "-threads", "2",
                "-ss", f"{seg.offset:.3f}",
                "-stream_loop", "-1",
                "-t", f"{length:.3f}",
                "-i", str(seg.item.path),
            ]
        uid = str(i)
        if seg.item.kind == "image" and motion:
            # Картинку вписываем с запасом и двигаем «камеру» эффектом Кена Бёрнса.
            sw, sh = int(W * 1.25) // 2 * 2, int(H * 1.25) // 2 * 2
            g.filters += _fit_chain(f"{i}:v", f"fit{uid}", fit, sw, sh, uid)
            g.filters.append(f"[fit{uid}]{_zoompan(seg.variant, frames)},setsar=1[mot{uid}]")
            src = f"mot{uid}"
        elif seg.item.kind == "image":
            g.filters += _fit_chain(f"{i}:v", f"fit{uid}", fit, W, H, uid)
            g.filters.append(f"[fit{uid}]{_still(length)}[mot{uid}]")
            src = f"mot{uid}"
        else:
            g.filters += _fit_chain(f"{i}:v", f"fit{uid}", fit, W, H, uid)
            src = f"fit{uid}"
        g.filters.append(
            f"[{src}]fps={FPS},format=yuv420p,setsar=1,trim=duration={length:.3f},setpts=PTS-STARTPTS[s{uid}]"
        )
        labels.append(f"s{uid}")
    g.input_count = n

    if n == 1:
        g.output = labels[0]
        return g
    if transition > 0:
        prev = labels[0]
        for k in range(1, n):
            out = f"x{k}"
            offset = segments[k].start
            g.filters.append(f"[{prev}][{labels[k]}]xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}[{out}]")
            prev = out
        g.output = prev
    else:
        g.filters.append("".join(f"[{l}]" for l in labels) + f"concat=n={n}:v=1:a=0[bgcat]")
        g.output = "bgcat"
    return g
