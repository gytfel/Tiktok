"""Озвучка текста.

Основной движок — нейроголоса Microsoft Edge (пакет edge-tts): бесплатно, без ключей,
отдаёт тайминг каждого слова для точных субтитров. Запасные движки работают без интернета:
нейроголоса Piper (на сервере — там Microsoft часто не отдаёт озвучку адресам
дата-центров) и системные голоса Windows через pyttsx3.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
import wave
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
    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked  # Microsoft не отдаёт звук этому адресу — поможет голос Piper


@dataclass
class TTSResult:
    audio_path: Path
    boundaries: list[Boundary] = field(default_factory=list)
    note: str = ""  # что стоит сказать пользователю (например, что взят запасной голос)


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
    voice = voice.removeprefix("piper:").replace("_", "-")
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
    if last_exc is not None and (_is_no_audio(last_exc) or isinstance(last_exc, TTSError)):
        raise TTSError(
            f"Microsoft не прислал звук для фразы «{_short(text)}» (голос {voice}), хотя связь есть. Обычно это "
            "временный сбой сервиса, иногда — слово или символ, который голос не может произнести. "
            "На сервере так бывает постоянно: Microsoft не отдаёт озвучку адресам дата-центров — "
            "тогда выберите голос Piper (язык «Piper — без Microsoft»). "
            "Ещё можно нажать «Создать видео» через минуту или убрать из этой фразы необычные символы.",
            blocked=True,
        )
    if "403" in str(last_exc):  # Microsoft отказал в подключении — обычно из-за адреса
        raise TTSError(
            "Microsoft отказал в озвучке (ошибка 403). Обычно так бывает с адресами серверов и VPN. "
            "Выберите голос Piper (язык «Piper — без Microsoft») или укажите прокси в настройках.",
            blocked=True,
        )
    raise TTSError(
        "Не удалось связаться с сервисом озвучки Microsoft. Проверьте интернет "
        f"(или укажите прокси в настройках). Ошибка: {type(last_exc).__name__}: {last_exc}"
    )


def _short(text: str) -> str:
    return text if len(text) <= 80 else text[:77] + "…"


def _join(parts: list[tuple[bytes, list[Boundary]]]) -> tuple[bytes, list[Boundary]]:
    """Склеивает MP3-части и сдвигает метки слов на длительность предыдущих частей."""
    audio = bytearray()
    boundaries: list[Boundary] = []
    offset = 0.0
    for data, part_boundaries in parts:
        boundaries += [Boundary(b.text, b.start + offset, b.end + offset) for b in part_boundaries]
        audio += data
        offset += len(data) / MP3_BYTES_PER_SECOND
    return bytes(audio), boundaries


# Символы, которые голос точно умеет читать; остальное (значки, редкие символы) заменяем пробелом.
_PLAIN_RE = re.compile(r"[^\w\s.,!?;:…'\"«»()%+\-—–]", re.UNICODE)


def _smaller_parts(text: str) -> list[str]:
    """Части поменьше: строки, затем предложения, затем куски между запятыми."""
    for pattern in (r"\n+", r"(?<=[.!?…])\s+", r"(?<=[,;:—])\s+"):
        parts = [p.strip() for p in re.split(pattern, text) if re.search(r"\w", p)]
        if len(parts) > 1:
            return parts
    return []


def _service_works(voice: str) -> bool:
    """Отдаёт ли Microsoft звук вообще — короткой фразой на языке голоса."""
    probe = "Проверка связи." if voice.split("-")[0].lower() in CYRILLIC_LANGS else "Hello there."
    try:
        data, _ = asyncio.run(_edge_stream(probe, voice, 0, 0, 0))
        return bool(data)
    except Exception:
        return False


def _edge_piece(text: str, voice: str, rate: int, pitch: int, volume: int, attempts: int,
                service: dict) -> tuple[bytes, list[Boundary]]:
    """Часть текста. Если Microsoft не озвучивает именно её (а не всё подряд), она дробится
    на строки и предложения, а упрямый кусок повторяется без необычных символов."""
    try:
        return _edge_chunk(text, voice, rate, pitch, volume, attempts)
    except TTSError as exc:
        if not exc.blocked:
            raise
        if "works" not in service:
            service["works"] = _service_works(voice)
        if not service["works"]:  # звука нет ни для чего — дробить бессмысленно, выручит Piper
            raise
        parts = _smaller_parts(text)
        if parts:
            return _join([_edge_piece(p, voice, rate, pitch, volume, 2, service) for p in parts])
        plain = " ".join(_PLAIN_RE.sub(" ", text).split())
        if plain != text and re.search(r"\w", plain):
            try:
                return _edge_chunk(plain, voice, rate, pitch, volume, 2)
            except TTSError:
                pass
        raise TTSError(
            f"Microsoft не смог озвучить фразу «{_short(text)}» (голос {voice}) даже по частям и без "
            "необычных символов. Перефразируйте её или выберите голос Piper (язык «Piper — без Microsoft»).",
            blocked=True,
        ) from None


def edge_synthesize(text: str, voice: str, rate: int, pitch: int, volume: int, out_path: Path, attempts: int = 5) -> TTSResult:
    """Озвучивает текст по частям: сбой сервиса задевает одну короткую часть, и её
    можно повторить, не переозвучивая всё остальное."""
    problem = voice_language_problem(text, voice)
    if problem:
        raise TTSError(problem)
    chunks = split_for_tts(text)
    if not chunks:
        raise TTSError("В тексте нет слов для озвучки")
    service: dict = {}  # проверка «отдаёт ли Microsoft звук вообще» — одна на весь текст
    audio, boundaries = _join([_edge_piece(chunk, voice, rate, pitch, volume, attempts, service) for chunk in chunks])
    out_path.write_bytes(audio)
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


# ---------------------------------------------------------------- Piper (без Microsoft)

# Нейроголоса Piper звучат проще голосов Edge, но работают прямо на компьютере или
# сервере. Модель голоса (~60 МБ) скачивается при первом использовании.
PIPER_VOICES = [
    {"id": "piper:ru_RU-dmitri-medium", "name": "Дмитрий (Piper)", "locale": "ru-RU", "gender": "Male"},
    {"id": "piper:ru_RU-irina-medium", "name": "Ирина (Piper)", "locale": "ru-RU", "gender": "Female"},
    {"id": "piper:ru_RU-denis-medium", "name": "Денис (Piper)", "locale": "ru-RU", "gender": "Male"},
    {"id": "piper:ru_RU-ruslan-medium", "name": "Руслан (Piper)", "locale": "ru-RU", "gender": "Male"},
    {"id": "piper:uk_UA-ukrainian_tts-medium", "name": "Українська (Piper)", "locale": "uk-UA", "gender": "Female"},
    {"id": "piper:kk_KZ-issai-high", "name": "Қазақша (Piper)", "locale": "kk-KZ", "gender": "Female"},
    {"id": "piper:en_US-ryan-high", "name": "Ryan (Piper)", "locale": "en-US", "gender": "Male"},
    {"id": "piper:en_US-amy-medium", "name": "Amy (Piper)", "locale": "en-US", "gender": "Female"},
]
PIPER_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "{family}/{code}/{name}/{quality}/{code}-{name}-{quality}{ext}?download=true"
)
_piper_loaded: dict[str, object] = {}
_piper_lock = threading.Lock()
_piper_download_lock = threading.Lock()  # прослушивание и рендер не должны качать модель в один файл


def piper_available() -> bool:
    return importlib.util.find_spec("piper") is not None


def piper_list_voices() -> list[dict]:
    return [dict(v) for v in PIPER_VOICES] if piper_available() else []


def piper_model(code: str) -> Path:
    """Путь к модели голоса; при первом обращении скачивает её с HuggingFace."""
    if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}-[\w]+-(?:x_low|low|medium|high)", code):
        raise TTSError(f"Неизвестный голос Piper: {code}")
    folder = config.CACHE_DIR / "piper"
    model = folder / f"{code}.onnx"
    if model.exists() and Path(f"{model}.json").exists():
        return model
    with _piper_download_lock:
        if model.exists() and Path(f"{model}.json").exists():  # пока ждали, скачал другой поток
            return model
        _piper_download(code, folder)
    return model


def _piper_download(code: str, folder: Path) -> None:
    from .net import USER_AGENT, opener
    import urllib.request

    lang, name, quality = code.split("-", 2)
    folder.mkdir(parents=True, exist_ok=True)
    for ext in (".onnx.json", ".onnx"):
        url = PIPER_URL.format(family=lang.split("_")[0], code=lang, name=name, quality=quality, ext=ext)
        target = folder / f"{code}{ext}"
        part = target.with_name(target.name + ".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener().open(req, timeout=120) as resp, open(part, "wb") as out:
                while chunk := resp.read(1024 * 1024):
                    out.write(chunk)
            part.replace(target)
        except OSError as exc:
            part.unlink(missing_ok=True)
            if config.is_disk_full(exc):
                raise TTSError(config.disk_full_message()) from exc
            raise TTSError(f"Не удалось скачать голос Piper ({code}) с huggingface.co: {exc}") from exc


def piper_synthesize(text: str, code: str, rate: int, volume: int, out_path: Path) -> TTSResult:
    if not piper_available():
        raise TTSError("Голоса Piper не установлены (нужен пакет piper-tts) — выберите голос Microsoft.")
    from piper import PiperVoice, SynthesisConfig

    model = piper_model(code)
    with _piper_lock:  # загрузка модели — секунды, держим её в памяти
        voice = _piper_loaded.get(code)
        if voice is None:
            try:
                voice = _piper_loaded[code] = PiperVoice.load(model)
            except Exception as exc:  # битая или недокачанная модель — скачаем заново в следующий раз
                model.unlink(missing_ok=True)
                raise TTSError(f"Не удалось открыть голос Piper ({code}): {exc}") from exc
    cfg = SynthesisConfig(length_scale=1.0 / max(0.5, 1 + rate / 100), volume=max(0.2, 1 + volume / 100))
    wav = wave.open(str(out_path), "wb")
    try:
        voice.synthesize_wav(text, wav, syn_config=cfg)
        wav.close()
    except Exception as exc:  # ошибка внутри модели (onnxruntime) или нехватка памяти
        try:
            wav.close()  # у пустого файла close() падает своей ошибкой — она скрыла бы настоящую
        except Exception:
            pass
        if config.is_disk_full(exc):
            raise TTSError(config.disk_full_message()) from exc
        raise TTSError(f"Голос Piper не смог озвучить текст: {type(exc).__name__}: {exc}") from exc
    if not out_path.exists() or out_path.stat().st_size < 1000:
        raise TTSError("Голос Piper не создал звук")
    return TTSResult(out_path, [])


def piper_fallback(voice: str) -> dict | None:
    """Голос Piper на том же языке и, если есть, того же пола, что и голос Edge."""
    lang = voice.split("-")[0].lower()
    known = {v["id"]: v.get("gender") for v in FALLBACK_VOICES}
    gender = known.get(voice) or ("Female" if re.search(r"(Svetlana|Dariya|Polina|Aigul|Ava|Emma|Jenny|Aria)", voice) else "Male")
    same_lang = [v for v in piper_list_voices() if v["locale"].split("-")[0].lower() == lang]
    return next((v for v in same_lang if v["gender"] == gender), same_lang[0] if same_lang else None)

# --------------------------------------------------------------- Общий API


def synthesize(text: str, voice: str, rate: int, pitch: int, volume: int, out_dir: Path,
               allow_fallback: bool = True) -> TTSResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    if voice.startswith("system:"):
        return offline_synthesize(text, voice[len("system:"):], rate, out_dir / "voice.wav")
    if voice.startswith("piper:"):
        return piper_synthesize(text, voice[len("piper:"):], rate, volume, out_dir / "voice.wav")
    try:
        return edge_synthesize(text, voice or DEFAULT_VOICE, rate, pitch, volume, out_dir / "voice.mp3")
    except TTSError as exc:
        if not (exc.blocked and allow_fallback):
            raise
        fallback = piper_fallback(voice or DEFAULT_VOICE)
        if fallback is None:
            if piper_available():
                raise
            raise TTSError(f"{exc}\n\nЗапасные голоса Piper не установлены. На сервере выполните: sudo tiktok-studio update") from None
        try:
            result = piper_synthesize(text, fallback["id"][len("piper:"):], rate, volume, out_dir / "voice.wav")
        except TTSError as piper_exc:  # показываем обе причины, иначе непонятно, почему не выручил Piper
            raise TTSError(f"{exc}\n\nЗапасной голос Piper тоже не сработал: {piper_exc}") from None
        result.note = (
            f"Microsoft не прислал озвучку (с адресами серверов так бывает постоянно), поэтому ролик озвучен "
            f"голосом {fallback['name']}. Чтобы не ждать каждый раз, выберите на вкладке «Голос» язык "
            "«Piper — без Microsoft»."
        )
        return result


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
