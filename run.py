"""Запуск TikTok Studio: python run.py [--port 8765] [--no-browser]"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(description="TikTok Studio — видео из текста с озвучкой и субтитрами")
    parser.add_argument("--host", default="127.0.0.1", help="адрес (по умолчанию только этот компьютер)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        sys.exit("Не установлены зависимости. Выполните: pip install -r requirements.txt")

    from studio.ffmpeg_utils import capabilities

    caps = capabilities()
    if not caps["found"]:
        print("ВНИМАНИЕ: ffmpeg не найден. Установите его или выполните: pip install imageio-ffmpeg")
    elif not caps["ok"]:
        missing = [k for k, ok in caps["filters"].items() if not ok]
        print(f"ВНИМАНИЕ: в вашем ffmpeg ({caps['path']}) нет нужных компонентов: {', '.join(missing) or 'libx264'}")

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"TikTok Studio запущена: {url}  (остановить — Ctrl+C)")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("studio.server:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
