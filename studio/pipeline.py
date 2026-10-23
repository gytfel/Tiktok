"""Сборка ролика: текст -> озвучка -> тайминги слов -> субтитры -> фон -> MP4."""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import backgrounds, config, library, pexels, tts
from .ffmpeg_utils import FFmpegError, detect_speech_spans, media_duration, prepare_voice, probe, run_ffmpeg
from .project import normalize
from .subtitles import SubtitleStyle, TitleStyle, build_ass, build_srt
from .text_utils import clean_text, tokenize
from .timing import apply_boundaries, estimate_from_speech

QUALITY = {
    "fast": ["-preset", "veryfast", "-crf", "22"],
    "balanced": ["-preset", "fast", "-crf", "20"],
    "high": ["-preset", "medium", "-crf", "18"],
}


class RenderError(RuntimeError):
    pass


@dataclass
class RenderContext:
    """Связь с внешним миром: лог, прогресс и отмена."""

    job_dir: Path
    on_log: Callable[[str], None] = print
    on_progress: Callable[[str, float], None] = lambda stage, value: None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def log(self, msg: str) -> None:
        self.on_log(msg)

    def progress(self, stage: str, value: float) -> None:
        self.on_progress(stage, max(0.0, min(1.0, value)))

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise RenderError("Отменено пользователем")


@dataclass
class Sources:
    """Файлы для фона и музыки, проверенные до начала озвучки."""

    items: list[backgrounds.MediaItem] = field(default_factory=list)
    pexels_queries: list[str] = field(default_factory=list)
    pexels_key: str = ""
    music_path: Path | None = None


def check_sources(p: dict) -> Sources:
    """Проверяет настройки фона и музыки сразу, чтобы не ждать озвучку ради ошибки в них."""
    b = p["background"]
    src = Sources()
    if b["type"] == "media":
        if not b["media"]:
            raise RenderError(
                "Не выбраны файлы для фона. На вкладке «Фон → Мои фото и видео» загрузите клипы "
                "или фото и проверьте, что они есть в блоке «В ролике». Либо выберите фон «Градиент»."
            )
        try:
            src.items = [library.media_item(mid) for mid in b["media"]]
        except library.LibraryError as exc:
            raise RenderError(str(exc)) from exc
    elif b["type"] == "pexels":
        src.pexels_queries = [q.strip() for q in b["pexels_query"].replace("\n", ",").split(",") if q.strip()]
        if not src.pexels_queries:
            raise RenderError("Для фона «Стоки Pexels» укажите ключевые слова через запятую, например: ночной город, телефон")
        src.pexels_key = config.load_settings().get("pexels_api_key", "")
        if not src.pexels_key:
            raise RenderError(
                "Для фона «Стоки Pexels» нужен бесплатный ключ: получите его на pexels.com/api "
                "и вставьте в ⚙ Настройках. Либо выберите другой фон."
            )
    music = p["music"]
    if music["file"] and music["volume"] > 0:
        try:
            src.music_path = library.audio_path(music["file"])
        except library.LibraryError as exc:
            raise RenderError(f"Музыка: {exc}") from exc
    return src


