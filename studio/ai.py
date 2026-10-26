"""Картинки и видео для сцен ролика, созданные нейросетями.

Текст озвучки делится на сцены (по предложениям и абзацам), языковая модель
придумывает для каждой сцены описание кадра на английском, а модель-художник
рисует картинку. Поддерживаются два сервиса:

* Cloudflare Workers AI — бесплатный дневной лимит, только картинки;
* Pollinations — платный (дёшево для картинок), умеет и видео.
"""

from __future__ import annotations

import base64
import io
import json
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from . import config, library
from .net import NetError, multipart, parse_json, request
from .text_utils import tokenize

# ---------------------------------------------------------------- стили

STYLES: dict[str, dict] = {
    "photo": {"label": "Реалистичное фото", "suffix": "photorealistic photo, natural light, shallow depth of field, 35mm, high detail"},
    "cinema": {"label": "Кино", "suffix": "cinematic film still, dramatic lighting, moody color grading, anamorphic lens, high detail"},
    "neon": {"label": "Неон / киберпанк", "suffix": "cyberpunk style, dark scene lit by glowing blue and pink neon, futuristic, high contrast"},
    "3d": {"label": "3D-мультфильм", "suffix": "3D animated movie style, soft studio lighting, expressive characters, highly detailed render"},
    "anime": {"label": "Аниме", "suffix": "anime illustration, vibrant colors, detailed background, clean line art"},
    "illustration": {"label": "Иллюстрация", "suffix": "flat digital illustration, bold simple shapes, vibrant palette, clean vector style"},
}
DEFAULT_STYLE = "cinema"
COMMON_SUFFIX = "vertical 9:16 composition, main subject in the center, no text, no letters, no captions, no watermark"
NEGATIVE = "text, letters, words, caption, subtitles, watermark, logo, blurry, low quality, deformed hands, extra fingers"

IMAGE_W, IMAGE_H = 768, 1344  # ~9:16, кратно 64 — подходит большинству моделей

CF_IMAGE_MODELS = {
    "flux": "@cf/black-forest-labs/flux-1-schnell",
    "sdxl": "@cf/bytedance/stable-diffusion-xl-lightning",
}
CF_TEXT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
CF_STT_MODEL = "@cf/openai/whisper-large-v3-turbo"  # распознавание речи
CF_VISION_MODEL = "@cf/meta/llama-4-scout-17b-16e-instruct"  # понимает картинки
POLLINATIONS_STT_MODEL = "openai/whisper-large-v3"
POLLINATIONS_BASE = "https://gen.pollinations.ai"
VIDEO_DURATIONS = (4, 6, 8)


class AIError(RuntimeError):
    pass


def build_prompt(prompt: str, style: str) -> str:
    suffix = STYLES.get(style, STYLES[DEFAULT_STYLE])["suffix"]
    return f"{prompt.strip().rstrip('.')}. {suffix}, {COMMON_SUFFIX}"


# ---------------------------------------------------------------- сцены

CHARS_PER_SECOND = 14.7


@dataclass
class Scene:
    text: str
    start_char: int
    end_char: int
    paragraph: int

    @property
    def seconds(self) -> float:
        return estimate_seconds(self.text)


def estimate_seconds(text: str) -> float:
    return len(text) / CHARS_PER_SECOND + 0.35


