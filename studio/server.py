"""Веб-интерфейс студии (FastAPI). Запуск: python run.py"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import tempfile
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, ai, config, library, tts
from .backgrounds import GRADIENT_PRESETS
from .ffmpeg_utils import capabilities
from .text_utils import clean_text
from .jobs import JobManager
from .pipeline import RenderError, check_sources
from .project import DEFAULT_PROJECT, normalize
from .subtitles import font_height_ratio

config.ensure_dirs()

app = FastAPI(title="TikTok Studio", version=__version__)
manager = JobManager(keep_job_dirs=os.environ.get("TIKTOK_STUDIO_KEEP_JOBS") == "1")

app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
app.mount("/fonts", StaticFiles(directory=config.FONTS_DIR), name="fonts")
app.mount("/files/uploads", StaticFiles(directory=config.UPLOADS_DIR), name="uploads")
app.mount("/files/outputs", StaticFiles(directory=config.OUTPUTS_DIR), name="outputs")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------- общее


@app.get("/api/status")
def status() -> dict:
    return {
        "version": __version__,
        "ffmpeg": capabilities(),
        "fonts": [
            {"key": k, "label": v["label"], "file": v["file"], "ratio": font_height_ratio(k)} for k, v in config.FONTS.items()
        ],
        "gradients": GRADIENT_PRESETS,
        "defaults": DEFAULT_PROJECT,
        "settings": _public_settings(),
        "ai_styles": {k: v["label"] for k, v in ai.STYLES.items()},
        "offline_tts": importlib.util.find_spec("pyttsx3") is not None,
    }


def _hint(secret: str) -> str:
    return ("…" + secret[-4:]) if secret else ""


def _public_settings() -> dict:
    """Настройки для интерфейса: секреты не отдаём, только «задан/не задан» и последние символы."""
    s = config.load_settings()
    provider = ai.get_provider(s)
    return {
        "pexels_api_key_set": bool(s.get("pexels_api_key")),
        "pexels_api_key_hint": _hint(s.get("pexels_api_key", "")),
        "proxy": s.get("proxy", ""),
        "ai_provider": s.get("ai_provider", ""),
        "cf_account_id": s.get("cf_account_id", ""),
        "cf_api_token_set": bool(s.get("cf_api_token")),
        "cf_api_token_hint": _hint(s.get("cf_api_token", "")),
        "cf_image_model": s.get("cf_image_model") or "flux",
        "pollinations_key_set": bool(s.get("pollinations_key")),
        "pollinations_key_hint": _hint(s.get("pollinations_key", "")),
        "pollinations_image_model": s.get("pollinations_image_model", ""),
        "pollinations_video_model": s.get("pollinations_video_model", ""),
        "ai_ready": provider is not None,
        "ai_label": provider.label if provider else "",
        "ai_video": bool(provider and provider.supports_video),
    }


@app.get("/api/settings")
def get_settings() -> dict:
    return _public_settings()


@app.post("/api/settings")
def update_settings(values: dict = Body(...)) -> dict:
    config.save_settings(values)
    return _public_settings()


# ------------------------------------------------------------- голоса


@app.get("/api/voices")
def voices(refresh: bool = False) -> dict:
    data = tts.list_voices(force=refresh)
    if importlib.util.find_spec("pyttsx3") is not None:
        data["voices"] = data["voices"] + tts.list_offline_voices()
    return data


@app.post("/api/voice-preview")
def voice_preview(payload: dict = Body(...)) -> FileResponse:
    p = normalize({"voice": payload})["voice"]
    text = str(payload.get("text") or "Привет! Так будет звучать озвучка твоего видео.")[:300]
    key = hashlib.sha1(f"{p['voice']}|{p['rate']}|{p['pitch']}|{text}".encode()).hexdigest()[:16]
    cache = config.CACHE_DIR / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    for ext, media in ((".mp3", "audio/mpeg"), (".wav", "audio/wav")):
        cached = cache / f"{key}{ext}"
        if cached.exists():
            return FileResponse(cached, media_type=media)
    with tempfile.TemporaryDirectory(dir=config.CACHE_DIR) as tmp:
        try:
            res = tts.synthesize(text, p["voice"], p["rate"], p["pitch"], p["volume"], Path(tmp))
        except tts.TTSError as exc:
            raise HTTPException(502, str(exc)) from exc
        target = cache / f"{key}{res.audio_path.suffix}"
        res.audio_path.replace(target)
    return FileResponse(target, media_type="audio/mpeg" if target.suffix == ".mp3" else "audio/wav")


# ------------------------------------------------------------- нейросети


def _provider() -> ai.Provider:
    provider = ai.get_provider()
    if provider is None:
        raise HTTPException(400, ai.NO_PROVIDER)
    return provider


@app.post("/api/ai/scenes")
def ai_scenes(payload: dict = Body(...)) -> dict:
    """Делит текст на сцены и придумывает промты для тех, где их ещё нет
    (или для всех, если rewrite=true)."""
    text = clean_text(str(payload.get("text") or ""))
    if not text:
        raise HTTPException(400, "Введите текст ролика — по нему придумываются сцены")
    stored = normalize({"background": {"ai_scenes": payload.get("scenes") or []}})["background"]["ai_scenes"]
    scenes = ai.sync_scenes(text, stored)
    rewrite = bool(payload.get("rewrite"))
    warning = None
    if rewrite or any(not sc["prompt"].strip() for sc in scenes):
        prompts, warning = ai.write_prompts(ai.get_provider(), [sc["text"] for sc in scenes], str(payload.get("context") or ""))
        for sc, prompt in zip(scenes, prompts):
            if rewrite or not sc["prompt"].strip():
                sc["prompt"] = prompt
    return {"scenes": scenes, "warning": warning}


@app.post("/api/ai/image")
def ai_image(payload: dict = Body(...)) -> dict:
    style = payload.get("style") if payload.get("style") in ai.STYLES else ai.DEFAULT_STYLE
    try:
        return ai.generate_image(_provider(), str(payload.get("prompt") or "")[:1500], style)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/ai/video")
def ai_video(payload: dict = Body(...)) -> dict:
    provider = _provider()
    if not provider.supports_video:
        raise HTTPException(400, f"{provider.label} не умеет делать видео — выберите Pollinations в ⚙ Настройках")
    style = payload.get("style") if payload.get("style") in ai.STYLES else ai.DEFAULT_STYLE
    try:
        seconds = float(payload.get("seconds") or 4)
    except (TypeError, ValueError):
        seconds = 4.0
    try:
        return ai.generate_video(provider, str(payload.get("prompt") or "")[:1500], style, seconds)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc


# ------------------------------------------------------------- файлы


@app.get("/api/uploads")
def uploads() -> list[dict]:
    return library.list_uploads()


@app.post("/api/uploads")
def upload(files: list[UploadFile] = File(...)) -> list[dict]:
    saved, errors = [], []
    for f in files:
        try:
            saved.append(library.save_upload(f.filename or "file", f.file))
        except library.LibraryError as exc:
            errors.append(str(exc))
    if errors and not saved:
        raise HTTPException(400, "; ".join(errors))
    return saved


@app.delete("/api/uploads/{item_id}")
def delete_upload(item_id: str) -> dict:
    try:
        library.delete_upload(item_id)
    except library.LibraryError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


# ------------------------------------------------------------- рендер


@app.post("/api/render")
def start_render(project: dict = Body(...)) -> dict:
    if not str(project.get("text") or "").strip():
        raise HTTPException(400, "Введите текст ролика")
    caps = capabilities()
    if not caps["found"]:
        raise HTTPException(500, "ffmpeg не найден — см. README, раздел «Установка»")
    try:
        check_sources(normalize(project))
    except RenderError as exc:
        raise HTTPException(400, str(exc)) from exc
    return manager.submit(project).public()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return job.public()


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> dict:
    return {"ok": manager.cancel(job_id)}


# ------------------------------------------------------------- готовые видео


@app.get("/api/videos")
def videos() -> list[dict]:
    return library.list_outputs()


@app.get("/api/videos/{output_id}")
def video(output_id: str) -> dict:
    try:
        meta = library.get_output(output_id)
    except library.LibraryError as exc:
        raise HTTPException(404, str(exc)) from exc
    data = library.public_output(meta)
    data["project"] = meta.get("project")
    return data


@app.get("/api/videos/{output_id}/download")
def download(output_id: str) -> FileResponse:
    try:
        meta = library.get_output(output_id)
    except library.LibraryError as exc:
        raise HTTPException(404, str(exc)) from exc
    path = config.OUTPUTS_DIR / f"{meta['id']}.mp4"
    if not path.exists():
        raise HTTPException(404, "Файл видео не найден")
    return FileResponse(path, media_type="video/mp4", filename=f"{meta['id']}.mp4")


@app.delete("/api/videos/{output_id}")
def delete_video(output_id: str) -> dict:
    try:
        library.delete_output(output_id)
    except library.LibraryError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
