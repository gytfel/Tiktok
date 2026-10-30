"""Сценарий по идее: пользователь пишет суть, нейросеть — весь ролик.

Из короткого описания («реклама бота, который сохраняет удалённые сообщения»)
языковая модель пишет текст озвучки нужной длины, разбитый на сцены, к каждой
сцене — описание кадра для нейросети-художника, а ещё заголовок-хук, подпись
к посту и хэштеги. Результат подставляется в проект так же, как основа
«По образцу», и дальше правится как обычно.
"""

from __future__ import annotations

from . import ai
from .reference import BASE_CPS, LANGUAGES, build_project, parse_object, pick_voice
from .text_utils import has_word_chars

DURATIONS = (15, 30, 45, 60)
FORMATS: dict[str, tuple[str, str]] = {
    "auto": ("Сам подберёт", "Choose the format that fits the idea best."),
    "ad": (
        "Реклама / продажа",
        "An ad: open with the viewer's pain or desire, show the product as the solution with 1-2 concrete "
        "benefits, remove the main doubt, end with a clear call to action.",
    ),
    "facts": (
        "Факты / познавательный",
        "An educational video: open with the most surprising fact, then 2-4 short facts building up, end with a "
        "question to the viewers or an invitation to follow for more.",
    ),
    "story": (
        "История",
        "A short story: a relatable character and situation, a twist or conflict, a quick resolution, a takeaway.",
    ),
    "howto": (
        "Лайфхак / инструкция",
        "A how-to: promise the result in the first line, then numbered short steps, end with the result and a call "
        "to save the video.",
    ),
    "motivation": (
        "Мотивация",
        "A motivational video: a strong statement, a short contrast (before/after or most people/you), an "
        "energetic call to act today.",
    ),
    "humor": ("Юмор", "A humorous sketch-like monologue with an absurd but relatable situation and a punchline at the end."),
}

_SYSTEM = (
    "You are an expert scriptwriter for short vertical videos (TikTok, Reels, Shorts) that hold attention to the "
    "end. From the user's idea write a complete video. {format} "
    "Reply with JSON only, exactly in this shape: "
    '{{"hook": "...", "style": "...", "caption": "...", "hashtags": ["#..."], "scenes": [{{"text": "...", "prompt": "..."}}]}}. '
    "Rules. scenes: exactly {n} items, in order. text: the voice-over of the scene in {lang}, about {per} characters "
    "(the whole video is about {chars} characters, {seconds} seconds of speech), natural spoken language, short "
    "sentences, no emojis, no hashtags, no stage directions or speaker names. The first scene is the hook: it must "
    "make the viewer stay within 2 seconds (a sharp question, a surprising claim or a conflict), never a greeting. "
    "{cta} prompt: an English prompt for an AI image generator, 25-60 words, a concrete shot that shows the meaning of "
    "the scene - subject, action, setting, lighting, camera angle; repeat the description of recurring characters so "
    "they stay the same; never ask for written text, letters, captions, logos or readable UI. hook: a short on-screen "
    "title in {lang}, at most 60 characters, that makes people want to watch. style: one of {styles}, whichever suits "
    "the idea. caption: a post description in {lang}, 1-2 sentences. hashtags: 5-8 relevant hashtags in {lang} or "
    "English."
)
_CTA = "The last scene is a call to action: {cta}."
_CTA_DEFAULT = "The last scene ends the video with a call to action that fits the idea (follow, comment, link in profile)."


class StoryError(RuntimeError):
    pass


def scene_count(seconds: int) -> int:
    return max(3, min(12, round(seconds / 4.5)))


def write_story(provider: ai.Provider, idea: str, *, seconds: int = 30, fmt: str = "auto", language: str = "ru",
                cta: str = "", rate: int = 0, current_voice: str = "") -> dict:
    """Пишет ролик по идее. Возвращает изменения проекта и подпись с хэштегами для поста."""
    idea = " ".join(idea.split())[:1500]
    if not has_word_chars(idea):
        raise StoryError("Напишите, о чём должен быть ролик — хотя бы одно предложение")
    seconds = seconds if seconds in DURATIONS else 30
    fmt = fmt if fmt in FORMATS else "auto"
    lang = language if language in LANGUAGES else "ru"
    n = scene_count(seconds)
    speed = BASE_CPS * (1 + max(-50, min(100, rate)) / 100)  # символов в секунду при выбранной скорости голоса
    chars = round(seconds * speed)
    system = _SYSTEM.format(
        format=FORMATS[fmt][1],
        n=n,
        lang=LANGUAGES[lang],
        per=round(chars / n),
        chars=chars,
        seconds=seconds,
        cta=_CTA.format(cta=" ".join(cta.split())[:200]) if cta.strip() else _CTA_DEFAULT,
        styles=", ".join(ai.STYLES),
    )
    data = parse_object(provider.chat(system, f"Idea of the video: {idea}", max_tokens=4000))
    scenes = []
    for item in data.get("scenes") or []:
        if isinstance(item, dict) and has_word_chars(str(item.get("text") or "")):
            scenes.append({"text": " ".join(str(item["text"]).split()), "prompt": " ".join(str(item.get("prompt") or "").split())})
    if not scenes:
        raise ai.AIError("нейросеть не написала текст сцен — попробуйте ещё раз")
    project = build_project(scenes, str(data.get("hook") or ""), str(data.get("style") or ""), idea)
    project["voice"] = {"voice": pick_voice(lang, current_voice or "ru-RU-DmitryNeural")}
    tags = data.get("hashtags") if isinstance(data.get("hashtags"), list) else []
    hashtags = []
    for tag in tags:
        tag = "#" + "".join(str(tag).split()).lstrip("#")
        if len(tag) > 1 and tag not in hashtags:
            hashtags.append(tag[:40])
    return {
        "project": project,
        "caption": " ".join(str(data.get("caption") or "").split())[:500],
        "hashtags": hashtags[:10],
        "seconds": round(len(project["text"]) / speed, 1),
    }
