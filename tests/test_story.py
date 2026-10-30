"""Сценарий по идее: нейросеть пишет весь ролик по короткому описанию."""

import json
import re
import time

import pytest

from studio import ai, config, phone, story, tts


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    for name in ("WORKSPACE_DIR", "UPLOADS_DIR", "OUTPUTS_DIR", "JOBS_DIR", "CACHE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(tts, "list_voices", lambda force=False: {"voices": list(tts.FALLBACK_VOICES)})
    config.ensure_dirs()


class Writer(ai.Provider):
    name, label = "fake", "Fake"

    def __init__(self, answer=None):
        self.answer = answer
        self.system = self.user = ""

    def chat(self, system, user, max_tokens=1500):
        self.system, self.user = system, user
        if self.answer is not None:
            return self.answer
        n = int(re.search(r"scenes: exactly (\d+) items", system).group(1))
        lines = ["Удалили сообщение, а ты так и не узнал, что там было?", "Знакомо? Наш бот сохраняет всё."]
        return json.dumps({
            "hook": "Собеседник удалил сообщение?",
            "style": "neon",
            "caption": "Бот, который ничего не даёт потерять.",
            "hashtags": ["telegram", "#бот", "#бот", "лайфхак дня"],
            "scenes": [{"text": lines[i % 2] + f" Сцена {i + 1}.", "prompt": f"phone screen, scene {i + 1}"} for i in range(n)],
        }, ensure_ascii=False)


def test_story_fills_project_and_post():
    w = Writer()
    res = story.write_story(w, "  реклама бота,\nкоторый сохраняет удалённые сообщения ", seconds=30, fmt="ad",
                            cta="ссылка в профиле", rate=20, current_voice="ru-RU-SvetlanaNeural")
    n = story.scene_count(30)
    assert n == 7 and f"scenes: exactly {n} items" in w.system
    assert "about 504 characters" in w.system  # 30 с × 14 символов × 1.2 (скорость +20%)
    assert "pain or desire" in w.system and "ссылка в профиле" in w.system
    assert w.user == "Idea of the video: реклама бота, который сохраняет удалённые сообщения"
    project = res["project"]
    assert project["text"].count("\n\n") == n - 1 and project["title"]["text"] == "Собеседник удалил сообщение?"
    assert project["background"]["type"] == "ai" and project["background"]["ai_style"] == "neon"
    assert project["background"]["ai_scenes"][0]["prompt"] == "phone screen, scene 1"
    assert project["background"]["ai_context"].startswith("реклама бота")
    assert project["voice"]["voice"] == "ru-RU-SvetlanaNeural" and "rate" not in project["voice"]
    assert res["hashtags"] == ["#telegram", "#бот", "#лайфхакдня"] and res["caption"].startswith("Бот")
    assert res["seconds"] > 0


def test_story_language_and_defaults():
    w = Writer()
    res = story.write_story(w, "5 facts about octopuses", seconds=99, fmt="nope", language="en", current_voice="ru-RU-DmitryNeural")
    assert "in English" in w.system and "Choose the format" in w.system and "exactly 7 items" in w.system  # 99 → 30 с
    assert res["project"]["voice"]["voice"].startswith("en-")


def test_story_needs_an_idea_and_scenes():
    with pytest.raises(story.StoryError):
        story.write_story(Writer(), "  🔥 ")
    with pytest.raises(ai.AIError):
        story.write_story(Writer(answer='{"scenes": []}'), "идея")
    with pytest.raises(ai.AIError):
        story.write_story(Writer(answer="не json"), "идея")


def test_story_api_runs_in_background(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    from studio.server import app

    phone.failures.clear()
    client = testclient.TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    monkeypatch.setattr(ai, "get_provider", lambda settings=None: None)
    assert client.post("/api/ai/story", json={"idea": "бот"}).status_code == 400  # нейросеть не подключена
    monkeypatch.setattr(ai, "get_provider", lambda settings=None: Writer())
    assert client.post("/api/ai/story", json={"idea": " "}).status_code == 400
    task = client.post("/api/ai/story", json={"idea": "бот для заметок", "seconds": 15, "format": "howto"}).json()
    for _ in range(100):
        task = client.get(f"/api/ai/tasks/{task['id']}").json()
        if task["status"] != "running":
            break
        time.sleep(0.05)
    assert task["status"] == "done", task.get("error")
    assert task["result"]["project"]["text"].count("\n\n") == story.scene_count(15) - 1
