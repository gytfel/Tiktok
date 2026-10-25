"""Озвучка текста.

Основной движок — нейроголоса Microsoft Edge (пакет edge-tts): бесплатно, без ключей,
отдаёт тайминг каждого слова для точных субтитров. Запасной движок — системные голоса
(Windows SAPI и т.п.) через pyttsx3, работает без интернета.
"""

from __future__ import annotations

import asyncio
import json
import re
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


# edge-tts всегда отдаёт MP3 24 кГц 48 кбит/с CBR: длительность считается по размеру.
MP3_BYTES_PER_SECOND = 48_000 / 8
CHUNK_CHARS = 600
# Языки голосов, которые читают кириллицу. Остальным (кроме Multilingual) русский
# текст не по силам — сервис в ответ молча не присылает звук.
CYRILLIC_LANGS = {"ru", "uk", "be", "bg", "sr", "mk", "kk", "ky", "mn", "tg", "tt", "ba"}


def voice_language_problem(text: str, voice: str) -> str | None:
    """Сообщение, если выбранный голос не сможет прочитать текст на этом языке."""
    if not voice or voice.startswith("system:") or "multilingual" in voice.lower():
        return None
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    cyrillic = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    lang = voice.split("-")[0].lower()
    if cyrillic / len(letters) > 0.3 and lang not in CYRILLIC_LANGS:
        return (
            f"Голос «{voice}» не умеет читать русский текст — Microsoft в таком случае не присылает звук. "
            "На вкладке «Голос» выберите русский голос или голос с пометкой Multilingual."
        )
    return None


def split_for_tts(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Делит текст на части до max_chars символов по абзацам и предложениям.
    Короткие абзацы объединяются, части без слов отбрасываются."""
    pieces: list[str] = []
    for para in text.split("\n\n"):
        para = " ".join(para.split())
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?…])\s+", para):
            while len(sentence) > max_chars:
                cut = sentence.rfind(", ", 0, max_chars) + 1
                if cut < max_chars // 2:
                    cut = sentence.rfind(" ", 0, max_chars)
                if cut <= 0:
                    cut = max_chars
                pieces.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            pieces.append(sentence)
    chunks: list[str] = []
    for piece in pieces:
        if not re.search(r"\w", piece):
            continue
        if chunks and len(chunks[-1]) + 1 + len(piece) <= max_chars:
            chunks[-1] = f"{chunks[-1]}\n{piece}"
        else:
            chunks.append(piece)
    return chunks


async def _edge_stream(text: str, voice: str, rate: int, pitch: int, volume: int) -> tuple[bytes, list[Boundary]]:
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
    audio = bytearray()
    boundaries: list[Boundary] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            start = chunk["offset"] / TICKS_PER_SECOND
            end = (chunk["offset"] + chunk["duration"]) / TICKS_PER_SECOND
            boundaries.append(Boundary(chunk["text"], start, end))
    return bytes(audio), boundaries


def _is_no_audio(exc: BaseException) -> bool:
    return type(exc).__name__ == "NoAudioReceived"


def _edge_chunk(text: str, voice: str, rate: int, pitch: int, volume: int, attempts: int) -> tuple[bytes, list[Boundary]]:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            data, boundaries = asyncio.run(_edge_stream(text, voice, rate, pitch, volume))
            if data:
                return data, boundaries
            last_exc = TTSError("пустой ответ")
        except ValueError as exc:  # неверное имя голоса или параметры — повтор не поможет
            raise TTSError(f"Неверные параметры озвучки: {exc}") from exc
        except Exception as exc:  # сбои сервиса и сети: пробуем ещё раз с паузой
            last_exc = exc
        if attempt < attempts - 1:
            time.sleep(min(8.0, 1.5 * 2 ** attempt))
    phrase = text if len(text) <= 80 else text[:77] + "…"
    if last_exc is not None and (_is_no_audio(last_exc) or isinstance(last_exc, TTSError)):
        raise TTSError(
            f"Microsoft не прислал звук для фразы «{phrase}» (голос {voice}), хотя связь есть. Обычно это "
            "временный сбой сервиса, иногда — слово или символ, который голос не может произнести. "
            "Попробуйте нажать «Создать видео» ещё раз через минуту, выбрать другой голос "
            "или убрать из этой фразы необычные символы."
        )
    raise TTSError(
        "Не удалось связаться с сервисом озвучки Microsoft. Проверьте интернет "
        f"(или укажите прокси в настройках). Ошибка: {type(last_exc).__name__}: {last_exc}"
    )


def edge_synthesize(text: str, voice: str, rate: int, pitch: int, volume: int, out_path: Path, attempts: int = 5) -> TTSResult:
    """Озвучивает текст по частям: сбой сервиса задевает одну короткую часть, и её
    можно повторить, не переозвучивая всё остальное."""
    problem = voice_language_problem(text, voice)
    if problem:
        raise TTSError(problem)
    chunks = split_for_tts(text)
    if not chunks:
        raise TTSError("В тексте нет слов для озвучки")
    audio = bytearray()
    boundaries: list[Boundary] = []
    offset = 0.0
    for chunk in chunks:
        data, chunk_boundaries = _edge_chunk(chunk, voice, rate, pitch, volume, attempts)
        boundaries += [Boundary(b.text, b.start + offset, b.end + offset) for b in chunk_boundaries]
        audio += data
        offset += len(data) / MP3_BYTES_PER_SECOND
    out_path.write_bytes(bytes(audio))
    return TTSResult(out_path, boundaries)


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
