"""Озвучка Edge TTS без сети: деление на части, повторы при сбоях, проверка языка голоса."""

import pytest

from studio import tts
from studio.pipeline import RenderError, check_sources
from studio.project import normalize
from studio.timing import Boundary


class NoAudioReceived(Exception):
    """Как edge_tts.exceptions.NoAudioReceived (сравнивается по имени класса)."""


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)


def fake_stream(fail_plan: dict):
    """Возвращает подмену _edge_stream: fail_plan[текст части] — сколько раз упасть."""
    calls = []

    async def stream(text, voice, rate, pitch, volume):
        calls.append(text)
        if fail_plan.get(text, 0) > 0:
            fail_plan[text] -= 1
            raise NoAudioReceived("No audio was received.")
        words = text.split()
        data = b"\xff" * int(tts.MP3_BYTES_PER_SECOND * 0.3 * len(words))  # 0.3 с на слово
        return data, [Boundary(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(words)]

    return stream, calls


def test_split_merges_short_paragraphs_and_drops_wordless():
    chunks = tts.split_for_tts("Привет!\n\nКак дела?\n\n—\n\n...")
    assert chunks == ["Привет!\nКак дела?"]


def test_split_long_text_into_bounded_chunks():
    text = " ".join(["Это предложение для проверки деления длинного текста."] * 40)
    chunks = tts.split_for_tts(text, max_chars=200)
    assert len(chunks) > 5 and all(len(c) <= 200 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_chunks_are_stitched_with_offsets(tmp_path, monkeypatch):
    stream, calls = fake_stream({})
    monkeypatch.setattr(tts, "_edge_stream", stream)
    monkeypatch.setattr(tts, "CHUNK_CHARS", 30)
    monkeypatch.setattr(tts, "split_for_tts", lambda text, max_chars=30: ["раз два три", "четыре пять"])
    res = tts.edge_synthesize("раз два три\n\nчетыре пять", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path / "v.mp3")
    assert [b.text for b in res.boundaries] == ["раз", "два", "три", "четыре", "пять"]
    assert res.boundaries[3].start == pytest.approx(0.9)  # вторая часть сдвинута на длину первой
    assert (tmp_path / "v.mp3").stat().st_size == int(tts.MP3_BYTES_PER_SECOND * 0.3 * 3) + int(tts.MP3_BYTES_PER_SECOND * 0.3 * 2)


def test_retries_only_the_failed_chunk(tmp_path, monkeypatch):
    stream, calls = fake_stream({"четыре пять": 3})
    monkeypatch.setattr(tts, "_edge_stream", stream)
    monkeypatch.setattr(tts, "split_for_tts", lambda text, max_chars=0: ["раз два три", "четыре пять"])
    tts.edge_synthesize("x", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path / "v.mp3")
    assert calls.count("раз два три") == 1 and calls.count("четыре пять") == 4


def test_persistent_no_audio_names_the_phrase(tmp_path, monkeypatch):
    stream, _ = fake_stream({"Ссылка в профиле!": 99})
    monkeypatch.setattr(tts, "_edge_stream", stream)
    with pytest.raises(tts.TTSError, match="«Ссылка в профиле!»"):
        tts.edge_synthesize("Ссылка в профиле!", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path / "v.mp3")


def test_network_errors_are_reported_as_connection_problem(tmp_path, monkeypatch):
    async def stream(*args):
        raise OSError("Cannot connect to host speech.platform.bing.com")

    monkeypatch.setattr(tts, "_edge_stream", stream)
    with pytest.raises(tts.TTSError, match="Не удалось связаться"):
        tts.edge_synthesize("Привет", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path / "v.mp3")


@pytest.mark.parametrize(
    "voice,ok",
    [
        ("ru-RU-DmitryNeural", True),
        ("uk-UA-OstapNeural", True),
        ("en-US-AndrewMultilingualNeural", True),
        ("system:HKEY_LOCAL_MACHINE\\Irina", True),
        ("en-US-GuyNeural", False),
        ("ja-JP-NanamiNeural", False),
    ],
)
def test_voice_language_problem(voice, ok):
    assert (tts.voice_language_problem("Собеседник удалил сообщение", voice) is None) is ok
    assert tts.voice_language_problem("Hello world", voice) is None


def test_render_rejects_english_voice_for_russian_text_before_tts():
    with pytest.raises(RenderError, match="не умеет читать русский"):
        check_sources(normalize({"text": "Привет, это ролик", "voice": {"voice": "en-US-GuyNeural"}}))
