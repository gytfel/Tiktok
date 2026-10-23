"""Рендер без браузера.

Примеры:
    python -m studio.cli script.txt
    python -m studio.cli script.txt -o video.mp4 --voice ru-RU-SvetlanaNeural --media bg1.jpg bg2.mp4
    python -m studio.cli --list-voices ru
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import config, library, tts
from .backgrounds import GRADIENT_PRESETS
from .pipeline import RenderContext, RenderError, render


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m studio.cli", description="Видео для TikTok из текста")
    ap.add_argument("text_file", nargs="?", help="файл с текстом (UTF-8); '-' — читать из stdin")
    ap.add_argument("-o", "--output", help="куда сохранить MP4 (по умолчанию workspace/outputs)")
    ap.add_argument("--title", default="", help="заголовок вверху кадра")
    ap.add_argument("--voice", default=tts.DEFAULT_VOICE)
    ap.add_argument("--rate", type=int, default=5, help="скорость речи, %% (-50..100)")
    ap.add_argument("--pitch", type=int, default=0, help="тон, Гц (-50..50)")
    ap.add_argument("--gradient", default="night", choices=sorted(GRADIENT_PRESETS), help="пресет градиента")
    ap.add_argument("--media", nargs="+", help="картинки/видео для фона (вместо градиента)")
    ap.add_argument("--pexels", help="ключевые слова для стоковых видео Pexels, через запятую")
    ap.add_argument("--music", help="фоновая музыка")
    ap.add_argument("--music-volume", type=float, default=0.15)
    ap.add_argument("--words", type=int, default=3, help="слов в одной фразе субтитров")
    ap.add_argument("--font", default=config.DEFAULT_FONT, choices=sorted(config.FONTS))
    ap.add_argument("--no-subtitles", action="store_true")
    ap.add_argument("--quality", default="balanced", choices=["fast", "balanced", "high"])
    ap.add_argument("--list-voices", nargs="?", const="", metavar="LANG", help="показать голоса (например ru)")
    args = ap.parse_args(argv)

    if args.list_voices is not None:
        data = tts.list_voices()
        for v in data["voices"]:
            if v["locale"].lower().startswith(args.list_voices.lower()):
                print(f"{v['id']:<40} {v['gender']:<7} {v['name']}")
        return 0
    if not args.text_file:
        ap.error("укажите файл с текстом")
    text = sys.stdin.read() if args.text_file == "-" else Path(args.text_file).read_text(encoding="utf-8")

    project: dict = {
        "text": text,
        "title": {"text": args.title},
        "voice": {"voice": args.voice, "rate": args.rate, "pitch": args.pitch},
        "subtitles": {"enabled": not args.no_subtitles, "words_per_chunk": args.words, "font": args.font},
        "background": {"type": "gradient", "gradient": GRADIENT_PRESETS[args.gradient]},
        "music": {"file": None, "volume": args.music_volume},
        "export": {"quality": args.quality},
    }
    if args.media:
        ids = [_import_file(Path(m))["id"] for m in args.media]
        project["background"] = {"type": "media", "media": ids}
    elif args.pexels:
        project["background"] = {"type": "pexels", "pexels_query": args.pexels}
    if args.music:
        project["music"]["file"] = _import_file(Path(args.music))["id"]

    config.ensure_dirs()
    job_dir = config.JOBS_DIR / "cli"
    shutil.rmtree(job_dir, ignore_errors=True)

    def progress(stage: str, value: float) -> None:
        bar = "#" * int(value * 30)
        print(f"\r[{bar:<30}] {value * 100:5.1f}%  {stage:<20}", end="", flush=True)

    try:
        result = render(project, RenderContext(job_dir=job_dir, on_log=lambda m: None, on_progress=progress))
    except RenderError as exc:
        print(f"\nОшибка: {exc}", file=sys.stderr)
        return 1
    print()
    produced = config.OUTPUTS_DIR / f"{result['id']}.mp4"
    if args.output:
        shutil.copy2(produced, args.output)
        produced = Path(args.output)
    print(f"Готово: {produced}")
    shutil.rmtree(job_dir, ignore_errors=True)
    return 0


def _import_file(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Файл не найден: {path}")
    with open(path, "rb") as f:
        return library.save_upload(path.name, f)


if __name__ == "__main__":
    raise SystemExit(main())