def render(project: dict, ctx: RenderContext, synthesize: Callable | None = None) -> dict:
    synthesize = synthesize or tts.synthesize
    p = normalize(project)
    text = clean_text(p["text"])
    if not text:
        raise RenderError("Введите текст ролика")
    tokens = tokenize(text)
    if not tokens:
        raise RenderError("В тексте нет слов для озвучки")
    sources = check_sources(p)
    ctx.job_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    # 1. Озвучка
    v = p["voice"]
    ctx.progress("Озвучка текста", 0.02)
    ctx.log(f"Голос: {v['voice']}, скорость {v['rate']:+d}%, тон {v['pitch']:+d}Hz")
    result = synthesize(text, v["voice"], v["rate"], v["pitch"], v["volume"], ctx.job_dir)
    ctx.check_cancel()
    speech = media_duration(result.audio_path)
    ctx.log(f"Длительность озвучки: {speech:.2f} c, меток слов: {len(result.boundaries)}")

    # 2. Тайминги слов
    if result.boundaries:
        apply_boundaries(text, tokens, result.boundaries, speech)
    else:
        ctx.log("Синтезатор не дал меток слов — оцениваю тайминги по паузам в речи")
        estimate_from_speech(tokens, detect_speech_spans(result.audio_path, speech))
    for tok in tokens:
        tok.start = (tok.start or 0.0) + config.LEAD_IN
        tok.end = (tok.end or tok.start) + config.LEAD_IN
    total = round(config.LEAD_IN + speech + config.TAIL, 3)
    ctx.progress("Субтитры", 0.12)

    # 3. Субтитры и заголовок
    s = p["subtitles"]
    style = SubtitleStyle(**{k: s[k] for k in SubtitleStyle.__dataclass_fields__})
    title = TitleStyle(**{k: p["title"][k] for k in TitleStyle.__dataclass_fields__})
    use_ass = s["enabled"] or bool(title.text.strip())
    if use_ass:
        (ctx.job_dir / "subs.ass").write_text(build_ass(tokens, style, total, title, s["enabled"]), encoding="utf-8")
        fonts_dir = ctx.job_dir / "fonts"
        fonts_dir.mkdir(exist_ok=True)
        shutil.copy2(config.FONTS_DIR / config.FONTS[style.font]["file"], fonts_dir)
    srt = build_srt(tokens, style, total)

    # 4. Фон
    b = p["background"]
    ctx.progress("Подготовка фона", 0.15)
    if b["type"] == "gradient":
        image = backgrounds.make_gradient_image(b["gradient"], ctx.job_dir / "gradient.png")
        graph = backgrounds.gradient_graph(image, total, b["gradient_speed"])
    else:
        items = sources.items
        if b["type"] == "pexels":
            try:
                paths = pexels.fetch_clips(sources.pexels_queries, sources.pexels_key, log=ctx.log)
            except pexels.PexelsError as exc:
                raise RenderError(str(exc)) from exc
            items = [backgrounds.MediaItem(path, "video", probe(path)["duration"]) for path in paths]
        segments = backgrounds.plan_segments(items, tokens, total, b["switch"], b["max_segment"])
        ctx.log("Сцены: " + ", ".join(f"{seg.item.path.name} {seg.start:.1f}-{seg.end:.1f}c" for seg in segments))
        graph = backgrounds.media_graph(segments, b["fit"], b["motion"], b["transition"])
    ctx.check_cancel()

    # 5. Звук: голос + музыка
    ctx.progress("Обработка звука", 0.18)
    voice_wav = ctx.job_dir / "voice_ready.wav"
    try:
        prepare_voice(result.audio_path, voice_wav, total, config.LEAD_IN)
    except FFmpegError as exc:
        raise RenderError(f"Не удалось обработать озвучку: {exc}") from exc
    inputs = list(graph.inputs)
    filters = list(graph.filters)
    voice_idx = graph.input_count
    inputs += ["-i", str(voice_wav)]
    audio_out = f"{voice_idx}:a"
    music = p["music"]
    if sources.music_path:
        inputs += ["-stream_loop", "-1", "-i", str(sources.music_path)]
        fade_start = max(0.0, total - 1.5)
        filters.append(
            f"[{voice_idx + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"atrim=0:{total:.3f},volume={music['volume']:.3f},"
            f"afade=t=in:d=0.5,afade=t=out:st={fade_start:.3f}:d=1.5[music]"
        )
        filters.append(f"[{voice_idx}:a][music]amix=inputs=2:duration=first:normalize=0[mix]")
        audio_out = "[mix]"

    # 6. Видео: затемнение + субтитры
    post = []
    if b["dim"] > 0:
        # Умножаем яркость и цветность (как чёрный слой поверх). drawbox с альфой
        # на YUV заметно обесцвечивает картинку, поэтому используем таблицу.
        k = 1 - b["dim"]
        post.append(f"lutyuv=y='16+(val-16)*{k:.3f}':u='128+(val-128)*{k:.3f}':v='128+(val-128)*{k:.3f}'")
    if use_ass:
        post.append("ass=subs.ass:fontsdir=fonts")
    post.append("format=yuv420p")
    filters.append(f"[{graph.output}]" + ",".join(post) + "[vout]")

    out_file = ctx.job_dir / "output.mp4"
    args = [
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", audio_out,
        "-c:v", "libx264", *QUALITY[p["export"]["quality"]],
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-r", str(config.FPS),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        "-t", f"{total:.3f}",
        out_file.name,
    ]
    ctx.progress("Рендер видео", 0.2)
    try:
        run_ffmpeg(
            args,
            cwd=ctx.job_dir,
            duration=total,
            on_progress=lambda f: ctx.progress("Рендер видео", 0.2 + 0.77 * f),
            log=ctx.log,
            cancel_event=ctx.cancel_event,
        )
    except FFmpegError as exc:
        raise RenderError(str(exc)) from exc

    # 7. Сохранение в библиотеку
    hint = p["export"]["name"] or p["title"]["text"] or " ".join(text.split()[:5])
    output_id = library.new_output_id(hint)
    config.ensure_dirs()
    final = config.OUTPUTS_DIR / f"{output_id}.mp4"
    shutil.move(str(out_file), final)
    (config.OUTPUTS_DIR / f"{output_id}.srt").write_text(srt, encoding="utf-8")
    try:
        run_ffmpeg(
            ["-ss", f"{min(1.5, total / 2):.2f}", "-i", str(final), "-frames:v", "1", "-vf", "scale=360:-2",
             str(config.OUTPUTS_DIR / f"{output_id}.jpg")]
        )
    except FFmpegError:
        pass
    meta = {
        "id": output_id,
        "title": p["title"]["text"] or p["export"]["name"] or " ".join(text.split()[:8]),
        "duration": total,
        "created": time.time(),
        "render_seconds": round(time.time() - started, 1),
        "size": final.stat().st_size,
        "project": p,
    }
    library.save_output_meta(output_id, meta)
    ctx.progress("Готово", 1.0)
    ctx.log(f"Готово: {final.name} ({total:.1f} c) за {meta['render_seconds']} c")
    (ctx.job_dir / "project.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    return library.public_output(meta)
