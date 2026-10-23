"""Бесплатные стоковые видео с Pexels (нужен бесплатный API-ключ: https://www.pexels.com/api/)."""

from __future__ import annotations

import json
import random
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import config
from .net import USER_AGENT, opener

API_URL = "https://api.pexels.com/videos/search"


class PexelsError(RuntimeError):
    pass


def search_videos(query: str, api_key: str, per_page: int = 15) -> list[dict]:
    params = urllib.parse.urlencode(
        {"query": query, "orientation": "portrait", "size": "medium", "per_page": per_page, "locale": "ru-RU"}
    )
    req = urllib.request.Request(f"{API_URL}?{params}", headers={"Authorization": api_key, "User-Agent": USER_AGENT})
    try:
        with opener().open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PexelsError("Pexels отклонил ключ API — проверьте его в настройках") from exc
        raise PexelsError(f"Ошибка Pexels: HTTP {exc.code}") from exc
    except OSError as exc:
        raise PexelsError(f"Нет связи с Pexels: {exc}") from exc
    return data.get("videos", [])


def _best_file(video: dict) -> dict | None:
    files = [f for f in video.get("video_files", []) if f.get("link") and (f.get("file_type") or "").endswith("mp4")]
    portrait = [f for f in files if (f.get("height") or 0) >= (f.get("width") or 0)]
    pool = portrait or files
    if not pool:
        return None
    # Ближайшее к 1920 по высоте, но не меньше 1280, если есть.
    good = [f for f in pool if (f.get("height") or 0) >= 1280]
    pool = good or pool
    return min(pool, key=lambda f: abs((f.get("height") or 0) - config.HEIGHT))


def fetch_clips(
    queries: list[str],
    api_key: str,
    log: Callable[[str], None] | None = None,
    seed: int | None = None,
) -> list[Path]:
    """Для каждого запроса скачивает по одному подходящему клипу (с кэшем)."""
    if not api_key:
        raise PexelsError("Укажите ключ Pexels API в настройках (это бесплатно)")
    rng = random.Random(seed)
    cache = config.CACHE_DIR / "pexels"
    cache.mkdir(parents=True, exist_ok=True)
    used_ids: set[int] = set()
    paths: list[Path] = []
    for q in queries:
        videos = [v for v in search_videos(q, api_key) if v.get("id") not in used_ids]
        if not videos:
            if log:
                log(f"Pexels: по запросу «{q}» ничего не найдено")
            continue
        # Берём один из первых результатов, чтобы ролики не повторялись.
        video = rng.choice(videos[: min(5, len(videos))])
        used_ids.add(video["id"])
        f = _best_file(video)
        if not f:
            continue
        target = cache / f"{video['id']}_{f.get('height', 0)}.mp4"
        if not target.exists():
            if log:
                log(f"Pexels: скачиваю «{q}» ({f.get('width')}x{f.get('height')})")
            tmp = target.with_suffix(".part")
            req = urllib.request.Request(f["link"], headers={"User-Agent": USER_AGENT})
            try:
                with opener().open(req, timeout=120) as resp, open(tmp, "wb") as out:
                    shutil.copyfileobj(resp, out)
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                raise PexelsError(f"Не удалось скачать видео с Pexels: {exc}") from exc
            tmp.replace(target)
        paths.append(target)
    if not paths:
        raise PexelsError("Pexels не вернул ни одного видео — попробуйте другие ключевые слова")
    return paths