def split_scenes(text: str, min_sec: float = 2.5, max_sec: float = 6.0, max_scenes: int = 12) -> list[Scene]:
    """Делит очищенный текст на сцены по 2.5–6 секунд озвучки. Абзац всегда
    начинает новую сцену, длинные предложения режутся по запятым."""
    tokens = tokenize(text)
    if not tokens:
        return []
    # 1. Предложения (длинные — по частям).
    parts: list[Scene] = []
    start = 0
    for i, tok in enumerate(tokens):
        last = i == len(tokens) - 1
        cur_text = text[tokens[start].start_char:tok.end_char]
        long_enough = estimate_seconds(cur_text) >= max_sec * 0.8
        if last or tok.ends_sentence or tokens[i + 1].paragraph != tok.paragraph or (tok.ends_clause and long_enough):
            parts.append(Scene(cur_text, tokens[start].start_char, tok.end_char, tok.paragraph))
            start = i + 1

    # 2. Склеиваем предложения в сцены.
    scenes: list[Scene] = []
    for part in parts:
        if scenes and scenes[-1].paragraph == part.paragraph:
            cur = scenes[-1]
            if cur.seconds < min_sec or cur.seconds + part.seconds <= max_sec:
                scenes[-1] = _join(text, cur, part)
                continue
        scenes.append(part)

    # 3. Совсем короткий хвост абзаца приклеиваем к предыдущей сцене.
    merged: list[Scene] = []
    for sc in scenes:
        if merged and sc.seconds < 1.8 and merged[-1].paragraph == sc.paragraph:
            merged[-1] = _join(text, merged[-1], sc)
        else:
            merged.append(sc)

    # 4. Ограничиваем число сцен: склеиваем самые короткие соседние пары.
    while len(merged) > max_scenes:
        i = min(range(len(merged) - 1), key=lambda k: merged[k].seconds + merged[k + 1].seconds)
        merged[i:i + 2] = [_join(text, merged[i], merged[i + 1])]
    return merged


def _join(text: str, a: Scene, b: Scene) -> Scene:
    return Scene(text[a.start_char:b.end_char], a.start_char, b.end_char, a.paragraph)


