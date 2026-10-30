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


# ---------------------------------------------------------------- Piper


def fake_piper(monkeypatch, calls):
    """Пакет piper: модель «говорит» по 100 мс тишины на символ."""
    import sys
    import types

    class SynthesisConfig:
        def __init__(self, **kw):
            self.kw = kw

    class Voice:
        def synthesize_wav(self, text, wav, syn_config=None):
            calls.append(("say", text, syn_config.kw))
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\x00\x00" * 2205 * len(text))

    class PiperVoice:
        @staticmethod
        def load(path):
            calls.append(("load", str(path)))
            return Voice()

    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=PiperVoice, SynthesisConfig=SynthesisConfig))
    monkeypatch.setattr(tts, "piper_available", lambda: True)
    monkeypatch.setattr(tts, "_piper_loaded", {})


@pytest.fixture
def piper_cache(tmp_path, monkeypatch):
    from studio import config

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    folder = tmp_path / "cache" / "piper"
    folder.mkdir(parents=True)
    for v in tts.PIPER_VOICES:
        code = v["id"].removeprefix("piper:")
        (folder / f"{code}.onnx").write_bytes(b"model")
        (folder / f"{code}.onnx.json").write_text("{}")
    return folder


def test_piper_voice_is_used_directly_with_rate_and_cached(tmp_path, monkeypatch, piper_cache):
    calls = []
    fake_piper(monkeypatch, calls)
    res = tts.synthesize("Привет мир", "piper:ru_RU-irina-medium", 25, 0, 0, tmp_path / "a")
    tts.synthesize("Ещё раз", "piper:ru_RU-irina-medium", 0, 0, 0, tmp_path / "b")
    assert res.audio_path.suffix == ".wav" and res.boundaries == [] and res.note == ""
    assert [c[0] for c in calls] == ["load", "say", "say"]  # модель загружена один раз
    assert calls[1][2]["length_scale"] == pytest.approx(0.8)  # +25% скорости


def test_blocked_edge_falls_back_to_piper_same_gender(tmp_path, monkeypatch, piper_cache):
    calls = []
    fake_piper(monkeypatch, calls)

    def blocked(*a, **kw):
        raise tts.TTSError("нет звука", blocked=True)

    monkeypatch.setattr(tts, "edge_synthesize", blocked)
    res = tts.synthesize("Привет", "ru-RU-SvetlanaNeural", 0, 0, 0, tmp_path)
    assert "ru_RU-irina-medium" in calls[0][1] and "Ирина (Piper)" in res.note
    with pytest.raises(tts.TTSError):  # прослушивание голоса — без подмены
        tts.synthesize("Привет", "ru-RU-SvetlanaNeural", 0, 0, 0, tmp_path, allow_fallback=False)


def test_network_errors_do_not_switch_to_piper(tmp_path, monkeypatch, piper_cache):
    fake_piper(monkeypatch, [])

    def offline(*a, **kw):
        raise tts.TTSError("Не удалось связаться с сервисом")

    monkeypatch.setattr(tts, "edge_synthesize", offline)
    with pytest.raises(tts.TTSError, match="связаться"):
        tts.synthesize("Привет", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path)


def test_persistent_no_audio_is_marked_blocked(tmp_path, monkeypatch):
    stream, _ = fake_stream({"Раз два.": 99})
    monkeypatch.setattr(tts, "_edge_stream", stream)
    with pytest.raises(tts.TTSError) as err:
        tts.edge_synthesize("Раз два.", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path / "v.mp3", attempts=2)
    assert err.value.blocked and "Piper" in str(err.value)


def test_piper_model_download(tmp_path, monkeypatch):
    import io

    from studio import config, net

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    urls = []

    class Opener:
        def open(self, req, timeout=0):
            urls.append(req.full_url)
            if "broken" in req.full_url:
                raise OSError("connection reset")
            body = io.BytesIO(b"{}" if req.full_url.split("?")[0].endswith(".json") else b"onnx")
            body.__enter__ = lambda: body
            return body

    monkeypatch.setattr(net, "opener", lambda: Opener())
    model = tts.piper_model("uk_UA-ukrainian_tts-medium")
    assert model.read_bytes() == b"onnx" and model.with_suffix(".onnx.json").read_bytes() == b"{}"
    assert urls[1] == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/uk/uk_UA/ukrainian_tts/medium/"
        "uk_UA-ukrainian_tts-medium.onnx?download=true"
    )
    with pytest.raises(tts.TTSError):
        tts.piper_model("../../etc-passwd-x")


def test_piper_fallback_and_language_checks(monkeypatch):
    monkeypatch.setattr(tts, "piper_available", lambda: True)
    assert tts.piper_fallback("ru-RU-DmitryNeural")["id"] == "piper:ru_RU-dmitri-medium"
    assert tts.piper_fallback("en-US-AvaMultilingualNeural")["id"] == "piper:en_US-amy-medium"
    assert tts.piper_fallback("de-DE-ConradNeural") is None
    assert tts.voice_language_problem("Привет, мир", "piper:ru_RU-dmitri-medium") is None
    assert tts.voice_language_problem("Привет, мир", "piper:en_US-ryan-high")
    monkeypatch.setattr(tts, "piper_available", lambda: False)
    assert tts.piper_list_voices() == [] and tts.piper_fallback("ru-RU-DmitryNeural") is None


