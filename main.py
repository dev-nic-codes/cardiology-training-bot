from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"
VENV_PYTHON = PROJECT_DIR / (".venv\\Scripts\\python.exe" if IS_WINDOWS else ".venv/bin/python")
REQUIRED_MODULES = (
    "aiogram",
    "sqlalchemy",
    "aiosqlite",
    "apscheduler",
    "httpx",
    "bs4",
    "feedparser",
    "pydantic_settings",
    "cloudscraper",
    "curl_cffi",
    "PIL",
)


def _print_status(message: str) -> None:
    print(f"[ECG Bot] {message}", flush=True)


def _run(command: list[str], *, description: str) -> None:
    _print_status(description)
    subprocess.run(command, cwd=PROJECT_DIR, check=True)


def _dependencies_ready() -> bool:
    for module_name in REQUIRED_MODULES:
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            return False
    return True


def _ensure_virtualenv() -> None:
    if VENV_PYTHON.exists():
        return

    _run([sys.executable, "-m", "venv", ".venv"], description="Creating project virtual environment...")


def _ensure_dependencies() -> None:
    if _dependencies_ready():
        return

    _run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], description="Upgrading pip in the project virtual environment...")
    _run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], description="Installing bot dependencies from requirements.txt...")


def bootstrap() -> None:
    current_python = Path(sys.executable).resolve()

    if current_python != VENV_PYTHON.resolve():
        _ensure_virtualenv()
        _print_status(f"Re-launching with project interpreter: {VENV_PYTHON}")
        completed = subprocess.run([str(VENV_PYTHON), str(__file__)], cwd=PROJECT_DIR)
        raise SystemExit(completed.returncode)

    _ensure_dependencies()


def main() -> None:
    bootstrap()
    _print_status("Starting ECG bot application...")

    from bot.main import main as bot_main

    import asyncio

    asyncio.run(bot_main())


if __name__ == "__main__":
    main()
