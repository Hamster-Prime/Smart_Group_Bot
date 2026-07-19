"""Local launcher with webhook support and polling fallback."""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from bot.utils.logging_setup import configure_logging

configure_logging()
log = logging.getLogger("launcher")


REQUIRED_MODULES = (
    "aiogram",
    "sqlalchemy",
    "litellm",
    "pydantic",
    "pydantic_settings",
    "dotenv",
    "cryptography",
)


def _missing_modules(modules: tuple[str, ...]) -> list[str]:
    return [name for name in modules if importlib.util.find_spec(name) is None]


def ensure_dependencies() -> None:
    """Best-effort one-click dependency bootstrap."""
    missing = _missing_modules(REQUIRED_MODULES)
    if not missing:
        return

    log.warning("Missing dependencies detected: %s", ", ".join(missing))
    log.info("Installing project dependencies via: %s -m pip install -e .", sys.executable)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=str(ROOT),
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Dependency installation failed, please check pip/network and retry.")

    still_missing = _missing_modules(REQUIRED_MODULES)
    if still_missing:
        raise RuntimeError("Dependencies still missing after install: " + ", ".join(still_missing))
    log.info("Dependency bootstrap finished")


def preflight() -> None:
    """Validate local runtime prerequisites before startup."""
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            shutil.copy(example, env_path)
            env_path.chmod(0o600)
            log.warning("Created .env from .env.example; fill the bootstrap settings first")
            print("\nPlease edit .env and set BOT_TOKEN, SUPER_ADMIN_ID, CONFIG_MASTER_KEY and MINIAPP_PUBLIC_BASE_URL.\n")
            sys.exit(1)
        log.error("Missing .env and .env.example")
        sys.exit(1)

    try:
        env_path.chmod(0o600)
    except OSError as exc:
        log.warning("Could not restrict .env permissions: %s", exc)

    from dotenv import load_dotenv

    load_dotenv()
    configure_logging(force=True)
    from bot.config import load_bootstrap_settings, validate_bootstrap_settings

    try:
        bootstrap_settings = load_bootstrap_settings()
        validate_bootstrap_settings(bootstrap_settings)
    except ValueError as exc:
        print(f"\n{exc}\n")
        sys.exit(1)
    if not bootstrap_settings.miniapp_public_base_url:
        log.warning("MINIAPP_PUBLIC_BASE_URL is empty; /settings and verification buttons will be unavailable")

    Path("data").mkdir(exist_ok=True)
    log.info("Preflight passed")


if __name__ == "__main__":
    try:
        ensure_dependencies()
        preflight()

        print()
        print("=" * 50)
        print("  Smart Group Bot - Local Run")
        print("=" * 50)
        print("  Mode: Telegram webhook / polling fallback")
        print("  Press Ctrl+C to stop")
        print("=" * 50)
        print()

        from bot.__main__ import run_bot

        run_bot()
    except KeyboardInterrupt:
        print("\nExited.")
    except Exception as exc:
        log.error("Startup failed: %s", exc)
        sys.exit(1)
