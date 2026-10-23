from pathlib import Path

import pytest

from studio import config
from studio.backgrounds import MediaItem, plan_segments
from studio.pipeline import RenderError, check_sources
from studio.project import normalize
from studio.subtitles import SubtitleStyle, TitleStyle, ass_color, ass_tag_color, build_ass, build_chunks, build_srt, layout_lines
from studio.text_utils import tokenize


def timed(text: str, step: float = 0.3):
    tokens = tokenize(text)
    for i, t in enumerate(tokens):
        t.start, t.end = i * step, i * step + step * 0.8
    return tokens


def test_colors():
    assert ass_color("#FFE600") == "&H0000E6FF"
    assert ass_color("#000000", 0.6) == "&H66000000"
    assert ass_tag_color("#FFE600") == "&H00E6FF&"


def test_chunks_respect_word_limit_and_punctuation():
    tokens = timed("Раз два три четыре пять, шесть. Семь восемь")
    style = SubtitleStyle(words_per_chunk=3)
    chunks = build_chunks(tokens, 3, 920, style, total=5)
    texts = [" ".join(t.text for t in c.tokens) for c in chunks]
    assert texts == ["Раз два три", "четыре пять,", "шесть.", "Семь восемь"]
    # Фразы идут подряд без «дырок» и наложений.
    for a, b in zip(chunks, chunks[1:]):
        assert a.end <= b.start + 1e-9


def test_layout_balances_lines():
    lines, scale = layout_lines("пять фактов которые взорвут мозг".split(), "montserrat-extrabold", 66, 600, max_lines=3)
    assert len(lines) >= 2 and scale == 1.0
    widths = [len(" ".join("пять фактов которые взорвут мозг".split()[i] for i in line)) for line in lines]
    assert max(widths) - min(widths) <= 12


def test_long_word_is_scaled_down():
    _, scale = layout_lines(["СВЕРХДЛИННОЕНЕПРОИЗНОСИМОЕСЛОВО"], "montserrat-extrabold", 90, 920)
    assert 0 < scale < 1


def test_build_ass_has_one_event_per_word_and_title():
    tokens = timed("Привет мир. Как дела?")
    ass = build_ass(tokens, SubtitleStyle(), 3.0, TitleStyle(text="Заголовок"))
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == 1 + 4
    assert "Style: Title" in ass and "Montserrat ExtraBold" in ass
    assert "ПРИВЕТ" in ass  # заглавные по умолчанию
    assert "{\\1c&H00E6FF&}" in ass


def test_srt_export():
    srt = build_srt(timed("Привет мир. Как дела?"), SubtitleStyle(), 3.0)
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert "Как дела?" in srt


def test_plan_segments_snaps_to_sentences():
    tokens = timed("Один два три. Четыре пять шесть. Семь восемь девять. Десять.", step=0.5)
    items = [MediaItem(Path("a.jpg"), "image"), MediaItem(Path("b.jpg"), "image")]
    segs = plan_segments(items, tokens, total=6.0, mode="auto", max_segment=0)
    assert len(segs) == 2
    assert segs[0].start == 0 and segs[-1].end == 6.0
    # Разрез ближе к середине — на начале предложения «Семь» (3.0 c) с небольшим запасом
    assert abs(segs[1].start - (3.0 - 0.08)) < 1e-6


def test_plan_segments_cycles_and_continues_video():
    tokens = timed(" ".join(["слово"] * 40), step=0.5)
    items = [MediaItem(Path("v.mp4"), "video", duration=4.0), MediaItem(Path("p.jpg"), "image")]
    segs = plan_segments(items, tokens, total=20.0, mode="auto", max_segment=5)
    assert len(segs) == 4
    assert [s.item.kind for s in segs] == ["video", "image", "video", "image"]
    assert segs[2].offset == segs[0].length % 4.0  # видео продолжается, а не начинается заново


def test_single_long_video_is_not_cut():
    tokens = timed("раз два три")
    segs = plan_segments([MediaItem(Path("v.mp4"), "video", duration=60)], tokens, total=30, max_segment=5)
    assert len(segs) == 1


def test_paragraph_mode():
    tokens = tokenize("Раз два.\n\nТри четыре.\n\nПять шесть.")
    for i, t in enumerate(tokens):
        t.start, t.end = i * 1.0, i * 1.0 + 0.8
    items = [MediaItem(Path(f"{i}.jpg"), "image") for i in range(3)]
    segs = plan_segments(items, tokens, total=6.5, mode="paragraphs")
    assert [round(s.start, 2) for s in segs] == [0, 1.92, 3.92]


def test_normalize_clamps_and_fills_defaults():
    p = normalize({"voice": {"rate": 500}, "subtitles": {"size": "abc", "font": "nope", "color": "red"}, "junk": 1})
    assert p["voice"]["rate"] == 100
    assert p["subtitles"]["size"] == 90
    assert p["subtitles"]["font"] == "montserrat-extrabold"
    assert p["subtitles"]["color"] == "#FFFFFF"
    assert "junk" not in p
    assert p["background"]["type"] == "gradient"


def test_check_sources_reports_background_problems_before_tts(monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda: {"pexels_api_key": ""})
    with pytest.raises(RenderError, match="Не выбраны файлы для фона"):
        check_sources(normalize({"background": {"type": "media", "media": []}}))
    with pytest.raises(RenderError, match="ключевые слова"):
        check_sources(normalize({"background": {"type": "pexels", "pexels_query": " , "}}))
    with pytest.raises(RenderError, match="бесплатный ключ"):
        check_sources(normalize({"background": {"type": "pexels", "pexels_query": "город"}}))
    with pytest.raises(RenderError, match="Музыка"):
        check_sources(normalize({"music": {"file": "deadbeef0000", "volume": 0.2}}))
    assert check_sources(normalize({})).items == []  # градиент без музыки — всё в порядке
