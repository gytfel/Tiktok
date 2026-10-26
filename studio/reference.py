"""Похожее видео по образцу.

Чужой ролик (загруженный файл или ссылка на TikTok, Reels, Shorts) разбирается
на основу для своего: где меняются кадры и что в них показано, что и в каком
темпе говорит диктор. По этой схеме нейросеть пишет новый текст и описания
кадров, а студия заполняет ими проект — дальше его можно править как обычно.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ai, config, library, tts
from .ffmpeg_utils import FFmpegError, _run_simple, ffmpeg_exe, probe, run_ffmpeg
from .text_utils import clean_text, has_word_chars

MAX_SECONDS = 180  # разбираем первые 3 минуты
MAX_SHOTS = 12
MIN_SHOT = 0.8  # кадры короче склеиваются с соседними
LONG_SHOT = 7.0  # длинный план делим на части, чтобы картинки менялись
CUT_SCORE = 0.3  # порог смены кадра для фильтра select='gt(scene,…)'
BASE_CPS = 14.0  # символов в секунду у голоса студии при скорости 0
TILE_W, TILE_H = 270, 480
# Слайд-шоу (фото-пост TikTok или свои скриншоты): слайды крупнее, чтобы модель прочитала текст.
MAX_SLIDES = 20
SLIDE_W, SLIDE_H = 720, 1280
SLIDE_SECONDS = 3.0
BIG_TILE = (540, 960)
TIKTOK_SHORT_RE = re.compile(r"^https?://(?:(?:vm|vt)\.tiktok\.com|(?:www\.)?tiktok\.com/t)/\w+", re.I)
TIKTOK_POST_RE = re.compile(r"^https?://(?:www\.|m\.)?tiktok\.com/@([\w.-]*)/(photo|video)/(\d+)", re.I)

LANGUAGES = {
    "ru": "Russian", "en": "English", "uk": "Ukrainian", "kk": "Kazakh", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese", "pl": "Polish", "tr": "Turkish",
}
MODES = ("adapt", "exact")

Stage = Callable[[str, float], None]


class ReferenceError(RuntimeError):
    pass


@dataclass
class Shot:
    start: float
    end: float
    shot: str = ""  # что в кадре (по-английски, от модели, которая видит картинки)
    screen_text: str = ""  # надпись в кадре
    speech: str = ""  # что говорят в это время

    @property
    def seconds(self) -> float:
        return self.end - self.start


# ---------------------------------------------------------------- ссылка


def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://\S+$", text.strip()))


def download(url: str, on_progress: Callable[[float], None] | None = None) -> dict:
    """Скачивает видео по ссылке (TikTok, Reels, Shorts, VK…) в библиотеку загрузок."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise ReferenceError(
            "Для видео по ссылке нужен пакет yt-dlp. Обновите студию "
            "(на сервере: sudo tiktok-studio update) или загрузите видео файлом."
        ) from exc
    config.ensure_dirs()
    tmp = Path(tempfile.mkdtemp(prefix="ref-", dir=config.CACHE_DIR))

    def hook(d: dict) -> None:
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if on_progress and total and d.get("downloaded_bytes"):
            on_progress(min(1.0, d["downloaded_bytes"] / total))

    opts = {
        "outtmpl": str(tmp / "video.%(ext)s"),
        # Готовый mp4 со звуком, не больше 1080x1920; иначе — склейка видео и звука.
        "format": "b[ext=mp4][height<=1920]/b[height<=1920]/bv*[height<=1920]+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "max_filesize": 400 * 1024 * 1024,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "ffmpeg_location": ffmpeg_exe(),
        "progress_hooks": [hook],
    }
    if config.get_proxy():
        opts["proxy"] = config.get_proxy()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            target = resolve_short_link(ydl, url.strip())
            post = TIKTOK_POST_RE.match(target)
            if post and post.group(2).lower() == "photo":  # фото-пост: слайды с музыкой, yt-dlp их не берёт
                return save_tiktok_slides(ydl, target, url.strip(), tmp, on_progress)
            info = ydl.extract_info(target, download=True) or {}
            files = sorted((p for p in tmp.iterdir() if p.suffix.lower() in config.VIDEO_EXTENSIONS), key=lambda p: p.stat().st_size)
            if not files and post:  # слайд-шоу по ссылке /video/: yt-dlp скачал только музыку
                return save_tiktok_slides(ydl, target, url.strip(), tmp, on_progress)
        if not files:
            raise ReferenceError("По ссылке не нашлось видео — проверьте, что ссылка ведёт на ролик, а не на профиль.")
        title = " ".join(str(info.get("title") or info.get("description") or "видео").split())
        with open(files[-1], "rb") as src:
            meta = library.save_upload(
                f"Образец — {title[:60]}{files[-1].suffix.lower()}",
                src,
                extra={"reference_url": url.strip(), "caption": str(info.get("description") or "")[:2000]},
            )
        return meta
    except ReferenceError:
        raise
    except Exception as exc:  # yt-dlp сообщает об ошибках своими исключениями
        raise ReferenceError(explain_download_error(str(exc))) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def resolve_short_link(ydl, url: str) -> str:
    """vt.tiktok.com/… → полный адрес поста: по нему видно, видео это или слайды."""
    if not TIKTOK_SHORT_RE.match(url):
        return url
    from yt_dlp.networking import HEADRequest

    try:
        resp = ydl.urlopen(HEADRequest(url, headers={"User-Agent": "facebookexternalhit/1.1"}))
        final = resp.url
        resp.close()
    except Exception:
        return url  # пусть yt-dlp попробует сам
    return final or url


