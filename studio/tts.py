"""Озвучка текста.

Основной движок — нейроголоса Microsoft Edge (пакет edge-tts): бесплатно, без ключей,
отдаёт тайминг каждого слова для точных субтитров. Запасной движок — системные голоса
(Windows SAPI и т.п.) через pyttsx3, работает без интернета.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .timing import Boundary

TICKS_PER_SECOND = 10_000_000

# Список на случай, если каталог голосов не загрузился. Multilingual-голоса
# умеют читать и русский текст.
FALLBACK_VOICES = [
    {"id": "ru-RU-DmitryNeural", "name": "Дмитрий", "locale": "ru-RU", "gender": "Male"},
    {"id": "ru-RU-SvetlanaNeural", "name": "Светлана", "locale": "ru-RU", "gender": "Female"},
    {"id": "uk-UA-OstapNeural", "name": "Остап", "locale": "uk-UA", "gender": "Male"},
    {"id": "uk-UA-PolinaNeural", "name": "Полина", "locale": "uk-UA", "gender": "Female"},
    {"id": "kk-KZ-DauletNeural", "name": "Даулет", "locale": "kk-KZ", "gender": "Male"},
    {"id": "kk-KZ-AigulNeural", "name": "Айгуль", "locale": "kk-KZ", "gender": "Female"},
    {"id": "en-US-AndrewMultilingualNeural", "name": "Andrew (Multilingual)", "locale": "en-US", "gender": "Male"},
    {"id": "en-US-BrianMultilingualNeural", "name": "Brian (Multilingual)", "locale": "en-US", "gender": "Male"},
    {"id": "en-US-AvaMultilingualNeural", "name": "Ava (Multilingual)", "locale": "en-US", "gender": "Female"},
    {"id": "en-US-EmmaMultilingualNeural", "name": "Emma (Multilingual)", "locale": "en-US", "gender": "Female"},
    {"id": "de-DE-SeraphinaMultilingualNeural", "name": "Seraphina (Multilingual)", "locale": "de-DE", "gender": "Female"},
    {"id": "fr-FR-RemyMultilingualNeural", "name": "Remy (Multilingual)", "locale": "fr-FR", "gender": "Male"},
    {"id": "en-US-GuyNeural", "name": "Guy", "locale": "en-US", "gender": "Male"},
    {"id": "en-US-ChristopherNeural", "name": "Christopher", "locale": "en-US", "gender": "Male"},
    {"id": "en-US-JennyNeural", "name": "Jenny", "locale": "en-US", "gender": "Female"},
    {"id": "en-US-AriaNeural", "name": "Aria", "locale": "en-US", "gender": "Female"},
]

DEFAULT_VOICE = "ru-RU-DmitryNeural"


class TTSError(RuntimeError):
    pass


@dataclass
class TTSResult:
    audio_path: Path
    boundaries: list[Boundary] = field(default_factory=list)


def _fmt_rate(percent: int) -> str:
    return f"{int(percent):+d}%"


def _fmt_pitch(hz: int) -> str:
    return f"{int(hz):+d}Hz"


# ---------------------------------------------------------------- Edge TTS


async def _edge_synthesize(text: str, voice: str, rate: int, pitch: int, volume: int, out_path: Path) -> list[Boundary]:
    import edge_tts

    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=_fmt_rate(rate),
        pitch=_fmt_pitch(pitch),
        volume=_fmt_rate(volume),
        boundary="WordBoundary",
        proxy=config.get_proxy(),
        connect_timeout=15,
        receive_timeout=90,
    )
    boundaries: list[Boundary] = []
    with open(out_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / TICKS_PER_SECOND
                end = (chunk["offset"] + chunk["duration"]) / TICKS_PER_SECOND
                boundaries.append(Boundary(chunk["text"], start, end))
    return boundaries


def edge_synthesize(text: str, voice: str, rate: int, pitch: int, volume: int, out_path: Path, attempts: int = 3) -> TTSResult:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            boundaries = asyncio.run(_edge_synthesize(text, voice, rate, pitch, volume, out_path))
            if out_path.stat().st_size == 0:
                raise TTSError("Сервис озвучки вернул пустой звук")
            return TTSResult(out_path, boundaries)
        except ValueError as exc:  # неверное имя голоса или параметры — повтор не поможет
            raise TTSError(f"Неверные параметры озвучки: {exc}") from exc
        except Exception as exc:  # сеть, временные ошибки сервиса
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise TTSError(
        "Не удалось получить озвучку от Microsoft Edge TTS. Проверьте интернет "
        f"(или укажите прокси в настройках). Ошибка: {type(last_exc).__name__}: {last_exc}"
    )


async def edge_list_voices() -> list[dict]:
    import edge_tts

    raw = await edge_tts.list_voices(proxy=config.get_proxy())
    voices = []
    for v in raw:
        friendly = v.get("FriendlyName") or v.get("ShortName")
        name = friendly.replace("Microsoft ", "").split(" Online")[0]
        voices.append(
            {
                "id": v["ShortName"],
                "name": name,
                "locale": v.get("Locale", ""),
                "gender": v.get("Gender", ""),
            }
        )
    return voices


# ------------------------------------------------- Системные голоса (офлайн)

_OFFLINE_SCRIPT = r"""
import json, sys
import pyttsx3
args = json.loads(sys.argv[1])
engine = pyttsx3.init()
if args["action"] == "list":
    out = []
    for v in engine.getProperty("voices"):
        langs = []
        for lang in (getattr(v, "languages", None) or []):
            langs.append(lang.decode("utf-8", "ignore") if isinstance(lang, bytes) else str(lang))
        out.append({"id": v.id, "name": v.name, "langs": langs})
    print(json.dumps(out, ensure_ascii=False))
