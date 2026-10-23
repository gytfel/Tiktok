"""Параметры ролика: значения по умолчанию и проверка входных данных."""

from __future__ import annotations

import copy
import re

from . import config
from .backgrounds import GRADIENT_PRESETS

DEFAULT_PROJECT: dict = {
    "text": "",
    "title": {"text": "", "duration": 0, "size": 66, "color": "#000000", "box_color": "#FFFFFF", "position": 15},
    "voice": {"voice": "ru-RU-DmitryNeural", "rate": 5, "pitch": 0, "volume": 0},
    "subtitles": {
        "enabled": True,
        "font": config.DEFAULT_FONT,
        "size": 90,
        "color": "#FFFFFF",
        "highlight": "#FFE600",
        "outline_color": "#000000",
        "outline": 6,
        "shadow": 3,
        "style": "outline",
        "box_color": "#000000",
        "box_opacity": 0.6,
        "position": 62,
        "words_per_chunk": 3,
        "uppercase": True,
        "highlight_mode": "color",
        "animation": "pop",
    },
    "background": {
        "type": "gradient",  # gradient | media | pexels
        "gradient": list(GRADIENT_PRESETS["night"]),
        "gradient_speed": 30,
        "media": [],  # id загруженных файлов по порядку
        "pexels_query": "",
        "fit": "cover",  # cover | blur
        "motion": True,
        "dim": 0.25,
        "switch": "auto",  # auto | paragraphs
        "max_segment": 6,
        "transition": 0.3,
    },
    "music": {"file": None, "volume": 0.15},
    "export": {"quality": "balanced", "name": ""},  # fast | balanced | high
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _num(value, default, lo, hi, cast=float):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _color(value, default):
    if isinstance(value, str) and _HEX_RE.match(value.strip()):
        v = value.strip()
        return v if v.startswith("#") else "#" + v
    return default


def _choice(value, options, default):
    return value if value in options else default


def _merge(base: dict, data: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (data or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def normalize(data: dict) -> dict:
    """Дополняет параметры значениями по умолчанию и приводит к допустимым диапазонам."""
    p = _merge(DEFAULT_PROJECT, data if isinstance(data, dict) else {})
    d = DEFAULT_PROJECT
    p["text"] = str(p.get("text") or "")[:20000]

    t = p["title"]
    t["text"] = str(t.get("text") or "")[:200]
    t["duration"] = _num(t.get("duration"), 0, 0, 600)
    t["size"] = _num(t.get("size"), 66, 30, 140, int)
    t["color"] = _color(t.get("color"), d["title"]["color"])
    t["box_color"] = _color(t.get("box_color"), d["title"]["box_color"])
    t["position"] = _num(t.get("position"), 15, 5, 95)

    v = p["voice"]
    v["voice"] = str(v.get("voice") or d["voice"]["voice"])[:200]
    v["rate"] = _num(v.get("rate"), 0, -50, 100, int)
    v["pitch"] = _num(v.get("pitch"), 0, -50, 50, int)
    v["volume"] = _num(v.get("volume"), 0, -50, 50, int)

    s = p["subtitles"]
    ds = d["subtitles"]
    s["enabled"] = bool(s.get("enabled"))
    s["font"] = _choice(s.get("font"), config.FONTS, config.DEFAULT_FONT)
    s["size"] = _num(s.get("size"), ds["size"], 30, 200, int)
    for key in ("color", "highlight", "outline_color", "box_color"):
        s[key] = _color(s.get(key), ds[key])
    s["outline"] = _num(s.get("outline"), ds["outline"], 0, 20)
    s["shadow"] = _num(s.get("shadow"), ds["shadow"], 0, 20)
    s["style"] = _choice(s.get("style"), ("outline", "box"), "outline")
    s["box_opacity"] = _num(s.get("box_opacity"), ds["box_opacity"], 0, 1)
    s["position"] = _num(s.get("position"), ds["position"], 5, 95)
    s["words_per_chunk"] = _num(s.get("words_per_chunk"), 3, 1, 8, int)
    s["uppercase"] = bool(s.get("uppercase"))
    s["highlight_mode"] = _choice(s.get("highlight_mode"), ("color", "scale", "none"), "color")
    s["animation"] = _choice(s.get("animation"), ("pop", "none"), "pop")
    # Заголовок берёт шрифт субтитров.
    t["font"] = s["font"]

    b = p["background"]
    db = d["background"]
    b["type"] = _choice(b.get("type"), ("gradient", "media", "pexels"), "gradient")
    grad = b.get("gradient")
    if isinstance(grad, str) and grad in GRADIENT_PRESETS:
        grad = GRADIENT_PRESETS[grad]
    if not isinstance(grad, list):
        grad = db["gradient"]
    b["gradient"] = [_color(c, "#202040") for c in grad[:5]] or list(db["gradient"])
    b["gradient_speed"] = _num(b.get("gradient_speed"), 30, 0, 200)
    b["media"] = [str(x) for x in (b.get("media") or []) if isinstance(x, str)][:50]
    b["pexels_query"] = str(b.get("pexels_query") or "")[:500]
    b["fit"] = _choice(b.get("fit"), ("cover", "blur"), "cover")
    b["motion"] = bool(b.get("motion"))
    b["dim"] = _num(b.get("dim"), db["dim"], 0, 0.85)
    b["switch"] = _choice(b.get("switch"), ("auto", "paragraphs"), "auto")
    b["max_segment"] = _num(b.get("max_segment"), 6, 0, 60)
    b["transition"] = _num(b.get("transition"), 0.3, 0, 1)

    m = p["music"]
    m["file"] = str(m["file"]) if m.get("file") else None
    m["volume"] = _num(m.get("volume"), 0.15, 0, 1)

    e = p["export"]
    e["quality"] = _choice(e.get("quality"), ("fast", "balanced", "high"), "balanced")
    e["name"] = str(e.get("name") or "")[:80]
    return p
