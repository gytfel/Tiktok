"""Фальшивый синтезатор для тестов: тон вместо голоса и равномерные метки слов."""

from __future__ import annotations

import re
from pathlib import Path

from studio.ffmpeg_utils import run_ffmpeg
from studio.timing import Boundary
from studio.tts import TTSResult


def fake_synthesize(text, voice, rate, pitch, volume, out_dir: Path, with_boundaries: bool = True, **kw) -> TTSResult:
    words = re.findall(r"\w[\w\-']*", text)
    per_word = 0.32
    boundaries = []
    t = 0.1
    for w in words:
        boundaries.append(Boundary(w, t, t + per_word * 0.8))
        t += per_word
    total = t + 0.2
    out = Path(out_dir) / "voice.mp3"
    run_ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=220:duration={total:.2f}", "-ac", "1", "-ar", "24000", str(out)])
    return TTSResult(out, boundaries if with_boundaries else [])
