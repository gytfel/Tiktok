"""ИИ-картинки: деление на сцены, промты, запросы к сервисам (без сети) и рендер."""

import base64
import io
import json

import pytest
from PIL import Image

from studio import ai, config, library
from studio.ffmpeg_utils import capabilities
from studio.net import NetError
from studio.pipeline import RenderContext, RenderError, check_sources, render
from studio.project import normalize
from studio.text_utils import clean_text
from tests.fake_tts import fake_synthesize

BOT_TEXT = clean_text(
    "Собеседник удалил сообщение, а ты так и не узнал, что там было? Знакомо?\n\n"
    "Наш бот сохраняет удалённые сообщения. Подключи его за минуту — и больше ничего не пропустишь. Ссылка в профиле!"
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for name in ("WORKSPACE_DIR", "UPLOADS_DIR", "OUTPUTS_DIR", "JOBS_DIR", "CACHE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    config.ensure_dirs()
    return tmp_path


def png(color=(200, 40, 90), size=(64, 112)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class FakeProvider(ai.Provider):
    name = "fake"
    label = "Fake"

    def __init__(self, chat_answer=None):
        self.chat_answer = chat_answer
        self.images = []

    def chat(self, system, user):
        if self.chat_answer is None:
            n = user.count("\n") - user.count("Overall idea")
            return json.dumps({"prompts": [f"shot {i}" for i in range(1, n + 1)]})
        return self.chat_answer

    def image(self, prompt, seed):
        self.images.append(prompt)
        return png((40 * len(self.images) % 255, 120, 200))


# ---------------------------------------------------------------- сцены


def test_split_scenes_for_bot_ad():
    scenes = ai.split_scenes(BOT_TEXT)
    assert [s.text for s in scenes] == [
        "Собеседник удалил сообщение, а ты так и не узнал, что там было? Знакомо?",
        "Наш бот сохраняет удалённые сообщения.",
        "Подключи его за минуту — и больше ничего не пропустишь. Ссылка в профиле!",
    ]
    assert all(BOT_TEXT[s.start_char:s.end_char] == s.text for s in scenes)


def test_split_scenes_cuts_long_sentence_at_commas_and_caps_count():
    long = clean_text(", ".join(["очень длинная часть предложения про что-то важное"] * 6) + ".")
    scenes = ai.split_scenes(long)
    assert len(scenes) > 1
    assert all(s.seconds <= 8 for s in scenes)
    many = clean_text("\n\n".join(f"Абзац номер {i}." for i in range(20)))
    assert len(ai.split_scenes(many, max_scenes=12)) == 12


def test_sync_scenes_keeps_prompts_and_media_of_unchanged_text(workspace):
    meta = library.save_upload("a.png", io.BytesIO(png()))
    stored = [{"text": "Наш бот сохраняет удалённые сообщения.", "prompt": "robot", "image": meta["id"]}]
    scenes = ai.sync_scenes(BOT_TEXT, stored)
    assert scenes[1]["prompt"] == "robot" and scenes[1]["image"] == meta["id"]
    assert scenes[0]["prompt"] == "" and scenes[0]["image"] is None
    # Удалённый файл не считается картинкой сцены.
    library.delete_upload(meta["id"])
    assert ai.sync_scenes(BOT_TEXT, stored)[1]["image"] is None


# ---------------------------------------------------------------- промты


@pytest.mark.parametrize(
    "answer",
    [
        '{"prompts": ["a cat", "a dog"]}',
        '["a cat", "a dog"]',
        '```json\n{"prompts": ["a cat", "a dog"]}\n```',
        'Sure! Here you go: {"prompts": [{"prompt": "a cat"}, {"prompt": "a dog"}]}',
        "1. a cat\n2. a dog",
    ],
)
def test_parse_prompt_list_variants(answer):
    assert ai.parse_prompt_list(answer) == ["a cat", "a dog"]


def test_write_prompts_pads_and_falls_back():
    prompts, warning = ai.write_prompts(FakeProvider('{"prompts": ["only one"]}'), ["раз", "два"], "идея")
    assert prompts == ["only one", "два"] and warning is None

    class Broken(FakeProvider):
        def chat(self, system, user):
            raise ai.AIError("сервис упал")

    prompts, warning = ai.write_prompts(Broken(), ["раз", "два"])
    assert prompts == ["раз", "два"] and "сервис упал" in warning
    prompts, warning = ai.write_prompts(None, ["раз"])
    assert prompts == ["раз"] and warning


def test_build_prompt_adds_style_and_no_text_rule():
    p = ai.build_prompt("a young man in bed at night.", "photo")
    assert p.startswith("a young man in bed at night. photorealistic")
    assert "no text" in p


# ---------------------------------------------------------------- Cloudflare


def test_cloudflare_flux_request_and_response(monkeypatch):
    calls = []

    def fake_request(url, **kw):
        calls.append((url, kw))
        return 200, "application/json", json.dumps({"result": {"image": base64.b64encode(png()).decode()}, "success": True}).encode()

    monkeypatch.setattr(ai, "request", fake_request)
    data = ai.CloudflareProvider("acc123", "tok", "flux").image("a cat", 7)
    assert data.startswith(b"\x89PNG")
    url, kw = calls[0]
    assert url == "https://api.cloudflare.com/client/v4/accounts/acc123/ai/run/@cf/black-forest-labs/flux-1-schnell"
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["json_body"] == {"prompt": "a cat", "steps": 6}  # у FLUX нет width/height/seed


def test_cloudflare_sdxl_binary_and_chat(monkeypatch):
    calls = []

    def fake_request(url, **kw):
        calls.append((url, kw))
        if "llama" in url:
            return 200, "application/json", json.dumps({"result": {"response": {"prompts": ["x"]}}}).encode()
        return 200, "image/png", png()

    monkeypatch.setattr(ai, "request", fake_request)
    cf = ai.CloudflareProvider("acc", "tok", "sdxl")
    assert cf.image("a cat", 5).startswith(b"\x89PNG")
    assert calls[0][1]["json_body"]["width"] == ai.IMAGE_W and calls[0][1]["json_body"]["seed"] == 5
    assert json.loads(cf.chat("sys", "user")) == {"prompts": ["x"]}
    assert calls[1][1]["json_body"]["messages"][0] == {"role": "system", "content": "sys"}


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, b'{"errors":[{"code":10000,"message":"Authentication error"}]}', "Account ID"),
        (429, b'{"errors":[{"code":4006,"message":"you have used up your daily free allocation of 10,000 neurons"}]}', "дневной лимит"),
        (500, b'{"errors":[{"message":"boom"}]}', "Cloudflare: boom"),
    ],
)
def test_cloudflare_errors_are_explained(monkeypatch, status, body, expected):
    from studio.net import error_message

    def fake_request(url, **kw):
        raise NetError(error_message(body), status, body)

    monkeypatch.setattr(ai, "request", fake_request)
    with pytest.raises(ai.AIError, match=expected):
        ai.CloudflareProvider("a", "t").image("x", 1)


