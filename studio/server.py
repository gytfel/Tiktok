"""Веб-интерфейс студии (FastAPI). Запуск: python run.py"""

from __future__ import annotations

import hashlib
import importlib.util
import mimetypes
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from fastapi.staticfiles import StaticFiles

from . import __version__, ai, config, library, phone, reference, tts
from .tunnel import tunnel
from .backgrounds import GRADIENT_PRESETS
from .ffmpeg_utils import capabilities
from .text_utils import clean_text
from .jobs import JobManager
from .pipeline import RenderError, check_sources
from .project import DEFAULT_PROJECT, normalize
from .subtitles import font_height_ratio

config.ensure_dirs()
mimetypes.add_type("application/manifest+json", ".webmanifest")

app = FastAPI(title="TikTok Studio", version=__version__)
app.add_middleware(phone.PhoneGuard)
manager = JobManager(keep_job_dirs=os.environ.get("TIKTOK_STUDIO_KEEP_JOBS") == "1")



@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Вместо безликого «There was an error parsing the body» показываем настоящую причину."""
    detail = exc.detail
    cause = exc.__cause__
    if exc.status_code == 400 and detail == "There was an error parsing the body" and cause is not None:
        detail = f"Сервер не смог прочитать запрос: {type(cause).__name__}: {cause}"
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
app.mount("/fonts", StaticFiles(directory=config.FONTS_DIR), name="fonts")
app.mount("/files/uploads", StaticFiles(directory=config.UPLOADS_DIR), name="uploads")
app.mount("/files/outputs", StaticFiles(directory=config.OUTPUTS_DIR), name="outputs")


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    # Метка версии у app.js и style.css: после обновления студии браузер не возьмёт старые файлы из кэша.
    html = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        digest = hashlib.sha1((config.WEB_DIR / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={digest}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


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
        "server": phone.server_mode(),
        "piper": tts.piper_problem() or "ok",  # что видит сама служба — для tiktok-studio doctor
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
    # Код доступа через веб не меняется: иначе любой, кто вошёл, мог бы выгнать остальных.
    config.save_settings({k: v for k, v in values.items() if k != "phone_key"})
    return _public_settings()


def _is_local_request(request: Request) -> bool:
    """Запрос с этого компьютера (а не с телефона через сеть или туннель). На сервере «своих» нет."""
    if phone.server_mode():
        return False
    return phone.is_local(request.client.host if request.client else "") and not phone.is_proxied(request.scope)


def _require_local(request: Request) -> None:
    if not _is_local_request(request):
        raise HTTPException(403, "Это можно сделать только на компьютере, где запущена студия")


@app.get("/api/phone")
def phone_access(request: Request) -> dict:
    """Ссылка и QR-код для телефона. Код доступа видно только на этом компьютере."""
    local = _is_local_request(request)
    data = {"enabled": phone.enabled(), "remote": not local, "urls": [], "key": "", "qr": None}
    if phone.enabled() and local:
        data["urls"] = phone.phone_urls()
        data["key"] = phone.access_key()
        data["qr"] = phone.qr_svg(data["urls"][0]) if data["urls"] else None
    return data


@app.get("/api/tunnel")
def tunnel_status(request: Request) -> dict:
    data = tunnel.status()
    data["url"] = data["url"] if tunnel.state == "running" or _is_local_request(request) else ""
    if not _is_local_request(request):
        data["detail"] = ""
    if _is_local_request(request) and data["url"]:
        data["phone_url"] = f"{data['url']}/?key={phone.access_key()}"
        data["key"] = phone.access_key()
        data["qr"] = phone.qr_svg(data["phone_url"])
    return data


@app.post("/api/tunnel/start")
def tunnel_start(request: Request) -> dict:
    _require_local(request)
    phone.access_key()  # код создаётся заранее
    tunnel.start(phone.port())
    return tunnel.status()


@app.post("/api/tunnel/stop")
def tunnel_stop(request: Request) -> dict:
    _require_local(request)
    tunnel.stop()
    return tunnel.status()


# ------------------------------------------------------------- голоса


@app.get("/api/voices")
def voices(refresh: bool = False) -> dict:
    data = tts.list_voices(force=refresh)
    if importlib.util.find_spec("pyttsx3") is not None:
        data["voices"] = data["voices"] + tts.list_offline_voices()
    data["voices"] = data["voices"] + tts.piper_list_voices()
    return data


@app.post("/api/voice-preview")
def voice_preview(payload: dict = Body(...)) -> FileResponse:
    p = normalize({"voice": payload})["voice"]
    text = str(payload.get("text") or "Привет! Так будет звучать озвучка твоего видео.")[:300]
    problem = tts.voice_language_problem(text, p["voice"])
    if problem:
        raise HTTPException(400, problem)
    key = hashlib.sha1(f"{p['voice']}|{p['rate']}|{p['pitch']}|{text}".encode()).hexdigest()[:16]
    cache = config.CACHE_DIR / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    for ext, media in ((".mp3", "audio/mpeg"), (".wav", "audio/wav")):
        cached = cache / f"{key}{ext}"
        if cached.exists():
            return FileResponse(cached, media_type=media)
    with tempfile.TemporaryDirectory(dir=config.CACHE_DIR) as tmp:
        try:
            # Без подмены голоса: в прослушивании должен звучать именно выбранный.
            res = tts.synthesize(text, p["voice"], p["rate"], p["pitch"], p["volume"], Path(tmp), allow_fallback=False)
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


# ИИ-видео делается минутами: запрос сразу возвращает номер задачи, а страница
# спрашивает о результате. Иначе через туннель (лимит ответа ~100 с) он бы обрывался.
_ai_tasks: dict[str, dict] = {}
TASK_TTL = 3600  # готовые задачи хранятся час, потом забываются — чтобы память сервера не росла


def _new_task() -> dict:
    now = time.time()
    for tid, old in list(_ai_tasks.items()):
        if old["status"] != "running" and now - old.get("finished", now) > TASK_TTL:
            del _ai_tasks[tid]
    task_id = uuid.uuid4().hex[:12]
    task = _ai_tasks[task_id] = {"id": task_id, "status": "running", "stage": "", "progress": 0, "result": None, "error": None}
    return task


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
    prompt = str(payload.get("prompt") or "")[:1500]
    task = _new_task()

    def work() -> None:
        try:
            task["result"] = ai.generate_video(provider, prompt, style, seconds, on_wait=lambda m: task.update(stage=m))
            task["status"] = "done"
        except Exception as exc:  # показываем пользователю любую причину
            task["error"] = str(exc) if isinstance(exc, ai.AIError) else f"{type(exc).__name__}: {exc}"
            task["status"] = "error"
        task["finished"] = time.time()

    threading.Thread(target=work, daemon=True, name=f"ai-video-{task['id']}").start()
    return task


@app.post("/api/reference")
def reference_start(payload: dict = Body(...)) -> dict:
    """Похожее видео по образцу: скачивает ролик по ссылке (или берёт загруженный),
    разбирает его и возвращает основу проекта. Идёт в фоне, как ИИ-видео."""
    provider = _provider()
    url = str(payload.get("url") or "").strip()
    upload_id = str(payload.get("upload_id") or "").strip()
    # Несколько картинок (скриншоты слайдов) — по порядку.
    image_ids = [str(x) for x in payload.get("upload_ids") or [] if isinstance(x, str)][: reference.MAX_SLIDES]
    if not url and not upload_id and not image_ids:
        raise HTTPException(400, "Загрузите видео-образец или вставьте ссылку на ролик")
    if url and not reference.is_url(url):
        raise HTTPException(400, "Это не похоже на ссылку — скопируйте её целиком, вместе с https://")
    options = {
        "topic": str(payload.get("topic") or "")[:500],
        "mode": str(payload.get("mode") or "adapt"),
        "language": str(payload.get("language") or "ru"),
        "current_voice": str(payload.get("voice") or ""),
    }
    task = _new_task()

    def work() -> None:
        try:
            item_id = upload_id
            if url:
                task.update(stage="Скачиваю видео…")
                item_id = reference.download(url, on_progress=lambda f: task.update(progress=round(f * 15)))["id"]
            elif image_ids:
                task.update(stage="Собираю слайды…")
                item_id = reference.slideshow_from_uploads(image_ids)["id"]

            def on_stage(text: str, frac: float) -> None:
                task.update(stage=text, progress=round(15 + frac * 85))

            task["result"] = reference.analyze(item_id, provider, on_stage=on_stage, **options)
            task["status"] = "done"
        except Exception as exc:  # показываем пользователю любую причину
            if config.is_disk_full(exc):
                task["error"] = config.disk_full_message()
            elif isinstance(exc, (reference.ReferenceError, ai.AIError)):
                task["error"] = str(exc)
            else:
                task["error"] = f"{type(exc).__name__}: {exc}"
            task["status"] = "error"
        task["finished"] = time.time()

    threading.Thread(target=work, daemon=True, name=f"reference-{task['id']}").start()
    return task


@app.get("/api/ai/tasks/{task_id}")
def ai_task(task_id: str) -> dict:
    task = _ai_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена — возможно, студию перезапускали")
    return task


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


@app.post("/api/uploads/raw")
async def upload_raw(request: Request, name: str) -> dict:
    """Загрузка одного файла «как есть» (тело запроса — содержимое файла).
    Пишем сразу в папку студии, без системной временной папки: на Windows её
    иногда блокирует антивирус, и разбор multipart-формы падает."""
    try:
        item_id, path = library.start_upload(name)
    except library.LibraryError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = None
    try:
        # Запись на диск — в отдельном потоке и крупными кусками: иначе большой файл
        # останавливал бы на время загрузки все остальные запросы студии.
        out = await run_in_threadpool(open, path, "wb")
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) >= 4 * 1024 * 1024:
                await run_in_threadpool(out.write, bytes(buf))
                buf.clear()
        if buf:
            await run_in_threadpool(out.write, bytes(buf))
        await run_in_threadpool(out.close)
    except ClientDisconnect:
        if out:
            out.close()
        path.unlink(missing_ok=True)
        raise HTTPException(400, "Загрузка прервалась — попробуйте ещё раз")
    except OSError as exc:
        if out:
            try:
                out.close()
            except OSError:
                pass
        path.unlink(missing_ok=True)
        if config.is_disk_full(exc):
            raise HTTPException(507, config.disk_full_message()) from exc
        raise HTTPException(500, f"Не удалось записать файл в папку студии ({path.parent}): {exc}") from exc
    try:
        return await run_in_threadpool(library.finish_upload, item_id, name, path)
    except library.LibraryError as exc:
        raise HTTPException(400, str(exc)) from exc


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