else:
    if args.get("voice"):
        engine.setProperty("voice", args["voice"])
    base = engine.getProperty("rate") or 200
    engine.setProperty("rate", int(base * (1 + args.get("rate", 0) / 100.0)))
    engine.save_to_file(args["text"], args["out"])
    engine.runAndWait()
"""


def _offline_call(payload: dict, timeout: int = 600) -> str:
    try:
        res = subprocess.run(
            [sys.executable, "-c", _OFFLINE_SCRIPT, json.dumps(payload, ensure_ascii=False)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TTSError("Системный синтезатор речи не ответил") from exc
    if res.returncode != 0:
        raise TTSError("Системные голоса недоступны (нужен pyttsx3): " + (res.stderr or "").strip()[-400:])
    return res.stdout


def offline_list_voices() -> list[dict]:
    out = _offline_call({"action": "list"}, timeout=60)
    voices = []
    for v in json.loads(out.strip().splitlines()[-1] if out.strip() else "[]"):
        voices.append({"id": "system:" + v["id"], "name": v["name"] + " (офлайн)", "locale": ",".join(v["langs"]), "gender": ""})
    return voices


def offline_synthesize(text: str, voice: str, rate: int, out_path: Path) -> TTSResult:
    _offline_call({"action": "say", "text": text, "voice": voice, "rate": rate, "out": str(out_path)})
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise TTSError("Системный синтезатор не создал аудиофайл")
    return TTSResult(out_path, [])


# --------------------------------------------------------------- Общий API


def synthesize(text: str, voice: str, rate: int, pitch: int, volume: int, out_dir: Path) -> TTSResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    if voice.startswith("system:"):
        return offline_synthesize(text, voice[len("system:"):], rate, out_dir / "voice.wav")
    return edge_synthesize(text, voice or DEFAULT_VOICE, rate, pitch, volume, out_dir / "voice.mp3")


_VOICES_CACHE = config.CACHE_DIR / "voices.json"
_VOICES_TTL = 24 * 3600


def list_voices(force: bool = False) -> dict:
    """Возвращает {"voices": [...], "source": "online"|"cache"|"fallback", "offline": [...]}."""
    voices: list[dict] = []
    source = "fallback"
    if not force and _VOICES_CACHE.exists() and time.time() - _VOICES_CACHE.stat().st_mtime < _VOICES_TTL:
        try:
            voices = json.loads(_VOICES_CACHE.read_text(encoding="utf-8"))
            source = "cache"
        except ValueError:
            voices = []
    if not voices:
        try:
            voices = asyncio.run(asyncio.wait_for(edge_list_voices(), timeout=12))
            config.ensure_dirs()
            _VOICES_CACHE.write_text(json.dumps(voices, ensure_ascii=False), encoding="utf-8")
            source = "online"
        except Exception:
            voices = list(FALLBACK_VOICES)
            source = "fallback"
    return {"voices": voices, "source": source}


_offline_cache: list[dict] | None = None


def list_offline_voices() -> list[dict]:
    global _offline_cache
    if _offline_cache is None:
        try:
            _offline_cache = offline_list_voices()
        except Exception:
            _offline_cache = []
    return _offline_cache
