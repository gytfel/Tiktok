"""Пути, константы и пользовательские настройки приложения."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
WEB_DIR = PACKAGE_DIR / "web"

WORKSPACE_DIR = Path(os.environ.get("TIKTOK_STUDIO_WORKSPACE", PACKAGE_DIR.parent / "workspace")).resolve()
UPLOADS_DIR = WORKSPACE_DIR / "uploads"
OUTPUTS_DIR = WORKSPACE_DIR / "outputs"
JOBS_DIR = WORKSPACE_DIR / "jobs"
CACHE_DIR = WORKSPACE_DIR / "cache"
SETTINGS_FILE = WORKSPACE_DIR / "settings.json"

# Формат TikTok: вертикальное видео 9:16.
WIDTH = 1080
HEIGHT = 1920
FPS = 30

# Тишина в начале и в конце ролика, секунды.
LEAD_IN = 0.25
TAIL = 0.7

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# Шрифты, которые идут вместе с программой (все с кириллицей, лицензия OFL).
# family — имя семейства, по которому шрифт ищет libass; file — файл в assets/fonts.
FONTS: dict[str, dict] = {
    "montserrat-extrabold": {
        "label": "Montserrat ExtraBold",
        "family": "Montserrat ExtraBold",
        "file": "Montserrat-ExtraBold.ttf",
    },
    "montserrat-black": {
        "label": "Montserrat Black",
        "family": "Montserrat Black",
        "file": "Montserrat-Black.ttf",
    },
    "rubik-extrabold": {
        "label": "Rubik ExtraBold",
        "family": "Rubik ExtraBold",
        "file": "Rubik-ExtraBold.ttf",
    },
    "oswald-bold": {
        "label": "Oswald Bold",
        "family": "Oswald",
        "file": "Oswald-Bold.ttf",
        "bold": True,
    },
    "russo-one": {
        "label": "Russo One",
        "family": "Russo One",
        "file": "RussoOne-Regular.ttf",
    },
}
DEFAULT_FONT = "montserrat-extrabold"


def ensure_dirs() -> None:
    for d in (WORKSPACE_DIR, UPLOADS_DIR, OUTPUTS_DIR, JOBS_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


DEFAULT_SETTINGS = {
    # Ключ Pexels для бесплатных стоковых видео (https://www.pexels.com/api/).
    "pexels_api_key": "",
    # Прокси для озвучки и Pexels, например http://127.0.0.1:8080. Пусто — без прокси
    # (переменная окружения HTTPS_PROXY тоже учитывается).
    "proxy": "",
    # Нейросети для картинок и видео: "", "cloudflare" или "pollinations".
    "ai_provider": "",
    "cf_account_id": "",
    "cf_api_token": "",
    "cf_image_model": "flux",  # flux | sdxl
    "pollinations_key": "",
    "pollinations_image_model": "",  # пусто — модель сервиса по умолчанию
    "pollinations_video_model": "",
}

_settings_lock = threading.Lock()


def load_settings() -> dict:
    with _settings_lock:
        data = dict(DEFAULT_SETTINGS)
        if SETTINGS_FILE.exists():
            try:
                data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return data


def save_settings(values: dict) -> dict:
    current = load_settings()
    for key in DEFAULT_SETTINGS:
        if key in values and isinstance(values[key], str):
            current[key] = values[key].strip()
    with _settings_lock:
        ensure_dirs()
        SETTINGS_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def get_proxy() -> str | None:
    proxy = load_settings().get("proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    return proxy or None
