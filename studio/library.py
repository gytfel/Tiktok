"""Хранилище загруженных файлов (картинки, видео, музыка) и готовых роликов."""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from . import config
from .backgrounds import MediaItem
from .ffmpeg_utils import FFmpegError, probe, run_ffmpeg

_ID_RE = re.compile(r"^[a-z0-9_\-]{4,80}$")


class LibraryError(ValueError):
    pass


def kind_of(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext in config.IMAGE_EXTENSIONS:
        return "image"
    if ext in config.VIDEO_EXTENSIONS:
        return "video"
    if ext in config.AUDIO_EXTENSIONS:
        return "audio"
    return None


def _check_id(item_id: str) -> str:
    if not isinstance(item_id, str) or not _ID_RE.match(item_id):
        raise LibraryError("Некорректный идентификатор файла")
    return item_id


# ------------------------------------------------------------- загрузки


def save_upload(filename: str, stream: BinaryIO) -> dict:
    config.ensure_dirs()
    kind = kind_of(filename)
    if not kind:
        raise LibraryError(f"Формат файла не поддерживается: {filename}")
    ext = Path(filename).suffix.lower()
    item_id = uuid.uuid4().hex[:12]
    path = config.UPLOADS_DIR / f"{item_id}{ext}"
    with open(path, "wb") as out:
        shutil.copyfileobj(stream, out, length=1024 * 1024)
    meta = {
        "id": item_id,
        "name": Path(filename).name[:120],
        "kind": kind,
        "ext": ext,
        "size": path.stat().st_size,
        "created": time.time(),
        "duration": None,
        "width": None,
        "height": None,
        "thumb": None,
    }
    try:
        info = probe(path)
        meta.update({k: info[k] for k in ("duration", "width", "height")})
        if kind in ("image", "video") and not info["has_video"]:
            raise LibraryError("Файл повреждён или не распознан")
        if kind == "video":
            thumb = config.UPLOADS_DIR / f"{item_id}.thumb.jpg"
            seek = min(1.0, (info["duration"] or 0) / 3)
            run_ffmpeg(["-ss", f"{seek:.2f}", "-i", str(path), "-frames:v", "1", "-vf", "scale=240:-2", str(thumb)])
            meta["thumb"] = thumb.name
    except LibraryError:
        path.unlink(missing_ok=True)
        raise
    except FFmpegError:
        pass
    (config.UPLOADS_DIR / f"{item_id}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return public_upload(meta)


def public_upload(meta: dict) -> dict:
    out = dict(meta)
    out["url"] = f"/files/uploads/{meta['id']}{meta['ext']}"
    out["thumb_url"] = f"/files/uploads/{meta['thumb']}" if meta.get("thumb") else (out["url"] if meta["kind"] == "image" else None)
    return out


def get_upload(item_id: str) -> tuple[dict, Path]:
    _check_id(item_id)
    meta_path = config.UPLOADS_DIR / f"{item_id}.json"
    if not meta_path.exists():
        raise LibraryError("Файл не найден — возможно, он был удалён")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    path = config.UPLOADS_DIR / f"{item_id}{meta['ext']}"
    if not path.exists():
        raise LibraryError(f"Файл «{meta.get('name')}» не найден на диске")
    return meta, path


def list_uploads() -> list[dict]:
    config.ensure_dirs()
    items = []
    for meta_path in config.UPLOADS_DIR.glob("*.json"):
        try:
            items.append(public_upload(json.loads(meta_path.read_text(encoding="utf-8"))))
        except (ValueError, KeyError, OSError):
            continue
    items.sort(key=lambda m: m.get("created", 0), reverse=True)
    return items


def delete_upload(item_id: str) -> None:
    _check_id(item_id)
    for p in config.UPLOADS_DIR.glob(f"{item_id}*"):
        p.unlink(missing_ok=True)


def media_item(item_id: str) -> MediaItem:
    meta, path = get_upload(item_id)
    if meta["kind"] not in ("image", "video"):
        raise LibraryError(f"«{meta['name']}» — не картинка и не видео")
    return MediaItem(path=path, kind=meta["kind"], duration=meta.get("duration"))


def audio_path(item_id: str) -> Path:
    meta, path = get_upload(item_id)
    if meta["kind"] != "audio":
        raise LibraryError(f"«{meta['name']}» — не аудиофайл")
    return path


# --------------------------------------------------------- готовые ролики

_TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
         "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
    )
)


def slugify(text: str, limit: int = 40) -> str:
    out = []
    for ch in text.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug[:limit].strip("-")


def new_output_id(hint: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    slug = slugify(hint)
    return f"{stamp}_{slug}" if slug else stamp


def save_output_meta(output_id: str, meta: dict) -> None:
    (config.OUTPUTS_DIR / f"{output_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def public_output(meta: dict) -> dict:
    oid = meta["id"]
    out = {k: v for k, v in meta.items() if k != "project"}
    out["url"] = f"/files/outputs/{oid}.mp4"
    out["download_url"] = f"/api/videos/{oid}/download"
    out["thumb_url"] = f"/files/outputs/{oid}.jpg" if (config.OUTPUTS_DIR / f"{oid}.jpg").exists() else None
    out["srt_url"] = f"/files/outputs/{oid}.srt" if (config.OUTPUTS_DIR / f"{oid}.srt").exists() else None
    return out


def list_outputs() -> list[dict]:
    config.ensure_dirs()
    items = []
    for meta_path in config.OUTPUTS_DIR.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if (config.OUTPUTS_DIR / f"{meta.get('id')}.mp4").exists():
            items.append(public_output(meta))
    items.sort(key=lambda m: m.get("created", 0), reverse=True)
    return items


def get_output(output_id: str) -> dict:
    _check_id(output_id)
    meta_path = config.OUTPUTS_DIR / f"{output_id}.json"
    if not meta_path.exists():
        raise LibraryError("Видео не найдено")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def delete_output(output_id: str) -> None:
    _check_id(output_id)
    for ext in (".mp4", ".json", ".jpg", ".srt"):
        (config.OUTPUTS_DIR / f"{output_id}{ext}").unlink(missing_ok=True)
