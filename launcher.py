"""Подготовка окружения и запуск студии. Вызывается из start.bat / start.sh.

Работает только на стандартной библиотеке: создаёт виртуальное окружение,
ставит зависимости и запускает run.py. Если окружение не создаётся в папке
программы (кириллица в пути, антивирус, повреждённая прошлая попытка), пробует
запасную папку с латинским путём и обходится без ensurepip.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
WINDOWS = os.name == "nt"


def fallback_venv_dir() -> Path:
    if WINDOWS:
        base = Path(os.environ.get("ProgramData") or r"C:\ProgramData")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "TikTokStudio" / "venv"


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if WINDOWS else "bin/python")


def _quiet(cmd: list) -> bool:
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def healthy(venv_dir: Path) -> bool:
    """Окружение есть и в нём работает pip."""
    py = venv_python(venv_dir)
    return py.exists() and _quiet([str(py), "-m", "pip", "--version"])


def create(venv_dir: Path, log=print) -> bool:
    """Создаёт окружение. Сначала обычным способом, потом — без ensurepip,
    поставив pip силами основного Python."""
    shutil.rmtree(venv_dir, ignore_errors=True)
    try:
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        if healthy(venv_dir):
            return True
    except Exception:  # ensurepip падает с невнятным «exit status 1»
        log("  стандартная установка pip не сработала — пробую другой способ...")
    shutil.rmtree(venv_dir, ignore_errors=True)
    if importlib.util.find_spec("pip") is None:
        return False
    try:
        venv.EnvBuilder(with_pip=False).create(venv_dir)
    except Exception as exc:
        log(f"  не удалось создать папку окружения: {exc}")
        return False
    ok = _quiet([sys.executable, "-m", "pip", "--python", str(venv_python(venv_dir)),
                 "install", "-q", "--disable-pip-version-check", "pip"])
    return ok and healthy(venv_dir)


def explain_failure(venv_dir: Path) -> None:
    """Показывает настоящую причину, которую venv прячет за «exit status 1»."""
    print("\nНе удалось подготовить Python для студии.")
    if importlib.util.find_spec("ensurepip") is None and importlib.util.find_spec("pip") is None:
        print("В вашем Python нет pip. Запустите установщик Python ещё раз → «Modify» →")
        print("отметьте «pip» → «Next» → «Install», затем снова запустите start.bat.")
        return
    print("Подробности ошибки:\n")
    shutil.rmtree(venv_dir, ignore_errors=True)
    try:
        venv.EnvBuilder(with_pip=False).create(venv_dir)
        subprocess.run([str(venv_python(venv_dir)), "-m", "ensurepip", "--upgrade", "--default-pip"])
    except Exception as exc:
        print(exc)
    print("\nЧто можно сделать:")
    print(" 1. Перенесите папку студии в путь только из латинских букв, например D:\\TikTokStudio.")
    print(" 2. Если стоит антивирус, разрешите ему эту папку или отключите его на время установки.")
    print(" 3. Переустановите Python с python.org (галочка «Add python.exe to PATH»).")
    print("Если не поможет — пришлите снимок этого окна.")


def ensure_venv() -> Path | None:
    candidates = [ROOT / ".venv", fallback_venv_dir()]
    for venv_dir in candidates:
        if healthy(venv_dir):
            return venv_dir
    print("Первый запуск: создаю окружение и устанавливаю зависимости. Это займёт пару минут...")
    for i, venv_dir in enumerate(candidates):
        if i:
            print(f"В папке программы окружение не создаётся — пробую запасную папку: {venv_dir}")
        if create(venv_dir):
            return venv_dir
    explain_failure(candidates[-1])
    return None


def install_requirements(venv_dir: Path) -> bool:
    """Ставит зависимости, только если requirements.txt изменился с прошлого раза."""
    digest = hashlib.sha1(REQUIREMENTS.read_bytes()).hexdigest()
    marker = venv_dir / ".tiktok-studio-requirements"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
        return True
    print("Устанавливаю зависимости...")
    res = subprocess.run([str(venv_python(venv_dir)), "-m", "pip", "install", "-q",
                          "--disable-pip-version-check", "-r", str(REQUIREMENTS)])
    if res.returncode != 0:
        print("\nНе удалось установить зависимости. Проверьте подключение к интернету и запустите ещё раз.")
        return False
    marker.write_text(digest, encoding="utf-8")
    return True


def main() -> int:
    if sys.version_info < (3, 10):
        print(f"Нужен Python 3.10 или новее, а установлен {sys.version.split()[0]}.")
        print("Скачайте свежий Python с https://www.python.org/downloads/")
        return 1
    venv_dir = ensure_venv()
    if venv_dir is None or not install_requirements(venv_dir):
        return 1
    try:
        return subprocess.call([str(venv_python(venv_dir)), str(ROOT / "run.py"), *sys.argv[1:]], cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