# ---------------------------------------------------------------- Pollinations


def test_pollinations_image_and_chat(monkeypatch):
    calls = []

    def fake_request(url, **kw):
        calls.append((url, kw))
        if url.endswith("/v1/chat/completions"):
            return 200, "application/json", json.dumps({"choices": [{"message": {"content": "hello"}}]}).encode()
        return 200, "image/jpeg", b"\xff\xd8\xff" + b"0" * 100

    monkeypatch.setattr(ai, "request", fake_request)
    pol = ai.PollinationsProvider("sk_test", image_model="flux")
    assert pol.image("a red cat", 42).startswith(b"\xff\xd8\xff")
    url, kw = calls[0]
    assert url.startswith("https://gen.pollinations.ai/image/a%20red%20cat?")
    assert "width=768" in url and "height=1344" in url and "seed=42" in url and "model=flux" in url
    assert kw["headers"]["Authorization"] == "Bearer sk_test"
    assert pol.chat("s", "u") == "hello"


def test_pollinations_video_async_job(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    statuses = iter(["pending", "succeeded"])
    calls = []

    def fake_request(url, **kw):
        calls.append((url, kw))
        if "/video/" in url:
            assert kw["headers"]["Prefer"] == "respond-async"
            return 202, "application/json", b'{"id": "job1", "status": "pending"}'
        if url.endswith("/v1/jobs/job1"):
            state = next(statuses)
            return 200, "application/json", json.dumps({"id": "job1", "status": state, "result_url": "/v1/jobs/job1/result"}).encode()
        if url.endswith("/v1/jobs/job1/result"):
            return 200, "video/mp4", b"\x00" * 5000
        raise AssertionError(url)

    monkeypatch.setattr(ai, "request", fake_request)
    data = ai.PollinationsProvider("sk").video("a cat", 4)
    assert len(data) == 5000
    assert "aspectRatio=9%3A16" in calls[0][0] and "duration=4" in calls[0][0]


def test_pollinations_payment_error(monkeypatch):
    def fake_request(url, **kw):
        raise NetError("Insufficient pollen balance", 402)

    monkeypatch.setattr(ai, "request", fake_request)
    with pytest.raises(ai.AIError, match="Pollen"):
        ai.PollinationsProvider("sk").image("x", 1)


def test_video_seconds():
    assert [ai.video_seconds(s) for s in (2, 4.5, 6, 9)] == [4, 6, 6, 8]


# ---------------------------------------------------------------- рендер


def test_ai_background_requires_provider(workspace, monkeypatch):
    monkeypatch.setattr(ai, "get_provider", lambda settings=None: None)
    with pytest.raises(RenderError, match="Настройках"):
        check_sources(normalize({"text": BOT_TEXT, "background": {"type": "ai"}}))


@pytest.mark.skipif(not capabilities()["ok"], reason="нужен ffmpeg")
def test_render_with_ai_images(workspace, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(ai, "get_provider", lambda settings=None: provider)
    logs = []
    project = {"text": BOT_TEXT, "background": {"type": "ai", "ai_style": "photo"}, "export": {"quality": "fast"}}
    result = render(project, RenderContext(workspace / "job", on_log=logs.append), synthesize=fake_synthesize)

    scenes = result["ai_scenes"]
    assert len(scenes) == 3 and len(provider.images) == 3
    assert [s["prompt"] for s in scenes] == ["shot 1", "shot 2", "shot 3"]
    assert all(s["image"] for s in scenes)
    assert "photorealistic" in provider.images[0]
    scene_log = next(line for line in logs if line.startswith("Сцены:"))
    assert scene_log.count(".png") == 3  # по кадру на сцену

    # Повторный рендер с теми же сценами ничего не рисует заново.
    project["background"]["ai_scenes"] = scenes
    render(project, RenderContext(workspace / "job2", on_log=lambda m: None), synthesize=fake_synthesize)
    assert len(provider.images) == 3
