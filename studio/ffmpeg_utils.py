"""Поиск и запуск ffmpeg, разбор длительности и прогресса."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from pathlib import Path
from typing import Callable

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class FFmpegError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Ищет ffmpeg: переменная TIKTOK_STUDIO_FFMPEG, PATH, затем пакет imageio-ffmpeg."""
    env = os.environ.get("TIKTOK_STUDIO_FFMPEG")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_exe() -> str:
    exe = find_ffmpeg()
    if not exe:
        raise FFmpegError(
            "ffmpeg не найден. Установите его (Windows: winget install Gyan.FFmpeg, "
            "macOS: brew install ffmpeg, Linux: sudo apt install ffmpeg) "
            "или выполните pip install imageio-ffmpeg."
        )
    return exe


REQUIRED_FILTERS = ("ass", "xfade", "zoompan", "loudnorm", "boxblur")


@lru_cache(maxsize=1)
def capabilities() -> dict:
    exe = find_ffmpeg()
    info = {"found": bool(exe), "path": exe, "version": None, "filters": {}, "libx264": False, "ok": False}
    if not exe:
        return info
    try:
        ver = _run_simple([exe, "-hide_banner", "-version"])
        m = re.search(r"ffmpeg version (\S+)", ver)
        info["version"] = m.group(1) if m else "?"
        filters = _run_simple([exe, "-hide_banner", "-filters"])
        names = set(re.findall(r"^\s*[A-Z.|]{2,3}\s+(\w+)\s", filters, flags=re.M))
        info["filters"] = {f: f in names for f in REQUIRED_FILTERS}
        encoders = _run_simple([exe, "-hide_banner", "-encoders"])
        info["libx264"] = "libx264" in encoders
    except Exception as exc:  # pragma: no cover - зависит от системы
        info["error"] = str(exc)
        return info
    info["ok"] = all(info["filters"].values()) and info["libx264"]
    return info


def _run_simple(cmd: list[str]) -> str:
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )
    return (res.stdout or "") + (res.stderr or "")


def probe(path: str | Path) -> dict:
    """Возвращает длительность, размеры и наличие звука/видео у медиафайла."""
    out = _run_simple([ffmpeg_exe(), "-hide_banner", "-i", str(path)])
    info = {"duration": None, "width": None, "height": None, "has_video": False, "has_audio": False}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    for line in out.splitlines():
        if "Stream #" not in line:
            continue
        if "Video:" in line and not info["has_video"] and "attached pic" not in line:
            info["has_video"] = True
            sm = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            if sm:
                info["width"], info["height"] = int(sm.group(1)), int(sm.group(2))
        elif "Audio:" in line:
            info["has_audio"] = True
    # Учитываем поворот видео с телефона (rotate: 90 / displaymatrix).
    rot = re.search(r"rotat(?:e|ion of)\s*:?\s*(-?\d+(?:\.\d+)?)", out)
    if rot and info["width"] and abs(round(float(rot.group(1)))) % 180 == 90:
        info["width"], info["height"] = info["height"], info["width"]
    return info


def media_duration(path: str | Path) -> float:
    """Точная длительность: полный проход декодером (быстро для аудио)."""
    out = _run_simple([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-f", "null", "-"])
    # В конце вывода ffmpeg пишет "time=00:00:12.34" — но с -nostats только в финальной строке.
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if times:
        h, mi, s = times[-1]
        return int(h) * 3600 + int(mi) * 60 + float(s)
    dur = probe(path)["duration"]
    if dur is None:
        raise FFmpegError(f"Не удалось определить длительность файла {path}")
    return dur


def detect_speech_spans(path: str | Path, total: float, noise_db: int = -38, min_silence: float = 0.25) -> list[tuple[float, float]]:
    """Находит участки речи в аудио (всё, что не тишина)."""
    out = _run_simple(
        [
            ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-",
        ]
    )
    silences: list[tuple[float, float]] = []
    start = None
    for line in out.splitlines():
        ms = re.search(r"silence_start:\s*(-?\d+(?:\.\d+)?)", line)
        if ms:
            start = max(0.0, float(ms.group(1)))
            continue
        me = re.search(r"silence_end:\s*(\d+(?:\.\d+)?)", line)
        if me and start is not None:
            silences.append((start, float(me.group(1))))
            start = None
    if start is not None:
        silences.append((start, total))
    spans = []
    pos = 0.0
    for s, e in silences:
        if s > pos:
            spans.append((pos, s))
        pos = max(pos, e)
    if pos < total:
        spans.append((pos, total))
    return spans or [(0.0, total)]


def prepare_voice(src: str | Path, dst: str | Path, total: float, delay: float, target_lufs: float = -14.0) -> None:
    """Готовит дорожку голоса: двухпроходная нормализация громкости, пауза в начале,
    тишина до полной длины ролика. Результат — WAV 48 кГц стерео.

    Делается отдельным проходом: внутри общего графа loudnorm вместе с amix
    в некоторых версиях ffmpeg обрывает звук."""
    base = f"I={target_lufs}:TP=-1.5:LRA=11"
    out = _run_simple(
        [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(src),
         "-af", f"loudnorm={base}:print_format=json", "-f", "null", "-"]
    )
    norm = f"loudnorm={base}"
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, flags=re.S)
    if m:
        try:
            stats = json.loads(m.group(0))
            if float(stats["input_i"]) > -70:  # не тишина
                norm += (
                    f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
                    f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
                    f":offset={stats['target_offset']}:linear=true"
                )
        except (ValueError, KeyError):
            pass
    ms = int(round(delay * 1000))
    run_ffmpeg(
        [
            "-i", str(src),
            "-af", f"aformat=channel_layouts=stereo,{norm},aresample=48000,adelay={ms}|{ms},"
                   f"apad=whole_dur={total:.3f},atrim=0:{total:.3f}",
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(dst),
        ]
    )


def run_ffmpeg(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    duration: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Запускает ffmpeg и сообщает прогресс (0..1) по ключу out_time_us."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-nostats", "-loglevel", "error", "-progress", "pipe:1", *args]
    if log:
        log("$ ffmpeg " + " ".join(_quote(a) for a in cmd[1:]))
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )
    err_lines: list[str] = []

    def read_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            err_lines.append(line.rstrip())

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel_event is not None and cancel_event.is_set():
            proc.kill()
            break
        key, _, value = line.strip().partition("=")
        if key in ("out_time_us", "out_time_ms") and duration and on_progress:
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                continue
            on_progress(max(0.0, min(1.0, seconds / duration)))
    proc.wait()
    t.join(timeout=5)
    if cancel_event is not None and cancel_event.is_set():
        raise FFmpegError("Отменено пользователем")
    if proc.returncode != 0:
        tail = "\n".join(err_lines[-25:])
        if log:
            log(tail)
        raise FFmpegError(f"ffmpeg завершился с ошибкой (код {proc.returncode}):\n{tail}")
    if on_progress:
        on_progress(1.0)


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or ";" in arg) else arg