def same_text(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


# ---------------------------------------------------------------- распознанная речь


@dataclass
class Transcript:
    text: str
    language: str  # код ISO-639-1 («ru», «en») или ""
    words: list[tuple[str, float, float]]  # слово, начало и конец в секундах


def transcript_from(result: dict, duration: float = 0.0) -> Transcript:
    """Приводит ответы разных моделей Whisper к одному виду. Если у слов нет
    своих времён, раскладывает слова фразы равномерно по её длительности."""
    if not isinstance(result, dict):
        raise AIError("Сервис распознавания речи вернул неожиданный ответ")
    info = result.get("transcription_info") if isinstance(result.get("transcription_info"), dict) else {}
    language = str(info.get("language") or result.get("language") or "").strip().lower()
    names = {"russian": "ru", "english": "en", "ukrainian": "uk", "kazakh": "kk", "german": "de", "french": "fr",
             "spanish": "es", "italian": "it", "portuguese": "pt", "polish": "pl", "turkish": "tr"}
    language = names.get(language, language)[:5]
    words: list[tuple[str, float, float]] = []

    def add_words(items: list) -> None:
        for w in items:
            if isinstance(w, dict) and str(w.get("word") or "").strip():
                try:
                    words.append((str(w["word"]).strip(), float(w["start"]), float(w["end"])))
                except (KeyError, TypeError, ValueError):
                    continue

    def spread(text: str, start: float, end: float) -> None:
        parts = text.split()
        step = max(end - start, 0.0) / max(len(parts), 1)
        for i, word in enumerate(parts):
            words.append((word, start + i * step, start + (i + 1) * step))

    if isinstance(result.get("words"), list) and result["words"]:
        add_words(result["words"])
    else:
        for seg in result.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            before = len(words)
            if isinstance(seg.get("words"), list):
                add_words(seg["words"])
            if len(words) == before:
                try:
                    spread(str(seg.get("text") or ""), float(seg.get("start") or 0), float(seg.get("end") or 0))
                except (TypeError, ValueError):
                    continue
    text = " ".join(str(result.get("text") or "").split())
    if not words and text:
        spread(text, 0.0, duration)
    if not text:
        text = " ".join(w for w, _, _ in words)
    return Transcript(text=text, language=language, words=words)


def _data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


# ---------------------------------------------------------------- сервисы


class Provider:
    name = ""
    label = ""
    supports_video = False

    def chat(self, system: str, user: str, max_tokens: int = 1500) -> str:
        raise NotImplementedError

    def transcribe(self, audio: bytes, duration: float = 0.0) -> Transcript:
        raise AIError(f"{self.label} не умеет распознавать речь")

    def look(self, image: bytes, prompt: str) -> str:
        """Ответ модели, которая видит картинку (JPEG)."""
        raise AIError(f"{self.label} не умеет описывать картинки")

    def image(self, prompt: str, seed: int) -> bytes:
        raise NotImplementedError

    def video(self, prompt: str, seconds: int, on_wait: Callable[[str], None] | None = None) -> bytes:
        raise AIError(f"{self.label} не умеет делать видео — выберите Pollinations в настройках")


class CloudflareProvider(Provider):
    name = "cloudflare"
    label = "Cloudflare Workers AI"

    def __init__(self, account_id: str, token: str, image_model: str = "flux"):
        self.account_id = account_id.strip()
        self.token = token.strip()
        self.image_model = CF_IMAGE_MODELS.get(image_model, CF_IMAGE_MODELS["flux"])

    def _run(self, model: str, payload: dict, timeout: float = 120) -> tuple[str, bytes]:
        url = f"https://api.cloudflare.com/client/v4/accounts/{urllib.parse.quote(self.account_id)}/ai/run/{model}"
        try:
            _, ctype, body = request(
                url, method="POST", headers={"Authorization": f"Bearer {self.token}"}, json_body=payload, timeout=timeout
            )
        except NetError as exc:
            raise AIError(self._explain(exc)) from exc
        return ctype, body

    @staticmethod
    def _explain(exc: NetError) -> str:
        msg = str(exc)
        low = msg.lower()
        if "neuron" in low or "daily free allocation" in low or "4006" in low:
            return "Закончился бесплатный дневной лимит Cloudflare. Он обновится в 03:00 по Москве (00:00 UTC)."
        if exc.status in (401, 403) or "authentication" in low or "unauthorized" in low:
            return "Cloudflare отклонил доступ: проверьте Account ID и API-токен (токен из шаблона «Workers AI»)."
        if exc.status == 429:
            return "Cloudflare: слишком много запросов, подождите минуту."
        return f"Cloudflare: {msg}"

    def chat(self, system: str, user: str, max_tokens: int = 1500) -> str:
        payload = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "response_format": {"type": "json_object"},
        }
        return self._ask(CF_TEXT_MODEL, payload)

    def _ask(self, model: str, payload: dict) -> str:
        try:
            _, body = self._run(model, payload, timeout=180)
        except AIError as exc:
            if "response_format" not in payload or "лимит" in str(exc):
                raise
            # Если модель не поддерживает JSON-режим, пробуем ещё раз без него.
            payload = {k: v for k, v in payload.items() if k != "response_format"}
            _, body = self._run(model, payload, timeout=180)
        data = parse_json(body)
        result = data.get("result", data) if isinstance(data, dict) else data
        response = result.get("response") if isinstance(result, dict) else result
        if isinstance(response, (dict, list)):
            return json.dumps(response, ensure_ascii=False)
        if not response:
            raise AIError("Cloudflare: языковая модель вернула пустой ответ")
        return str(response)

    def transcribe(self, audio: bytes, duration: float = 0.0) -> Transcript:
        payload = {"audio": base64.b64encode(audio).decode("ascii"), "task": "transcribe", "vad_filter": True}
        _, body = self._run(CF_STT_MODEL, payload, timeout=300)
        data = parse_json(body)
        result = data.get("result", data) if isinstance(data, dict) else data
        return transcript_from(result, duration)

    def look(self, image: bytes, prompt: str) -> str:
        content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": _data_url(image)}}]
        payload = {
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 2500,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        return self._ask(CF_VISION_MODEL, payload)

    def image(self, prompt: str, seed: int) -> bytes:
        if self.image_model == CF_IMAGE_MODELS["sdxl"]:
            payload = {"prompt": prompt, "negative_prompt": NEGATIVE, "width": IMAGE_W, "height": IMAGE_H, "seed": seed}
        else:  # FLUX schnell: только prompt и steps, картинка квадратная 1024x1024
            payload = {"prompt": prompt[:2048], "steps": 6}
        ctype, body = self._run(self.image_model, payload)
        if ctype.startswith("image/"):
            return body
        data = parse_json(body)
        b64 = (data.get("result") or {}).get("image") if isinstance(data, dict) else None
        if not b64:
            raise AIError("Cloudflare не вернул картинку: " + body[:200].decode("utf-8", "replace"))
        return base64.b64decode(b64)


class PollinationsProvider(Provider):
    name = "pollinations"
    label = "Pollinations"
    supports_video = True

    def __init__(self, key: str, image_model: str = "", video_model: str = ""):
        self.key = key.strip()
        self.image_model = image_model.strip()
        self.video_model = video_model.strip()

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    @staticmethod
    def _explain(exc: NetError) -> str:
        if exc.status == 401:
            return "Pollinations отклонил ключ — проверьте его в ⚙ Настройках."
        if exc.status == 402:
            return "На балансе Pollinations не хватает Pollen — пополните баланс на enter.pollinations.ai."
        if exc.status == 403:
            return f"Pollinations: эта модель недоступна для вашего ключа ({exc})."
        if exc.status == 429:
            return "Pollinations: слишком много запросов, подождите минуту."
        return f"Pollinations: {exc}"

    def _get(self, url: str, headers: dict | None = None, timeout: float = 180) -> tuple[int, str, bytes]:
        try:
            return request(url, headers={**self._auth, **(headers or {})}, timeout=timeout)
        except NetError as exc:
            raise AIError(self._explain(exc)) from exc

    def chat(self, system: str, user: str, max_tokens: int = 1500) -> str:
        return self._chat([{"role": "system", "content": system}, {"role": "user", "content": user}])

    def _chat(self, messages: list[dict]) -> str:
        try:
            _, _, body = request(
                f"{POLLINATIONS_BASE}/v1/chat/completions",
                method="POST",
                headers=self._auth,
                json_body={"messages": messages},
                timeout=180,
            )
        except NetError as exc:
            raise AIError(self._explain(exc)) from exc
        data = parse_json(body)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError("Pollinations: неожиданный ответ языковой модели") from exc

    def transcribe(self, audio: bytes, duration: float = 0.0) -> Transcript:
        fields = {"model": POLLINATIONS_STT_MODEL, "response_format": "verbose_json"}
        for attempt in (1, 2):
            data, ctype = multipart(fields, {"file": ("audio.mp3", audio, "audio/mpeg")})
            try:
                _, _, body = request(
                    f"{POLLINATIONS_BASE}/v1/audio/transcriptions",
                    method="POST",
                    headers={**self._auth, "Content-Type": ctype},
                    data=data,
                    timeout=300,
                )
                return transcript_from(parse_json(body), duration)
            except NetError as exc:
                if exc.status == 400 and attempt == 1:  # модель не умеет verbose_json — берём простой текст
                    fields = {"model": POLLINATIONS_STT_MODEL}
                    continue
                raise AIError(self._explain(exc)) from exc
        raise AIError("Pollinations не распознал речь")

    def look(self, image: bytes, prompt: str) -> str:
        content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": _data_url(image)}}]
        return self._chat([{"role": "user", "content": content}])

    def image(self, prompt: str, seed: int) -> bytes:
        params = {"width": IMAGE_W, "height": IMAGE_H, "seed": seed}
        if self.image_model:
            params["model"] = self.image_model
        url = f"{POLLINATIONS_BASE}/image/{urllib.parse.quote(prompt[:1500], safe='')}?{urllib.parse.urlencode(params)}"
        _, ctype, body = self._get(url)
        if not ctype.startswith("image/") and not _image_ext(body):
            raise AIError("Pollinations не вернул картинку: " + body[:200].decode("utf-8", "replace"))
        return body

    def video(self, prompt: str, seconds: int, on_wait: Callable[[str], None] | None = None) -> bytes:
        params = {"duration": seconds, "aspectRatio": "9:16", "audio": "false"}
        if self.video_model:
            params["model"] = self.video_model
        url = f"{POLLINATIONS_BASE}/video/{urllib.parse.quote(prompt[:1500], safe='')}?{urllib.parse.urlencode(params)}"
        status, ctype, body = self._get(url, headers={"Prefer": "respond-async"}, timeout=600)
        if ctype.startswith("video/"):
            return body
        job = parse_json(body)
        job_id = job.get("id") if isinstance(job, dict) else None
        if not job_id:
            raise AIError("Pollinations не принял задачу на видео: " + body[:200].decode("utf-8", "replace"))
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            time.sleep(5)
            _, _, jbody = self._get(f"{POLLINATIONS_BASE}/v1/jobs/{job_id}", timeout=60)
            info = parse_json(jbody)
            state = info.get("status") if isinstance(info, dict) else None
            if on_wait:
                on_wait(f"видео: {state or '…'}")
            if state == "failed":
                raise AIError("Pollinations не смог сделать видео: " + str(info.get("error") or info)[:300])
            if state == "succeeded":
                result_url = info.get("result_url") or f"/v1/jobs/{job_id}/result"
                _, _, video = self._get(urllib.parse.urljoin(POLLINATIONS_BASE + "/", result_url), timeout=300)
                return video
        raise AIError("Pollinations слишком долго делает видео — попробуйте ещё раз позже")


def get_provider(settings: dict | None = None) -> Provider | None:
    s = settings or config.load_settings()
    name = s.get("ai_provider") or ""
    if name == "cloudflare" and s.get("cf_account_id") and s.get("cf_api_token"):
        return CloudflareProvider(s["cf_account_id"], s["cf_api_token"], s.get("cf_image_model") or "flux")
    if name == "pollinations" and s.get("pollinations_key"):
        return PollinationsProvider(s["pollinations_key"], s.get("pollinations_image_model", ""), s.get("pollinations_video_model", ""))
    return None


NO_PROVIDER = (
    "Для ИИ-картинок выберите сервис в ⚙ Настройках: Cloudflare Workers AI (бесплатно) "
    "или Pollinations (платно, умеет видео) и вставьте ключ."
)


# ---------------------------------------------------------------- промты

_STORYBOARD_SYSTEM = (
    "You are a storyboard artist for short vertical videos (TikTok). You receive the voice-over of a video "
    "split into numbered scenes (usually in Russian) and, optionally, the overall idea of the video. For EVERY "
    "scene write ONE prompt for an AI image generator, in English, 25-60 words: a concrete shot that shows the "
    "meaning of that scene - subject, action, setting, lighting, camera angle. Rules: show things that can be "
    "drawn, not abstract words; keep recurring characters and places identical between scenes by repeating "
    "their description; never ask for written text, letters, captions, logos or readable UI in the image; do "
    "not mention an art style, it is added separately. Reply with JSON only, exactly in this shape: "
    '{"prompts": ["scene 1 prompt", "scene 2 prompt"]} with one item per scene, in order.'
)


def write_prompts(provider: Provider | None, scene_texts: list[str], context: str = "") -> tuple[list[str], str | None]:
    """Возвращает промты для сцен и предупреждение, если пришлось обойтись без языковой модели."""
    if not scene_texts:
        return [], None
    if provider is None:
        return list(scene_texts), "Сервис не настроен — в промты подставлен текст сцен. Отредактируйте их или настройте сервис."
    lines = []
    if context.strip():
        lines.append(f"Overall idea of the video: {context.strip()}")
    lines.append(f"Scenes ({len(scene_texts)}):")
    lines += [f"{i}. {t}" for i, t in enumerate(scene_texts, 1)]
    try:
        answer = provider.chat(_STORYBOARD_SYSTEM, "\n".join(lines))
        prompts = parse_prompt_list(answer)
    except (AIError, ValueError) as exc:
        return list(scene_texts), f"Не удалось придумать промты автоматически ({exc}) — в промты подставлен текст сцен."
    if len(prompts) < len(scene_texts):
        prompts += scene_texts[len(prompts):]
    return prompts[: len(scene_texts)], None


def parse_prompt_list(answer: str) -> list[str]:
    """Достаёт список строк из ответа модели (JSON-объект, массив или нумерованный список)."""
    text = answer.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    for candidate in (text, _first_json(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("prompts") or next((v for v in data.values() if isinstance(v, list)), None)
        if isinstance(data, list):
            items = [str(x.get("prompt") if isinstance(x, dict) else x).strip() for x in data]
            items = [x for x in items if x]
            if items:
                return items
    numbered = re.findall(r"^\s*\d+[.)]\s*(.+)$", text, flags=re.M)
    if numbered:
        return [n.strip().strip('"') for n in numbered]
    raise ValueError("ответ модели не похож на список промтов")


def _first_json(text: str) -> str | None:
    m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
    return m.group(1) if m else None


# ---------------------------------------------------------------- генерация


def _image_ext(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def generate_image(provider: Provider, prompt: str, style: str, seed: int | None = None) -> dict:
    """Рисует картинку и кладёт её в библиотеку загрузок. Возвращает описание файла."""
    if not prompt.strip():
        raise AIError("Пустой промт")
    seed = seed if seed is not None else random.randint(1, 2_000_000_000)
    data = provider.image(build_prompt(prompt, style), seed)
    ext = _image_ext(data)
    if not ext:
        raise AIError("Сервис вернул файл, который не похож на картинку")
    name = f"ИИ — {prompt.strip()[:48]}{ext}"
    return library.save_upload(name, io.BytesIO(data), extra={"ai": True, "prompt": prompt, "provider": provider.name})


def video_seconds(scene_seconds: float) -> int:
    for d in VIDEO_DURATIONS:
        if d >= scene_seconds:
            return d
    return VIDEO_DURATIONS[-1]


def generate_video(provider: Provider, prompt: str, style: str, scene_seconds: float = 4.0,
                   on_wait: Callable[[str], None] | None = None) -> dict:
    if not prompt.strip():
        raise AIError("Пустой промт")
    data = provider.video(build_prompt(prompt, style), video_seconds(scene_seconds), on_wait)
    if len(data) < 1000:
        raise AIError("Сервис вернул пустое видео")
    name = f"ИИ-видео — {prompt.strip()[:44]}.mp4"
    return library.save_upload(name, io.BytesIO(data), extra={"ai": True, "prompt": prompt, "provider": provider.name})


def media_ok(item_id: str | None) -> bool:
    if not item_id:
        return False
    try:
        library.get_upload(item_id)
        return True
    except library.LibraryError:
        return False


def sync_scenes(text: str, stored: list[dict]) -> list[dict]:
    """Сцены для текущего текста: делит его заново и переносит промты и
    картинки из сохранённых сцен с тем же текстом."""
    by_text = {" ".join(s.get("text", "").split()): s for s in stored or []}
    out = []
    for sc in split_scenes(text):
        old = by_text.get(" ".join(sc.text.split()), {})
        out.append(
            {
                "text": sc.text,
                "prompt": old.get("prompt", ""),
                "image": old.get("image") if media_ok(old.get("image")) else None,
                "video": old.get("video") if media_ok(old.get("video")) else None,
                "use_video": bool(old.get("use_video")) and media_ok(old.get("video")),
                "seconds": round(sc.seconds, 1),
                "start_char": sc.start_char,
            }
        )
    return out
