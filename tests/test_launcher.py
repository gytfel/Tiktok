"""Загрузчик: выбор и восстановление окружения (без реального создания venv)."""

from pathlib import Path

import launcher


def test_uses_existing_healthy_venv_without_creating(monkeypatch):
    monkeypatch.setattr(launcher, "healthy", lambda d: d == launcher.fallback_venv_dir())
    monkeypatch.setattr(launcher, "create", lambda d, log=print: (_ for _ in ()).throw(AssertionError("не должен создавать")))
    assert launcher.ensure_venv() == launcher.fallback_venv_dir()


def test_falls_back_to_ascii_folder_when_program_folder_fails(monkeypatch):
    created = []
    monkeypatch.setattr(launcher, "healthy", lambda d: False)

    def fake_create(d, log=print):
        created.append(d)
        return d != launcher.ROOT / ".venv"

    monkeypatch.setattr(launcher, "create", fake_create)
    assert launcher.ensure_venv() == launcher.fallback_venv_dir()
    assert created == [launcher.ROOT / ".venv", launcher.fallback_venv_dir()]


def test_reports_failure(monkeypatch):
    explained = []
    monkeypatch.setattr(launcher, "healthy", lambda d: False)
    monkeypatch.setattr(launcher, "create", lambda d, log=print: False)
    monkeypatch.setattr(launcher, "explain_failure", explained.append)
    assert launcher.ensure_venv() is None
    assert explained == [launcher.fallback_venv_dir()]


def test_requirements_marker_skips_reinstall(tmp_path, monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.install_requirements(tmp_path) and len(run.calls) == 1
    assert launcher.install_requirements(tmp_path) and len(run.calls) == 1  # хэш совпал — pip не вызывается
    assert isinstance(launcher.fallback_venv_dir(), Path)


class FakeRun:
    def __init__(self, returncode=0, output=""):
        self.calls = []
        self.result = type("R", (), {"returncode": returncode, "stdout": "", "stderr": output})()

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.result


def test_install_uses_no_cache_and_temp_next_to_venv(tmp_path, monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(launcher.subprocess, "run", run)
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    assert launcher.install_requirements(venv_dir)
    cmd, kwargs = run.calls[0]
    assert "--no-cache-dir" in cmd
    assert kwargs["env"]["TEMP"] == str(tmp_path / ".pip-tmp")
    assert not (tmp_path / ".pip-tmp").exists()  # временная папка убрана


def test_install_reports_full_disk(tmp_path, monkeypatch, capsys):
    run = FakeRun(1, "ERROR: Could not install packages due to an OSError: (\"Connection broken: "
                     "OSError(28, 'No space left on device')\", OSError(28, 'No space left on device'))")
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert not launcher.install_requirements(tmp_path)
    out = capsys.readouterr().out
    assert "закончилось место" in out and "Свободно:" in out
    assert "интернет" not in out.split("закончилось место")[1]


def test_install_other_errors_mention_internet(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher.subprocess, "run", FakeRun(1, "ERROR: No matching distribution found for fastapi"))
    assert not launcher.install_requirements(tmp_path)
    assert "интернету" in capsys.readouterr().out