def _fetch(ydl, url: str) -> bytes:
    from yt_dlp.networking import Request

    resp = ydl.urlopen(Request(url, headers={"Referer": "https://www.tiktok.com/"}))
    try:
        return resp.read()
    finally:
        resp.close()


def tiktok_post_data(ydl, page_url: str) -> dict:
    """Данные поста со страницы TikTok — тем же способом, что и у yt-dlp (с обходом проверок)."""
    user, _, post_id = TIKTOK_POST_RE.match(page_url).groups()
    ie = ydl.get_info_extractor("TikTok")
    for page in (page_url, f"https://www.tiktok.com/@{user or '_'}/video/{post_id}"):
        try:
            data, _status = ie._extract_web_data_and_status(page, post_id, fatal=False)
        except Exception:
            data = {}
        if isinstance(data, dict) and (data.get("imagePost") or {}).get("images"):
            return data
    return {}


def save_tiktok_slides(ydl, page_url: str, source_url: str, workdir: Path,
                       on_progress: Callable[[float], None] | None = None) -> dict:
    """Фото-пост TikTok: скачивает слайды и музыку и собирает из них видео-образец."""
    data = tiktok_post_data(ydl, page_url)
    images = (data.get("imagePost") or {}).get("images") or []
    if not images:
        raise ReferenceError(
            "Не удалось получить слайды из этого поста TikTok. Сохраните скриншоты слайдов "
            "и загрузите их картинками кнопкой «Загрузить видео или скриншоты»."
        )
    pictures: list[bytes] = []
    for i, img in enumerate(images[:MAX_SLIDES]):
        for src in ((img.get("imageURL") or {}).get("urlList") or []) if isinstance(img, dict) else []:
            try:
                pictures.append(_fetch(ydl, src))
                break
            except Exception:
                continue
        if on_progress:
            on_progress((i + 1) / min(len(images), MAX_SLIDES))
    music_info = data.get("music") or {}
    music = None
    if music_info.get("playUrl"):
        try:
            music = _fetch(ydl, music_info["playUrl"])
        except Exception:
            music = None  # без музыки разбор тоже работает
    count = len(pictures)
    seconds = SLIDE_SECONDS
    if count and music_info.get("duration"):
        seconds = max(2.0, min(4.0, float(music_info["duration"]) / count))
    video, slides = build_slideshow(pictures, music, seconds, workdir)
    desc = " ".join(str(data.get("desc") or "").split())
    with open(video, "rb") as src:
        return library.save_upload(
            f"Образец (слайды) — {(desc or 'TikTok')[:50]}.mp4",
            src,
            extra={"reference_url": source_url, "caption": desc[:2000], **slideshow_meta(seconds, slides, len(images))},
        )


