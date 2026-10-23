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
    calls = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})())
    assert launcher.install_requirements(tmp_path) and len(calls) == 1
    assert launcher.install_requirements(tmp_path) and len(calls) == 1  # хэш совпал — pip не вызывается
    assert isinstance(launcher.fallback_venv_dir(), Path)
