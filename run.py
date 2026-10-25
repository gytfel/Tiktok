"""Запуск TikTok Studio: python run.py [--port 8765] [--no-browser] [--phone]"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(description="TikTok Studio — видео из текста с озвучкой и субтитрами")
    parser.add_argument("--host", default=None, help="адрес (по умолчанию только этот компьютер)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    parser.add_argument("--phone", action="store_true", help="разрешить вход с телефона по домашней сети (с кодом доступа)")
    args = parser.parse_args()
    host = args.host or ("0.0.0.0" if args.phone else "127.0.0.1")
    os.environ["TIKTOK_STUDIO_PORT"] = str(args.port)
    if args.phone:
        os.environ["TIKTOK_STUDIO_PHONE"] = "1"

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

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{args.port}"
    print(f"TikTok Studio запущена: {url}  (остановить — Ctrl+C)")
    if args.phone:
        from studio import phone

        urls = phone.phone_urls()
        print("\nВход с телефона (телефон и компьютер — в одной сети Wi-Fi):")
        for u in urls or ["не найден адрес компьютера в сети — проверьте подключение к Wi-Fi"]:
            print("  " + u)
        print(f"Код доступа: {phone.access_key()}")
        print("QR-код — в студии на компьютере, кнопка «📱 Телефон».")
        print("Если Windows спросит про брандмауэр — отметьте «Частные сети» и нажмите «Разрешить доступ».\n")
        url += "/#phone"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("studio.server:app", host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