def slideshow_meta(seconds: float, slides: int, total: int) -> dict:
    return {"slideshow": True, "slide_seconds": seconds, "slides": slides, "slides_total": total}


def _fit_slide(data: bytes):
    """Слайд 720x1280: картинка целиком на размытой подложке из неё же."""
    from PIL import Image, ImageFilter, ImageOps

    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        bg = ImageOps.fit(im, (SLIDE_W, SLIDE_H)).filter(ImageFilter.GaussianBlur(40)).point(lambda v: int(v * 0.45))
        fg = ImageOps.contain(im, (SLIDE_W, SLIDE_H))
        bg.paste(fg, ((SLIDE_W - fg.width) // 2, (SLIDE_H - fg.height) // 2))
        return bg


def build_slideshow(pictures: list[bytes], music: bytes | None, seconds: float, workdir: Path) -> tuple[Path, int]:
    """Видео из картинок (по seconds секунд на слайд) с музыкой, если она есть.
    Возвращает путь и число слайдов, которые удалось открыть."""
    names = []
    for data in pictures[:MAX_SLIDES]:
        try:
            slide = _fit_slide(data)
        except Exception:  # не картинка или неизвестный формат — пропускаем
            continue
        name = f"slide{len(names):02d}.jpg"
        slide.save(workdir / name, "JPEG", quality=90)
        names.append(name)
    if not names:
        raise ReferenceError("Не получилось открыть ни одной картинки — нужны JPG, PNG или WEBP.")
    lines = []
    for name in names:
        lines += [f"file '{name}'", f"duration {seconds:.3f}"]
    lines.append(f"file '{names[-1]}'")  # concat требует повторить последний кадр
    (workdir / "slides.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = ["-f", "concat", "-safe", "0", "-i", "slides.txt"]
    if music:
        (workdir / "music.mp3").write_bytes(music)
        args += ["-stream_loop", "-1", "-i", "music.mp3", "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "128k"]
    args += ["-vf", "fps=25,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-t", f"{seconds * len(names):.2f}", "slideshow.mp4"]
    try:
        run_ffmpeg(args, cwd=workdir)
    except FFmpegError:
        if not music:
            raise
        return build_slideshow(pictures, None, seconds, workdir)  # музыка битая — собираем без неё
    return workdir / "slideshow.mp4", len(names)


def slideshow_from_uploads(item_ids: list[str]) -> dict:
    """Свои скриншоты или картинки (по порядку) → видео-образец из слайдов."""
    pictures = []
    for item_id in item_ids[:MAX_SLIDES]:
        try:
            meta, path = library.get_upload(item_id)
        except library.LibraryError as exc:
            raise ReferenceError("Картинка не найдена — загрузите скриншоты ещё раз") from exc
        if meta.get("kind") != "image":
            raise ReferenceError("Выберите одно видео или несколько картинок (скриншотов)")
        pictures.append(path.read_bytes())
    config.ensure_dirs()
    workdir = Path(tempfile.mkdtemp(prefix="slides-", dir=config.CACHE_DIR))
    try:
        video, slides = build_slideshow(pictures, None, SLIDE_SECONDS, workdir)
        with open(video, "rb") as src:
            return library.save_upload(
                f"Образец (слайды) — {slides} картинок.mp4", src, extra=slideshow_meta(SLIDE_SECONDS, slides, len(pictures))
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def explain_download_error(message: str) -> str:
    low = message.lower()
    tail = " Если не получится — скачайте ролик на телефон и загрузите файлом."
    if "unsupported url" in low:
        return "Эта ссылка не поддерживается. Нужна ссылка на сам ролик (TikTok, Instagram Reels, YouTube Shorts, VK)." + tail
    if "login" in low or "cookies" in low or "sign in" in low or "private" in low or "confirm your age" in low:
        return "Сайт не отдаёт это видео без входа в аккаунт (закрытый профиль, возраст или Instagram)." + tail
    if "404" in low or "not found" in low or "unavailable" in low or "removed" in low:
        return "Видео не найдено — возможно, его удалили или ссылка неполная." + tail
    if "403" in low or "forbidden" in low or "blocked" in low or "geo" in low:
        return "Сайт не отдаёт видео с этого адреса (403 или ограничение по стране)." + tail
    if "max_filesize" in low or "larger than max" in low:
        return "Видео слишком большое (больше 400 МБ)." + tail
    if config.is_disk_full(message):
        return config.disk_full_message()
    return f"Не удалось скачать видео: {message.strip()[:300]}." + tail


# ---------------------------------------------------------------- кадры


def detect_cuts(path: Path, limit: float) -> list[float]:
    """Моменты смены кадра (секунды) по резкому изменению картинки."""
    out = _run_simple(
        [
            ffmpeg_exe(), "-hide_banner", "-nostats", "-t", f"{limit:.2f}", "-i", str(path), "-an", "-sn",
            "-vf", f"scale=160:-2,select='gt(scene,{CUT_SCORE})',showinfo", "-f", "null", "-",
        ]
    )
    return sorted(float(t) for t in re.findall(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)", out))


def plan_shots(duration: float, cuts: list[float]) -> list[Shot]:
    """Делит ролик на планы по сменам кадра: короткие склеивает, длинные режет."""
    bounds = [0.0] + [c for c in cuts if MIN_SHOT <= c <= duration - MIN_SHOT] + [duration]
    spans: list[list[float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if spans and (b - a < MIN_SHOT or spans[-1][1] - spans[-1][0] < MIN_SHOT):
            spans[-1][1] = b
        elif b > a:
            spans.append([a, b])
    shots: list[Shot] = []
    for a, b in spans:
        parts = max(1, math.ceil((b - a) / 5.0)) if b - a > LONG_SHOT else 1
        step = (b - a) / parts
        shots += [Shot(a + i * step, a + (i + 1) * step) for i in range(parts)]
    while len(shots) > MAX_SHOTS:  # склеиваем самые короткие соседние пары
        i = min(range(len(shots) - 1), key=lambda k: shots[k].seconds + shots[k + 1].seconds)
        shots[i:i + 2] = [Shot(shots[i].start, shots[i + 1].end)]
    return shots


def grab_frames(path: Path, shots: list[Shot], workdir: Path, size: tuple[int, int] = (TILE_W, TILE_H)) -> list[Path]:
    w, h = size
    frames = []
    for i, sh in enumerate(shots, 1):
        out = workdir / f"frame{i:02d}.jpg"
        t = sh.start + sh.seconds / 2
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
        run_ffmpeg(["-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out)])
        frames.append(out)
    return frames


def contact_sheet(frames: list[Path], tile: tuple[int, int] = (TILE_W, TILE_H), cols: int = 4) -> bytes:
    """Кадры на одной картинке с номерами — модели достаточно одного снимка."""
    from PIL import Image, ImageDraw, ImageFont

    tw, th = tile
    cols = min(cols, len(frames))
    rows = math.ceil(len(frames) / cols)
    gap = 6
    sheet = Image.new("RGB", (cols * (tw + gap) + gap, rows * (th + gap) + gap), "#111111")
    try:
        font = ImageFont.truetype(str(config.FONTS_DIR / config.FONTS[config.DEFAULT_FONT]["file"]), 44)
    except OSError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(sheet)
    for i, frame in enumerate(frames):
        x = gap + (i % cols) * (tw + gap)
        y = gap + (i // cols) * (th + gap)
        with Image.open(frame) as im:
            sheet.paste(im.convert("RGB").resize((tw, th)), (x, y))
        draw.rectangle((x, y, x + 64, y + 58), fill="#FFE600")
        draw.text((x + 32, y + 29), str(i + 1), fill="#000000", font=font, anchor="mm")
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    return buf.getvalue()


_LOOK_PROMPT = (
    "This is a contact sheet of {n} keyframes from a vertical short video (TikTok/Reels), numbered 1-{n} "
    "in yellow squares, left to right, top to bottom. For EVERY frame write in English, 15-35 words, what "
    "the shot shows: subject, action, setting, camera angle and distance, lighting and colors, and the kind "
    "of footage (real video, screen recording, screenshot, animation, text slide). Also copy the on-screen "
    "text exactly, in its original language: for screenshots and text slides all readable text, up to about "
    "300 characters (empty string if none). Reply with JSON only: "
    '{{"frames": [{{"n": 1, "shot": "...", "text": "..."}}], "look": "one sentence about the overall visual style"}}'
)


def describe_shots(provider: ai.Provider, frames: list[Path], shots: list[Shot], per_sheet: int = MAX_SHOTS,
                   tile: tuple[int, int] = (TILE_W, TILE_H)) -> str:
    """Подписывает планы по снимкам-раскладкам (по per_sheet кадров на снимок).
    Возвращает общее описание стиля."""
    looks = []
    for offset in range(0, len(frames), per_sheet):
        chunk = frames[offset:offset + per_sheet]
        sheet = contact_sheet(chunk, tile, cols=2 if per_sheet <= 4 else 4)
        data = parse_object(provider.look(sheet, _LOOK_PROMPT.format(n=len(chunk))))
        for item in data.get("frames") or []:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(chunk):
                shots[offset + n - 1].shot = " ".join(str(item.get("shot") or "").split())[:400]
                shots[offset + n - 1].screen_text = " ".join(str(item.get("text") or "").split())[:500]
        looks.append(" ".join(str(data.get("look") or "").split()))
    return next((x for x in looks if x), "")[:300]


# ---------------------------------------------------------------- речь


def extract_audio(path: Path, workdir: Path, limit: float) -> bytes:
    out = workdir / "speech.mp3"
    run_ffmpeg(["-t", f"{limit:.2f}", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(out)])
    return out.read_bytes()


def assign_speech(shots: list[Shot], words: list[tuple[str, float, float]]) -> None:
    """Раздаёт слова по планам: слово относится к плану, где звучит его середина."""
    buckets: list[list[str]] = [[] for _ in shots]
    for word, start, end in words:
        mid = (start + end) / 2
        i = next((k for k, sh in enumerate(shots) if mid < sh.end), len(shots) - 1)
        buckets[i].append(word)
    for sh, bucket in zip(shots, buckets):
        sh.speech = " ".join(bucket)


def speech_pace(words: list[tuple[str, float, float]]) -> float | None:
    """Темп речи в символах в секунду (None, если речи почти нет)."""
    if len(words) < 8:
        return None
    span = words[-1][2] - words[0][1]
    if span <= 1:
        return None
    chars = len(" ".join(w for w, _, _ in words))
    return max(8.0, min(22.0, chars / span))


# ---------------------------------------------------------------- сценарий

_SCRIPT_SYSTEM = (
    "You help a creator make a new short vertical video (TikTok/Reels/Shorts) modelled on a REFERENCE "
    "video. You get the reference's scenes with timings, what each shot shows, its on-screen text and what "
    "the narrator says during it. Copy the reference's structure, pacing, hook technique and visual idea. "
    "{mode} "
    "Reply with JSON only, exactly in this shape: "
    '{{"hook": "...", "style": "...", "summary": "...", "scenes": [{{"text": "...", "prompt": "..."}}]}}. '
    "Rules. scenes: exactly {n} items, in order, one per reference scene. text: the voice-over for that "
    "scene in {lang}, natural spoken language, about the number of characters given for the scene (the "
    "narrator has to fit into its duration), no emojis, no hashtags, no stage directions; if the reference "
    "scene is silent, still write a short line that fits its meaning. prompt: an English prompt for an AI "
    "image generator, 25-60 words, describing a shot like the reference one (same kind of subject, framing, "
    "setting, mood) but for the new video; repeat the description of recurring characters so they stay the "
    "same; never ask for written text, letters, captions, logos or readable UI. hook: a short on-screen "
    "title in {lang}, at most 60 characters, if the reference opens with a text hook, otherwise an empty "
    "string. style: one of {styles} - the closest to the reference footage. summary: 2-3 sentences in "
    "Russian about what the reference is about and which techniques make it work."
)

_MODE_TOPIC = "The new video is about: {topic}. Keep the reference's structure and rhythm, but the content is about this new topic."
_MODE_REWRITE = "Keep the reference's topic and message, but say it in your own words - do not copy its sentences."
_MODE_EXACT = "Keep the narrator's original words (translate them to {lang} if they are in another language); just split them over the scenes."
_MODE_SLIDES_EXACT = (
    "Use the on-screen text of each slide as the voice-over of its scene (translate it to {lang} if needed), "
    "only cleaned up for reading aloud: drop clock times, usernames, UI labels and other screen clutter."
)


def reference_brief(shots: list[Shot], duration: float, transcript: ai.Transcript | None, cps: float,
                    look: str, caption: str, slideshow: bool = False) -> str:
    lines = [f"Reference video: {duration:.1f} s, {len(shots)} scenes."]
    if slideshow:
        lines.append(
            "The reference is a photo slideshow (screenshots or pictures, one per scene) over background music. "
            "Its message is in the on-screen text of the slides; turn it into a narrated video, one voice-over line per slide."
        )
    elif transcript and transcript.text:
        lines.append(f"Speech language: {transcript.language or 'unknown'}. Full voice-over: \"{transcript.text[:3000]}\"")
    else:
        lines.append("The reference has no voice-over (music or silence).")
    if caption.strip():
        lines.append(f"Post caption: \"{' '.join(caption.split())[:500]}\"")
    if look:
        lines.append(f"Overall look: {look}")
    lines.append("Scenes:")
    for i, sh in enumerate(shots, 1):
        chars = max(15, round(sh.seconds * cps))
        parts = [f"{i}. {sh.start:.1f}-{sh.end:.1f} s, ~{chars} characters of voice-over."]
        if sh.shot:
            parts.append(f"Shot: {sh.shot}")
        if sh.screen_text:
            parts.append(f"On-screen text: \"{sh.screen_text}\"")
        if sh.speech:
            parts.append(f"Narrator: \"{sh.speech}\"")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def parse_object(answer: str) -> dict:
    """JSON-объект из ответа модели (бывает обёрнут в ```json … ``` или окружён текстом)."""
    text = re.sub(r"^```(?:json)?|```$", "", answer.strip(), flags=re.M).strip()
    for candidate in (text, (re.search(r"\{.*\}", text, flags=re.S) or [None])[0]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise ai.AIError("нейросеть ответила не в том формате — попробуйте ещё раз")


def write_script(provider: ai.Provider, brief: str, n: int, lang: str, mode: str, topic: str,
                 slideshow: bool = False) -> dict:
    lang_name = LANGUAGES.get(lang, "the same language as the reference voice-over")
    if mode == "exact":
        mode_text = (_MODE_SLIDES_EXACT if slideshow else _MODE_EXACT).format(lang=lang_name)
    elif topic.strip():
        mode_text = _MODE_TOPIC.format(topic=topic.strip()[:500])
    else:
        mode_text = _MODE_REWRITE
    system = _SCRIPT_SYSTEM.format(mode=mode_text, n=n, lang=lang_name, styles=", ".join(ai.STYLES))
    data = parse_object(provider.chat(system, brief, max_tokens=4000))
    scenes = []
    for item in data.get("scenes") or []:
        if isinstance(item, dict):
            scenes.append({"text": " ".join(str(item.get("text") or "").split()), "prompt": " ".join(str(item.get("prompt") or "").split())})
    if not any(has_word_chars(sc["text"]) for sc in scenes):
        raise ai.AIError("нейросеть не написала текст сцен — попробуйте ещё раз")
    data["scenes"] = scenes
    return data


# ---------------------------------------------------------------- проект


def pick_voice(lang: str, current: str) -> str:
    """Голос для языка ролика: текущий, если он подходит, иначе того же пола на нужном языке."""
    if current.lower().startswith(lang.lower() + "-"):
        return current
    try:
        voices = tts.list_voices()["voices"]
    except Exception:
        voices = list(tts.FALLBACK_VOICES)
    gender = next((v.get("gender") for v in voices if v.get("id") == current), "Male")
    matching = [v for v in voices if str(v.get("locale", "")).lower().startswith(lang.lower() + "-")]
    if not matching:
        return current
    same = [v for v in matching if v.get("gender") == gender] or matching
    # Для русского — привычный Дмитрий/Светлана, если они есть.
    preferred = {"ru": ("ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural")}.get(lang, ())
    for vid in preferred:
        if any(v.get("id") == vid for v in same):
            return vid
    return str(same[0]["id"])


def voice_rate(cps: float | None) -> int | None:
    if cps is None:
        return None
    rate = (cps / BASE_CPS - 1) * 100
    return int(max(-20, min(40, round(rate / 5) * 5)))


def build_project(scenes: list[dict], hook: str, style: str, topic: str) -> dict:
    """Изменения проекта: текст по абзацам (абзац — сцена образца) и ИИ-кадры с промтами."""
    paragraphs = [(sc["text"], sc["prompt"]) for sc in scenes if has_word_chars(sc["text"])]
    text = clean_text("\n\n".join(t for t, _ in paragraphs))
    prompts = [p for _, p in paragraphs]
    ai_scenes = []
    for sc in ai.split_scenes(text):
        prompt = prompts[sc.paragraph] if sc.paragraph < len(prompts) else ""
        ai_scenes.append({"text": sc.text, "prompt": prompt or sc.text, "image": None, "video": None, "use_video": False})
    patch: dict = {
        "text": text,
        "background": {
            "type": "ai",
            "ai_scenes": ai_scenes,
            "ai_style": style if style in ai.STYLES else ai.DEFAULT_STYLE,
            "ai_context": topic.strip()[:500],
        },
    }
    if hook.strip():
        patch["title"] = {"text": hook.strip()[:200]}
    return patch


def exact_scenes(shots: list[Shot], llm_scenes: list[dict]) -> list[dict]:
    """Слова диктора как есть: по одному абзацу на план, немые планы — к соседнему."""
    out: list[dict] = []
    for i, sh in enumerate(shots):
        prompt = llm_scenes[i]["prompt"] if i < len(llm_scenes) and llm_scenes[i]["prompt"] else sh.shot
        if has_word_chars(sh.speech):
            out.append({"text": sh.speech, "prompt": prompt})
        elif out and not out[-1]["prompt"]:
            out[-1]["prompt"] = prompt
    return out


# ---------------------------------------------------------------- всё вместе


def analyze(item_id: str, provider: ai.Provider, *, topic: str = "", mode: str = "adapt", language: str = "ru",
            current_voice: str = "", on_stage: Stage | None = None) -> dict:
    """Разбирает загруженное видео-образец и возвращает основу для проекта."""
    stage = on_stage or (lambda text, frac: None)
    mode = mode if mode in MODES else "adapt"
    try:
        meta, path = library.get_upload(item_id)
    except library.LibraryError as exc:
        raise ReferenceError("Видео-образец не найдено — загрузите его ещё раз") from exc
    if meta.get("kind") != "video":
        raise ReferenceError("Образцом может быть только видео")
    info = probe(path)
    duration = float(info.get("duration") or meta.get("duration") or 0)
    if duration <= 0.5:
        raise ReferenceError("Не удалось определить длительность видео — файл повреждён?")
    warnings: list[str] = []
    limit = min(duration, MAX_SECONDS)
    if duration > MAX_SECONDS:
        warnings.append(f"Видео длиннее {MAX_SECONDS // 60} минут — разобраны только первые {MAX_SECONDS // 60}.")

    workdir = Path(tempfile.mkdtemp(prefix="analyze-", dir=config.CACHE_DIR))
    try:
        slideshow = bool(meta.get("slideshow"))
        transcript: ai.Transcript | None = None
        if info.get("has_audio") and not slideshow:  # у слайд-шоу звук — это музыка
            stage("Распознаю речь…", 0.1)
            try:
                transcript = provider.transcribe(extract_audio(path, workdir, limit), limit)
            except (ai.AIError, FFmpegError) as exc:
                warnings.append(f"Речь не распознана ({exc}) — текст придуман по кадрам.")
        if transcript and len(transcript.words) < 3:
            transcript = None  # музыка без слов

        stage("Ищу смены кадров…", 0.35)
        slide = float(meta.get("slide_seconds") or SLIDE_SECONDS)
        if slideshow and meta.get("slides"):  # границы слайдов известны точно
            shots = [Shot(i * slide, (i + 1) * slide) for i in range(min(int(meta["slides"]), MAX_SLIDES)) if i * slide < limit]
            if int(meta.get("slides_total") or 0) > len(shots):
                warnings.append(f"В посте {meta['slides_total']} слайдов — разобраны первые {len(shots)}.")
        else:
            shots = plan_shots(limit, detect_cuts(path, limit))
        if transcript:
            assign_speech(shots, transcript.words)

        stage("Смотрю, что в кадрах…", 0.5)
        frames = grab_frames(path, shots, workdir, BIG_TILE if slideshow else (TILE_W, TILE_H))
        sheet = contact_sheet(frames, cols=5 if len(frames) > 12 else 4)
        look = ""
        try:
            if slideshow:  # по 4 крупных слайда на снимок — чтобы прочитать текст на скриншотах
                look = describe_shots(provider, frames, shots, per_sheet=4, tile=BIG_TILE)
            else:
                look = describe_shots(provider, frames, shots)
        except ai.AIError as exc:
            warnings.append(f"Не удалось разобрать кадры ({exc}) — описания кадров придуманы по тексту.")

        if language == "same":
            lang = (transcript.language if transcript else "") or "ru"
        else:
            lang = language if language in LANGUAGES else "ru"
        cps = speech_pace(transcript.words) if transcript else None
        if mode == "exact" and not transcript and not (slideshow and any(sh.screen_text for sh in shots)):
            warnings.append("В образце нет речи — текст написан заново по кадрам и надписям.")
            mode = "adapt"

        stage("Пишу сценарий…", 0.7)
        brief = reference_brief(shots, limit, transcript, cps or ai.CHARS_PER_SECOND, look, str(meta.get("caption") or ""), slideshow)
        script = write_script(provider, brief, len(shots), lang, mode, topic, slideshow)
        scenes = script["scenes"]
        if mode == "exact" and transcript and (not transcript.language or transcript.language == lang):
            scenes = exact_scenes(shots, scenes) or scenes

        project = build_project(scenes, str(script.get("hook") or ""), str(script.get("style") or ""), topic)
        project["voice"] = {"voice": pick_voice(lang, current_voice or "ru-RU-DmitryNeural")}
        rate = voice_rate(cps)
        if rate is not None:
            project["voice"]["rate"] = rate
        stage("Готово", 1.0)
        return {
            "project": project,
            "summary": " ".join(str(script.get("summary") or "").split())[:800],
            "warnings": warnings,
            "reference": {
                "id": item_id,
                "name": meta.get("name"),
                "duration": round(duration, 1),
                "shots": [
                    {"start": round(sh.start, 1), "end": round(sh.end, 1), "shot": sh.shot, "text": sh.screen_text, "speech": sh.speech}
                    for sh in shots
                ],
                "language": transcript.language if transcript else "",
                "transcript": transcript.text if transcript else "",
                "pace": round(cps, 1) if cps else None,
                "slideshow": slideshow,
                "sheet": "data:image/jpeg;base64," + base64.b64encode(sheet).decode("ascii"),
            },
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
