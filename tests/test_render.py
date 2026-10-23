"""Сквозная проверка рендера (нужен ffmpeg). Вместо голоса — тон и равномерные метки слов."""

import re
import subprocess
from pathlib import Path

import pytest

from studio import config, library
from studio.ffmpeg_utils import capabilities, ffmpeg_exe, probe
from studio.pipeline import RenderContext, render
from tests.fake_tts import fake_synthesize

pytestmark = pytest.mark.skipif(not capabilities()["ok"], reason="нужен ffmpeg с libass и libx264")

TEXT = "Привет! Это проверка студии.\n\nВторой абзац с субтитрами и фоном."


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    for name in ("WORKSPACE_DIR", "UPLOADS_DIR", "OUTPUTS_DIR", "JOBS_DIR", "CACHE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    config.ensure_dirs()
    return tmp_path


def _ffmpeg(*args):
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", *args], check=True)


def _loudness(path: Path) -> float:
    out = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    return float(re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", out)[-1])


def _upload(path: Path) -> str:
    with open(path, "rb") as f:
        return library.save_upload(path.name, f)["id"]


def _check_output(result: dict, min_duration: float):
    path = config.OUTPUTS_DIR / f"{result['id']}.mp4"
    info = probe(path)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["has_audio"]
    assert info["duration"] >= min_duration
    assert (config.OUTPUTS_DIR / f"{result['id']}.srt").exists()
    return path


def test_render_gradient(workspace):
    from PIL import Image

    result = render(
        {
            "text": TEXT,
            "title": {"text": "Заголовок"},
            "background": {"type": "gradient", "gradient": ["#ff512f", "#dd2476", "#5f0a87"], "gradient_speed": 0, "dim": 0.3},
            "export": {"quality": "fast"},
        },
        RenderContext(workspace / "job1", on_log=lambda m: None),
        synthesize=fake_synthesize,
    )
    path = _check_output(result, 3)
    assert abs(_loudness(path) + 14) < 1.5

    # Затемнение не должно обесцвечивать фон: пиксель кадра ≈ пиксель градиента × 0.7.
    frame = workspace / "frame.png"
    _ffmpeg("-i", str(path), "-frames:v", "1", "-update", "1", str(frame))
    got = Image.open(frame).convert("RGB").getpixel((30, 960))
    src = Image.open(workspace / "job1" / "gradient.png").convert("RGB").getpixel((30, 960))
    expected = [c * 0.7 for c in src]
    assert all(abs(g - e) < 14 for g, e in zip(got, expected)), (got, expected)


def test_render_media_with_music(workspace):
    media = workspace / "src"
    media.mkdir()
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3", "-pix_fmt", "yuv420p", str(media / "clip.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "color=c=orange:size=800x600", "-frames:v", "1", str(media / "photo.jpg"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(media / "music.mp3"))
    ids = [_upload(media / "clip.mp4"), _upload(media / "photo.jpg")]
    music = _upload(media / "music.mp3")
    result = render(
        {
            "text": TEXT,
            "background": {"type": "media", "media": ids, "fit": "blur", "max_segment": 2},
            "music": {"file": music, "volume": 0.3},
            "subtitles": {"style": "box", "highlight_mode": "scale"},
            "export": {"quality": "fast"},
        },
        RenderContext(workspace / "job2", on_log=lambda m: None),
        synthesize=fake_synthesize,
    )
    path = _check_output(result, 3)
    # Регрессия: при смешивании с музыкой звук не должен пропадать.
    assert _loudness(path) > -20


def test_render_without_word_boundaries(workspace):
    def no_boundaries(*args):
        return fake_synthesize(*args, with_boundaries=False)

    result = render(
        {"text": TEXT, "export": {"quality": "fast"}},
        RenderContext(workspace / "job3", on_log=lambda m: None),
        synthesize=no_boundaries,
    )
    _check_output(result, 3)
