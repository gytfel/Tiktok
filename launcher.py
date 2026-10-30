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
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
WINDOWS = os.name == "nt"
NEEDED_MB = 500  # окружение с зависимостями + временные файлы установки
NO_SPACE_MARKERS = ("no space left on device", "errno 28", "winerror 112", "not enough space on the disk")


def free_space_report(paths: list[Path]) -> str:
    """«D: — 0.2 ГБ, C: — 1.5 ГБ» по дискам, на которых лежат пути."""
    seen: dict[str, int] = {}
    for p in paths:
        try:
            anchor = Path(p).resolve().anchor or str(p)
            if anchor not in seen:
                seen[anchor] = shutil.disk_usage(anchor).free
        except OSError:
            continue
    return ", ".join(f"{(a.rstrip(chr(92) + '/') or a)} — {free / 1024 ** 3:.1f} ГБ" for a, free in seen.items())


def low_space(paths: list[Path], need_mb: int = NEEDED_MB) -> bool:
    for p in paths:
        try:
            if shutil.disk_usage(Path(p).resolve().anchor or str(p)).free < need_mb * 1024 * 1024:
                return True
        except OSError:
            continue
    return False


def print_no_space(paths: list[Path]) -> None:
    print("\nНа диске закончилось место — установка не помещается.")
    print(f"Свободно: {free_space_report(paths)}. Для установки нужно около {NEEDED_MB} МБ, и ещё место под видео.")
    print("Освободите место: очистите Корзину и папку «Загрузки», удалите ненужные программы или")
    print("запустите «Очистку диска» Windows. Затем снова запустите start.bat.")


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
    disks = [ROOT, venv_dir.parent, Path(tempfile.gettempdir())]
    if low_space(disks, need_mb=200):
        print_no_space(disks)
        return
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
    # Временные файлы pip — на тот же диск, что и окружение (а не на системный C:),
    # и без кэша скачанных пакетов: так установке нужно меньше места.
    tmp = venv_dir.parent / ".pip-tmp"
    disks = [venv_dir, Path(tempfile.gettempdir())]
    if low_space(disks):
        print(f"Внимание: мало места на диске (свободно: {free_space_report(disks)}), установка может не поместиться.")
    print("Устанавливаю зависимости... (несколько минут при первом запуске)")
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "TMP": str(tmp), "TEMP": str(tmp), "TMPDIR": str(tmp)}
    except OSError:
        env = dict(os.environ)
    res = subprocess.run(
        [str(venv_python(venv_dir)), "-m", "pip", "install", "-q", "--disable-pip-version-check",
         "--no-cache-dir", "-r", str(REQUIREMENTS)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    shutil.rmtree(tmp, ignore_errors=True)
    output = (res.stdout or "") + (res.stderr or "")
    if output.strip():
        print(output.rstrip())
    if res.returncode != 0:
        if any(m in output.lower() for m in NO_SPACE_MARKERS):
            print_no_space(disks)
        else:
            print("\nНе удалось установить зависимости. Проверьте подключение к интернету и запустите ещё раз.")
        return False
    marker.write_text(digest, encoding="utf-8")
    return True


# Необязательные пакеты: если не встанут (например, нет сборки под эту версию Python), студия
# всё равно работает. На Linux Piper уже в requirements.txt.
OPTIONAL = [] if sys.platform.startswith("linux") else ["piper-tts>=1.3"]


def install_optional(venv_dir: Path) -> None:
    """Голоса Piper — озвучка без Microsoft (выручает, когда Microsoft не отдаёт звук, например через VPN).
    Ставятся один раз; неудача не мешает запуску."""
    if not OPTIONAL:
        return
    marker = venv_dir / ".tiktok-studio-optional"
    spec = " ".join(OPTIONAL)
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == spec:
        return
    print("Ставлю запасные голоса Piper (один раз, ~1 минута)...")
    res = subprocess.run(
        [str(venv_python(venv_dir)), "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-cache-dir", *OPTIONAL],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        print("Голоса Piper не установились — студия работает и без них (голоса Microsoft и системные).")
    try:
        marker.write_text(spec, encoding="utf-8")  # не пытаемся при каждом запуске
    except OSError:
        pass


def main() -> int:
    if sys.version_info < (3, 10):
        print(f"Нужен Python 3.10 или новее, а установлен {sys.version.split()[0]}.")
        print("Скачайте свежий Python с https://www.python.org/downloads/")
        return 1
    venv_dir = ensure_venv()
    if venv_dir is None or not install_requirements(venv_dir):
        return 1
    install_optional(venv_dir)
    try:
        return subprocess.call([str(venv_python(venv_dir)), str(ROOT / "run.py"), *sys.argv[1:]], cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