def test_failed_piper_fallback_shows_both_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "piper_available", lambda: True)

    def blocked(*a, **kw):
        raise tts.TTSError("Microsoft не прислал звук", blocked=True)

    def piper_broken(*a, **kw):
        raise tts.TTSError("Не удалось скачать голос Piper (ru_RU-dmitri-medium) с huggingface.co: 403")

    monkeypatch.setattr(tts, "edge_synthesize", blocked)
    monkeypatch.setattr(tts, "piper_synthesize", piper_broken)
    with pytest.raises(tts.TTSError) as err:
        tts.synthesize("Привет", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path)
    assert "Microsoft не прислал звук" in str(err.value) and "Piper тоже не сработал" in str(err.value)
    assert "huggingface.co: 403" in str(err.value)

    monkeypatch.setattr(tts, "piper_available", lambda: False)
    monkeypatch.setattr(tts, "piper_problem", lambda: "пакет piper-tts не установлен")
    monkeypatch.delenv("TIKTOK_STUDIO_SERVER", raising=False)
    with pytest.raises(tts.TTSError, match="на этом компьютере нет.*VPN"):  # на компьютере — про VPN и start.bat
        tts.synthesize("Привет", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path)
    monkeypatch.setenv("TIKTOK_STUDIO_SERVER", "1")
    with pytest.raises(tts.TTSError, match="на сервере недоступен.*не установлен.*tiktok-studio update"):
        tts.synthesize("Привет", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path)


def test_piper_model_errors_become_tts_errors(tmp_path, monkeypatch, piper_cache):
    import sys
    import types

    class Voice:
        def synthesize_wav(self, text, wav, syn_config=None):
            raise RuntimeError("onnxruntime: out of memory")

    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(
        PiperVoice=types.SimpleNamespace(load=lambda path: Voice()), SynthesisConfig=lambda **kw: kw))
    monkeypatch.setattr(tts, "piper_available", lambda: True)
    monkeypatch.setattr(tts, "_piper_loaded", {})
    with pytest.raises(tts.TTSError, match="RuntimeError: onnxruntime"):
        tts.piper_synthesize("Привет", "ru_RU-dmitri-medium", 0, 0, tmp_path / "v.wav")


def picky_stream(refuse):
    """Подмена _edge_stream: молчит (NoAudioReceived) для текстов, где refuse(text) — истина."""
    calls = []

    async def stream(text, voice, rate, pitch, volume):
        calls.append(text)
        if refuse(text):
            raise NoAudioReceived("No audio was received.")
        words = text.split()
        data = b"\xff" * int(tts.MP3_BYTES_PER_SECOND * 0.3 * len(words))
        return data, [Boundary(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(words)]

    return stream, calls


def test_one_bad_symbol_is_dropped_instead_of_failing(tmp_path, monkeypatch):
    stream, calls = picky_stream(lambda t: "‼" in t)
    monkeypatch.setattr(tts, "_edge_stream", stream)
    text = "Телеграм бот хранит все сообщения\n\nБот также делает экспорт ‼ быстро."
    res = tts.edge_synthesize(text, "ru-RU-DmitryNeural", 0, 0, 0, tmp_path / "v.mp3")
    assert "Проверка связи." in calls  # сначала убедились, что Microsoft вообще работает
    assert "Бот также делает экспорт быстро." in calls  # затем повторили строку без значка
    words = [b.text for b in res.boundaries]
    assert words[:5] == ["Телеграм", "бот", "хранит", "все", "сообщения"] and words[-1] == "быстро."
    starts = [b.start for b in res.boundaries]
    assert starts == sorted(starts) and res.boundaries[5].start == pytest.approx(5 * 0.3)


def test_blocked_service_is_not_split_into_pieces(tmp_path, monkeypatch):
    stream, calls = picky_stream(lambda t: True)
    monkeypatch.setattr(tts, "_edge_stream", stream)
    with pytest.raises(tts.TTSError) as err:
        tts.edge_synthesize("Первая строка.\nВторая строка.", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path / "v.mp3", attempts=3)
    assert err.value.blocked and len(calls) == 4  # 3 попытки + одна проверка связи


def test_unpronounceable_phrase_names_it_after_trying_parts(tmp_path, monkeypatch):
    stream, calls = picky_stream(lambda t: "ужас" in t)
    monkeypatch.setattr(tts, "_edge_stream", stream)
    with pytest.raises(tts.TTSError, match="даже по частям") as err:
        tts.edge_synthesize("Это ужас, правда.", "ru-RU-DmitryNeural", 0, 0, 0, tmp_path / "v.mp3", attempts=1)
    assert err.value.blocked and "Это ужас," in calls  # пробовали и по запятым


def test_concurrent_piper_downloads_fetch_once(tmp_path, monkeypatch):
    import io
    import threading
    import time as _time

    from studio import config, net

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    urls = []

    class Opener:
        def open(self, req, timeout=0):
            urls.append(req.full_url)
            _time.sleep(0.05)
            return io.BytesIO(b"{}" if ".onnx.json" in req.full_url else b"onnx")

    monkeypatch.setattr(net, "opener", lambda: Opener())
    threads = [threading.Thread(target=tts.piper_model, args=("ru_RU-irina-medium",)) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(urls) == 2  # .onnx.json и .onnx — по одному разу
    assert (tmp_path / "cache" / "piper" / "ru_RU-irina-medium.onnx").read_bytes() == b"onnx"



def test_piper_problem_reports_import_failure(monkeypatch):
    import importlib.util
    import sys

    monkeypatch.setattr(tts, "_piper_problem", None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "piper" else None)
    monkeypatch.setitem(sys.modules, "piper", None)  # import piper → ImportError, как при сломанном onnxruntime
    assert "не загружается" in tts.piper_problem() and not tts.piper_available()
    monkeypatch.setattr(tts, "_piper_problem", None)
