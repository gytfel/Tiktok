"""Похожее видео по образцу: разбор ролика, распознавание речи, скачивание по ссылке."""

import json
import re
import subprocess
import sys
import time
import types

import pytest

from studio import ai, config, library, phone, reference
from studio.ffmpeg_utils import capabilities, ffmpeg_exe

needs_ffmpeg = pytest.mark.skipif(not capabilities()["ok"], reason="нужен ffmpeg с libx264")

WORDS = "Знаешь почему твои видео не набирают просмотры? Всё дело в первых трёх секундах. Цепляй сразу и держи темп до конца".split()


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    for name in ("WORKSPACE_DIR", "UPLOADS_DIR", "OUTPUTS_DIR", "JOBS_DIR", "CACHE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(reference.tts, "list_voices", lambda force=False: {"voices": list(reference.tts.FALLBACK_VOICES)})
    config.ensure_dirs()
    return tmp_path


@pytest.fixture
def sample_video(tmp_path):
    """14 секунд: красный 2 с, синий 3 с, зелёный 9 с — и звук."""
    path = tmp_path / "ref.mp4"
    subprocess.run(
        [
            ffmpeg_exe(), "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=180x320:d=2:r=25",
            "-f", "lavfi", "-i", "color=c=blue:s=180x320:d=3:r=25",
            "-f", "lavfi", "-i", "color=c=green:s=180x320:d=9:r=25",
            "-f", "lavfi", "-i", "sine=f=300:d=14",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


def upload(path) -> str:
    with open(path, "rb") as f:
        return library.save_upload("ref.mp4", f)["id"]


class FakeProvider(ai.Provider):
    name, label = "fake", "Fake"

    def __init__(self, fail_look=False, fail_transcribe=False):
        self.fail_look = fail_look
        self.fail_transcribe = fail_transcribe
        self.system = self.brief = ""
        self.sheet = b""

    def transcribe(self, audio, duration=0.0):
        assert audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")  # mp3
        if self.fail_transcribe:
            raise ai.AIError("сервис недоступен")
        step = 13.0 / len(WORDS)
        return ai.Transcript(" ".join(WORDS), "ru", [(w, 0.5 + i * step, 0.5 + (i + 1) * step) for i, w in enumerate(WORDS)])

    def look(self, image, prompt):
        self.sheet = image
        if self.fail_look:
            raise ai.AIError("модель не видит картинки")
        return json.dumps({"frames": [{"n": i, "shot": f"shot {i}", "text": "ХУК" if i == 1 else ""} for i in range(1, 5)], "look": "flat"})

    def chat(self, system, user, max_tokens=1500):
        self.system, self.brief = system, user
        n = int(re.search(r"scenes: exactly (\d+) items", system).group(1))
        return "```json\n" + json.dumps(
            {
                "hook": "Почему нет просмотров?",
                "style": "neon",
                "summary": "Ролик о хуках.",
                "scenes": [{"text": f"Сцена {i} рассказывает важное.", "prompt": f"prompt {i}"} for i in range(1, n + 1)],
            },
            ensure_ascii=False,
        ) + "\n```"


# ---------------------------------------------------------------- планы


def test_plan_shots_merges_short_and_splits_long():
    shots = reference.plan_shots(14.0, [0.3, 2.0, 2.4, 5.0])
    assert [(round(s.start, 1), round(s.end, 1)) for s in shots] == [(0, 2.4), (2.4, 5.0), (5.0, 9.5), (9.5, 14.0)]


def test_plan_shots_caps_count():
    shots = reference.plan_shots(31.0, [float(t) for t in range(1, 31)])
    assert len(shots) == reference.MAX_SHOTS
    assert shots[0].start == 0 and shots[-1].end == 31.0
    assert all(a.end == b.start for a, b in zip(shots, shots[1:]))


def test_assign_speech_by_word_middle():
    shots = [reference.Shot(0, 2), reference.Shot(2, 5)]
    reference.assign_speech(shots, [("раз", 0.1, 0.5), ("два", 1.5, 2.2), ("три", 4.0, 4.5), ("хвост", 6.0, 7.0)])
    assert shots[0].speech == "раз два" and shots[1].speech == "три хвост"


def test_voice_rate_and_pace():
    assert reference.voice_rate(None) is None
    assert reference.voice_rate(14.0) == 0
    assert reference.voice_rate(18.0) == 30
    assert reference.voice_rate(40.0) == 40  # ограничение
    assert reference.speech_pace([("a", 0, 1)] * 3) is None


def test_pick_voice_keeps_suitable_and_gender():
    assert reference.pick_voice("ru", "ru-RU-SvetlanaNeural") == "ru-RU-SvetlanaNeural"
    assert reference.pick_voice("ru", "en-US-AvaMultilingualNeural") == "ru-RU-SvetlanaNeural"  # женский → женский
    assert reference.pick_voice("en", "ru-RU-DmitryNeural").startswith("en-")


def test_build_project_keeps_prompt_for_split_paragraph():
    long = "Первое длинное предложение про бота, который сохраняет сообщения. Второе предложение тоже длинное и подробное. Третье, чтобы точно разрезать."
    patch = reference.build_project(
        [{"text": "Короткий хук.", "prompt": "hook prompt"}, {"text": long, "prompt": "long prompt"}, {"text": "🔥", "prompt": "emoji"}],
        "Хук", "photo", "мой бот",
    )
    scenes = patch["background"]["ai_scenes"]
    assert scenes[0]["prompt"] == "hook prompt"
    assert len(scenes) >= 3 and {sc["prompt"] for sc in scenes[1:]} == {"long prompt"}
    assert patch["text"].count("\n\n") == 1  # сцена из одного эмодзи выброшена
    assert patch["title"]["text"] == "Хук" and patch["background"]["ai_style"] == "photo"


def test_parse_object_variants():
    assert reference.parse_object('Вот ответ: {"a": 1} — готово')["a"] == 1
    with pytest.raises(ai.AIError):
        reference.parse_object("нет json")


# ---------------------------------------------------------------- разбор целиком


@needs_ffmpeg
def test_analyze_adapt(sample_video):
    provider = FakeProvider()
    stages = []
    res = reference.analyze(upload(sample_video), provider, topic="бот для Telegram", current_voice="ru-RU-SvetlanaNeural",
                            on_stage=lambda text, frac: stages.append(text))
    info = res["reference"]
    assert [(s["start"], s["end"]) for s in info["shots"]] == [(0.0, 2.0), (2.0, 5.0), (5.0, 9.5), (9.5, 14.0)]
    assert info["shots"][0]["text"] == "ХУК" and info["shots"][1]["speech"].startswith("твои")
    assert info["sheet"].startswith("data:image/jpeg;base64,") and provider.sheet[:3] == b"\xff\xd8\xff"
    assert "бот для Telegram" in provider.system and "Narrator:" in provider.brief and "On-screen text" in provider.brief
    project = res["project"]
    assert project["text"].count("\n\n") == 3 and project["title"]["text"] == "Почему нет просмотров?"
    assert project["background"]["type"] == "ai" and project["background"]["ai_style"] == "neon"
    assert [sc["prompt"] for sc in project["background"]["ai_scenes"]] == ["prompt 1", "prompt 2", "prompt 3", "prompt 4"]
    assert project["voice"]["voice"] == "ru-RU-SvetlanaNeural" and "rate" in project["voice"]
    assert stages[-1] == "Готово" and not res["warnings"]


@needs_ffmpeg
def test_analyze_exact_keeps_narrator_words(sample_video):
    res = reference.analyze(upload(sample_video), FakeProvider(), mode="exact")
    assert res["project"]["text"].split("\n\n")[0] == "Знаешь почему"
    assert " ".join(res["project"]["text"].split()) == " ".join(WORDS)


@needs_ffmpeg
def test_analyze_survives_failed_vision_and_speech(sample_video):
    res = reference.analyze(upload(sample_video), FakeProvider(fail_look=True, fail_transcribe=True), mode="exact")
    assert len(res["warnings"]) == 3  # речь, кадры и «нет речи — текст заново»
    assert res["project"]["text"] and res["reference"]["transcript"] == ""


@needs_ffmpeg
def test_analyze_rejects_images(workspace, tmp_path):
    img = tmp_path / "pic.png"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(img)], check=True)
    with open(img, "rb") as f:
        item = library.save_upload("pic.png", f)["id"]
    with pytest.raises(reference.ReferenceError):
        reference.analyze(item, FakeProvider())


# ---------------------------------------------------------------- распознавание речи


def test_transcript_from_whisper_variants():
    turbo = {
        "transcription_info": {"language": "ru"},
        "text": "Привет мир",
        "segments": [{"start": 0.0, "end": 1.0, "text": "Привет мир", "words": [{"word": "Привет", "start": 0.0, "end": 0.4}, {"word": "мир", "start": 0.5, "end": 1.0}]}],
    }
    t = ai.transcript_from(turbo)
    assert t.language == "ru" and t.words[1] == ("мир", 0.5, 1.0)

    classic = {"text": "один два", "words": [{"word": "один", "start": 0, "end": 1}, {"word": "два", "start": 1, "end": 2}]}
    assert [w for w, _, _ in ai.transcript_from(classic).words] == ["один", "два"]

    verbose = {"language": "russian", "text": "раз два три", "segments": [{"start": 2.0, "end": 5.0, "text": "раз два три"}]}
    t = ai.transcript_from(verbose)
    assert t.language == "ru" and t.words[0] == ("раз", 2.0, 3.0) and t.words[-1][2] == 5.0

    plain = ai.transcript_from({"text": "только текст"}, duration=4.0)
    assert plain.words[-1][2] == 4.0


def test_cloudflare_transcribe_and_look(monkeypatch):
    calls = []

    def fake_request(url, **kw):
        calls.append((url, kw))
        if "whisper" in url:
            return 200, "application/json", json.dumps({"result": {"text": "да", "transcription_info": {"language": "ru"}, "segments": []}}).encode()
        return 200, "application/json", json.dumps({"result": {"response": {"frames": []}}}).encode()

    monkeypatch.setattr(ai, "request", fake_request)
    p = ai.CloudflareProvider("acc", "tok")
    t = p.transcribe(b"ID3audio", 2.0)
    assert t.text == "да" and t.words == [("да", 0.0, 2.0)]
    url, kw = calls[0]
    assert url.endswith(ai.CF_STT_MODEL) and kw["json_body"]["audio"] == "SUQzYXVkaW8="
    assert json.loads(p.look(b"\xff\xd8\xffjpeg", "что тут?")) == {"frames": []}
    url, kw = calls[1]
    part = kw["json_body"]["messages"][0]["content"][1]
    assert url.endswith(ai.CF_VISION_MODEL) and part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_pollinations_transcribe_multipart_with_fallback(monkeypatch):
    calls = []

    def fake_request(url, **kw):
        calls.append(kw)
        if b"verbose_json" in kw["data"]:
            raise ai.NetError("unsupported", 400)
        return 200, "application/json", b'{"text": "hello there"}'

    monkeypatch.setattr(ai, "request", fake_request)
    t = ai.PollinationsProvider("sk_test").transcribe(b"ID3audio", 3.0)
    assert t.text == "hello there" and len(calls) == 2
    kw = calls[1]
    assert kw["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="audio.mp3"' in kw["data"] and b"ID3audio" in kw["data"]


# ---------------------------------------------------------------- ссылка


def fake_ytdlp(monkeypatch, source=None, error=None):
    seen = {}

    class YoutubeDL:
        def __init__(self, opts):
            seen["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            seen["url"] = url
            if error:
                raise Exception(error)
            out = seen["opts"]["outtmpl"].replace("%(ext)s", "mp4")
            with open(out, "wb") as f:
                f.write(source.read_bytes())
            seen["opts"]["progress_hooks"][0]({"downloaded_bytes": 5, "total_bytes": 10})
            return {"title": "Крутой ролик", "description": "описание #хэштег"}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=YoutubeDL))
    return seen


@needs_ffmpeg
def test_download_saves_reference_to_library(monkeypatch, sample_video):
    seen = fake_ytdlp(monkeypatch, source=sample_video)
    progress = []
    meta = reference.download(" https://www.tiktok.com/@a/video/1 ", on_progress=progress.append)
    assert seen["url"] == "https://www.tiktok.com/@a/video/1" and seen["opts"]["noplaylist"]
    assert meta["kind"] == "video" and meta["name"].startswith("Образец — Крутой ролик") and progress == [0.5]
    stored, _ = library.get_upload(meta["id"])
    assert stored["caption"] == "описание #хэштег" and stored["reference_url"].endswith("/video/1")
    assert not list(config.CACHE_DIR.glob("ref-*"))  # временная папка убрана


@pytest.mark.parametrize(
    "error, expected",
    [
        ("ERROR: Unsupported URL: https://example.com", "не поддерживается"),
        ("ERROR: [Instagram] abc: Requested content is not available, rate-limit reached or login required", "без входа"),
        ("ERROR: [TikTok] 1: HTTP Error 404: Not Found", "не найдено"),
    ],
)
def test_download_errors_are_explained(monkeypatch, error, expected):
    fake_ytdlp(monkeypatch, error=error)
    with pytest.raises(reference.ReferenceError, match=expected):
        reference.download("https://example.com/v")


def test_is_url():
    assert reference.is_url("https://vt.tiktok.com/ZS123/") and not reference.is_url("tiktok.com/abc")


# ---------------------------------------------------------------- API


@pytest.fixture
def client():
    testclient = pytest.importorskip("fastapi.testclient")
    from studio.server import app

    phone.failures.clear()
    return testclient.TestClient(app, client=("127.0.0.1", 50000))


def test_reference_api_requires_provider_and_source(client, monkeypatch):
    monkeypatch.setattr(ai, "get_provider", lambda settings=None: None)
    assert client.post("/api/reference", json={"url": "https://x.y/v"}).status_code == 400
    monkeypatch.setattr(ai, "get_provider", lambda settings=None: FakeProvider())
    assert "Загрузите" in client.post("/api/reference", json={}).json()["detail"]
    assert "ссылк" in client.post("/api/reference", json={"url": "tiktok.com/abc"}).json()["detail"]


@needs_ffmpeg
def test_reference_api_runs_in_background(client, monkeypatch, sample_video):
    monkeypatch.setattr(ai, "get_provider", lambda settings=None: FakeProvider())
    task = client.post("/api/reference", json={"upload_id": upload(sample_video), "topic": "бот"}).json()
    assert task["status"] == "running"
    for _ in range(200):
        task = client.get(f"/api/ai/tasks/{task['id']}").json()
        if task["status"] != "running":
            break
        time.sleep(0.1)
    assert task["status"] == "done", task.get("error")
    assert task["progress"] == 100 and task["result"]["project"]["background"]["type"] == "ai"
