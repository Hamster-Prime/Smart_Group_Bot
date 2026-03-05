"""Local launcher for Telegram bot polling mode."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

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
    "qdrant_client",
)

QDRANT_CONTAINER = "smart-group-bot-qdrant"
NEO4J_CONTAINER = "smart-group-bot-neo4j"


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
        raise RuntimeError(
            "Dependencies still missing after install: " + ", ".join(still_missing)
        )
    log.info("Dependency bootstrap finished")


def _load_runtime_env() -> dict[str, str]:
    from dotenv import dotenv_values

    merged: dict[str, str] = {}
    env_path = Path(".env")
    if env_path.exists():
        loaded = dotenv_values(env_path)
        merged.update({k: str(v) for k, v in loaded.items() if k and v is not None})
    merged.update({k: str(v) for k, v in os.environ.items() if k})
    return {k.upper(): v for k, v in merged.items()}


def _bool_env(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_port_open(host: str, port: int, timeout_sec: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, *, timeout_sec: float) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _is_port_open(host, port):
            return True
        time.sleep(1.0)
    return False


def _is_linux() -> bool:
    return platform.system().lower() == "linux"


def _is_ubuntu_22() -> bool:
    if not _is_linux():
        return False
    release = Path("/etc/os-release")
    if not release.exists():
        return False
    data: dict[str, str] = {}
    for raw in release.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        data[key.strip().upper()] = value.strip().strip('"')
    os_id = data.get("ID", "").lower()
    version_id = data.get("VERSION_ID", "")
    return os_id == "ubuntu" and version_id.startswith("22.04")


def _can_passwordless_sudo() -> bool:
    if not _is_linux():
        return False
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    if shutil.which("sudo") is None:
        return False
    probe = subprocess.run(
        ["sudo", "-n", "true"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    return probe.returncode == 0


def _sudo_exists() -> bool:
    return _is_linux() and shutil.which("sudo") is not None


def _run_privileged(cmd: list[str], *, step: str) -> bool:
    actual = list(cmd)
    capture = True
    if _is_linux() and hasattr(os, "geteuid") and os.geteuid() != 0:
        if _can_passwordless_sudo():
            actual = ["sudo", "-n", *cmd]
        elif _sudo_exists() and sys.stdin.isatty():
            log.warning("%s requires sudo, waiting for password prompt...", step)
            actual = ["sudo", *cmd]
            capture = False
        else:
            log.error(
                "%s requires sudo privilege. Run once with sudo:\n"
                "  sudo %s start.py",
                step,
                sys.executable,
            )
            return False

    result = subprocess.run(
        actual,
        cwd=str(ROOT),
        text=True,
        capture_output=capture,
    )
    if result.returncode != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        stdout = (getattr(result, "stdout", "") or "").strip()
        detail = stderr or stdout or f"exit={result.returncode}"
        log.error("%s failed: %s", step, detail)
        return False
    return True


def _docker_cli_exists() -> bool:
    return shutil.which("docker") is not None


def _run_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", *args]
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return result

    err = (result.stderr or "").lower()
    if _is_linux() and "permission denied" in err:
        if _can_passwordless_sudo():
            return subprocess.run(
                ["sudo", "-n", *cmd],
                cwd=str(ROOT),
                text=True,
                capture_output=True,
            )
        if _sudo_exists() and sys.stdin.isatty():
            log.warning("Docker requires sudo for current user, waiting for password prompt...")
            return subprocess.run(
                ["sudo", *cmd],
                cwd=str(ROOT),
                text=True,
            )
    return result


def _docker_available() -> bool:
    if not _docker_cli_exists():
        return False
    probe = _run_docker(["version"])
    return probe.returncode == 0


def _docker_container_exists(name: str) -> bool:
    inspect = _run_docker(
        [
            "ps",
            "-a",
            "--filter",
            f"name=^/{name}$",
            "--format",
            "{{.Names}}",
        ]
    )
    return inspect.returncode == 0 and name in (inspect.stdout or "").splitlines()


def _ensure_docker_runtime() -> bool:
    if _docker_available():
        return True

    if not _is_linux():
        return False

    if not _docker_cli_exists() and _is_ubuntu_22():
        log.warning("Docker not found, trying one-click install on Ubuntu 22.04")
        if not _run_privileged(["apt-get", "update"], step="apt-get update"):
            return False
        if not _run_privileged(
            ["apt-get", "install", "-y", "docker.io"],
            step="apt-get install docker.io",
        ):
            return False

    if _docker_cli_exists():
        _run_privileged(["systemctl", "enable", "--now", "docker"], step="start docker service")

    return _docker_available()


def _ensure_qdrant_with_docker(host: str, port: int) -> bool:
    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0"}
    if host not in local_hosts:
        return False
    if not _ensure_docker_runtime():
        return False

    if _docker_container_exists(QDRANT_CONTAINER):
        start = _run_docker(["start", QDRANT_CONTAINER])
        if start.returncode != 0:
            log.error("Failed to start qdrant container: %s", (start.stderr or "").strip())
            return False
    else:
        create_args = [
            "run",
            "-d",
            "--name",
            QDRANT_CONTAINER,
            "--restart",
            "unless-stopped",
            "-p",
            f"{port}:6333",
        ]
        if _is_linux():
            storage_dir = ROOT / "data" / "qdrant_storage"
            storage_dir.mkdir(parents=True, exist_ok=True)
            create_args.extend(["-v", f"{storage_dir}:/qdrant/storage"])
        create_args.append("qdrant/qdrant:latest")

        create = _run_docker(create_args)
        if create.returncode != 0:
            stderr = (create.stderr or "").strip()
            log.error("Failed to create qdrant container: %s", stderr)
            return False

    check_host = "127.0.0.1" if host in {"localhost", "0.0.0.0"} else host
    return _wait_port(check_host, port, timeout_sec=45.0)


def _parse_neo4j_bolt_uri(uri: str) -> tuple[str, int]:
    raw = (uri or "").strip() or "bolt://localhost:7687"
    if "://" not in raw:
        raw = "bolt://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname or "localhost"
    port = int(parsed.port or 7687)
    return host, port


def _ensure_neo4j_with_docker(*, host: str, bolt_port: int, user: str, password: str) -> bool:
    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0"}
    if host not in local_hosts:
        return False
    if not _ensure_docker_runtime():
        return False

    if _docker_container_exists(NEO4J_CONTAINER):
        start = _run_docker(["start", NEO4J_CONTAINER])
        if start.returncode != 0:
            log.error("Failed to start neo4j container: %s", (start.stderr or "").strip())
            return False
    else:
        create_args = [
            "run",
            "-d",
            "--name",
            NEO4J_CONTAINER,
            "--restart",
            "unless-stopped",
            "-p",
            f"{bolt_port}:7687",
            "-p",
            "7474:7474",
            "-e",
            f"NEO4J_AUTH={user}/{password}",
        ]
        if _is_linux():
            data_dir = ROOT / "data" / "neo4j_data"
            logs_dir = ROOT / "data" / "neo4j_logs"
            data_dir.mkdir(parents=True, exist_ok=True)
            logs_dir.mkdir(parents=True, exist_ok=True)
            create_args.extend(
                [
                    "-v",
                    f"{data_dir}:/data",
                    "-v",
                    f"{logs_dir}:/logs",
                ]
            )
        create_args.append("neo4j:5-community")

        create = _run_docker(create_args)
        if create.returncode != 0:
            stderr = (create.stderr or "").strip()
            log.error("Failed to create neo4j container: %s", stderr)
            return False

    check_host = "127.0.0.1" if host in {"localhost", "0.0.0.0"} else host
    return _wait_port(check_host, bolt_port, timeout_sec=90.0)


def ensure_qdrant_ready() -> None:
    """Ensure Memory v2 vector backend is reachable; try auto-start qdrant locally."""
    env = _load_runtime_env()
    backend = (env.get("MEMORY_VECTOR_BACKEND", "qdrant") or "").strip().lower() or "qdrant"
    if backend != "qdrant":
        raise RuntimeError("MEMORY_VECTOR_BACKEND must be qdrant for Memory v2.")

    host = (env.get("MEMORY_QDRANT_HOST", "localhost") or "").strip() or "localhost"
    port_raw = (env.get("MEMORY_QDRANT_PORT", "6333") or "").strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid MEMORY_QDRANT_PORT: {port_raw!r}") from exc

    check_host = "127.0.0.1" if host in {"localhost", "0.0.0.0"} else host
    if _is_port_open(check_host, port):
        log.info("Qdrant ready at %s:%s", host, port)
        return

    log.warning("Qdrant is not reachable at %s:%s, trying docker one-click start", host, port)
    if _ensure_qdrant_with_docker(host, port):
        log.info("Qdrant docker container is ready at %s:%s", host, port)
        return

    raise RuntimeError(
        "Qdrant is unavailable. Please start qdrant manually "
        f"(expected at {host}:{port}) and retry."
    )


def ensure_neo4j_ready() -> None:
    """Ensure optional graph DB is ready when KG is enabled."""
    env = _load_runtime_env()
    if not _bool_env(env.get("MEMORY_KG_ENABLED"), default=False):
        return

    uri = (env.get("MEMORY_KG_URI", "") or "").strip() or "bolt://localhost:7687"
    user = (env.get("MEMORY_KG_USER", "") or "").strip() or "neo4j"
    password = (env.get("MEMORY_KG_PASSWORD", "") or "").strip()
    if not password:
        raise RuntimeError("MEMORY_KG_PASSWORD is required when MEMORY_KG_ENABLED=true")

    host, bolt_port = _parse_neo4j_bolt_uri(uri)
    check_host = "127.0.0.1" if host in {"localhost", "0.0.0.0"} else host
    if _is_port_open(check_host, bolt_port):
        log.info("Neo4j ready at %s:%s", host, bolt_port)
        return

    log.warning("Neo4j is not reachable at %s:%s, trying docker one-click start", host, bolt_port)
    if _ensure_neo4j_with_docker(host=host, bolt_port=bolt_port, user=user, password=password):
        log.info("Neo4j docker container is ready at %s:%s", host, bolt_port)
        return

    raise RuntimeError(
        "Neo4j is unavailable. Please start neo4j manually "
        f"(expected bolt at {host}:{bolt_port}) and retry."
    )


def preflight() -> None:
    """Validate local runtime prerequisites before startup."""
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            shutil.copy(example, env_path)
            log.warning("Created .env from .env.example, please fill BOT_TOKEN first")
            print("\nPlease edit .env and set BOT_TOKEN, then retry.\n")
            sys.exit(1)
        log.error("Missing .env and .env.example")
        sys.exit(1)

    from dotenv import load_dotenv

    load_dotenv()
    configure_logging(force=True)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "your_bot_token_here":
        print("\nPlease set a valid BOT_TOKEN in .env and retry.\n")
        sys.exit(1)
    if not os.getenv("SUPER_ADMIN_ID", "").strip():
        log.warning("SUPER_ADMIN_ID is empty, group authorization commands will be unavailable")

    Path("data").mkdir(exist_ok=True)
    log.info("Preflight passed")


async def start_bot(settings, session_factory) -> None:
    """Start telegram polling and background memory tasks."""
    from bot.handlers import admin, commands, group
    from bot.loader import create_bot, dp
    from bot.middlewares.db import DbSessionMiddleware
    from bot.middlewares.logging_mw import LoggingMiddleware
    from bot.middlewares.throttle import ThrottleMiddleware
    from bot.services import memory_holder
    from bot.services.knowledge import KnowledgeService
    from bot.services.llm import LLMService
    from bot.services.memory import MemoryService

    dp["settings"] = settings
    dp["session_factory"] = session_factory

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        embed=settings.bot.embed_model,
    )

    memory = MemoryService(
        settings.bot,
        llm,
        session_factory=session_factory,
        memory_v2=settings.memory_v2,
    )
    await memory.bootstrap()
    memory_holder.init(memory)

    kb = KnowledgeService(settings.knowledge, llm)
    async with session_factory() as session:
        count = await kb.backfill_embeddings(session)
        if count:
            await session.commit()

    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(ThrottleMiddleware(rate_limit=0.0))
    dp.message.middleware(DbSessionMiddleware(session_factory))
    dp.callback_query.middleware(DbSessionMiddleware(session_factory))

    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(group.router)

    bot = create_bot(settings)
    log.info("Bot starting in polling mode")

    async def _periodic_memory_jobs() -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                stats = await memory.maybe_run_daily_memory_maintenance()
                if any(int(v) > 0 for v in stats.values()):
                    log.info(
                        "daily memory maintenance: groups=%d consolidated_messages=%d facts=%d preferences=%d pruned=%d",
                        stats.get("groups", 0),
                        stats.get("consolidated_messages", 0),
                        stats.get("facts", 0),
                        stats.get("preferences", 0),
                        stats.get("pruned", 0),
                    )
            except Exception:
                log.exception("daily memory maintenance failed")

    memory_task = asyncio.create_task(_periodic_memory_jobs(), name="memory-hourly-jobs")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=settings.bot.drop_pending_updates,
            handle_as_tasks=True,
            tasks_concurrency_limit=8,
        )
    finally:
        memory_task.cancel()
        with suppress(asyncio.CancelledError):
            await memory_task
        try:
            await memory.flush_background_tasks(timeout_sec=8.0)
            log.info("memory background index flushed")
        except Exception:
            log.exception("memory background flush failed")


async def main() -> None:
    from bot.config import load_settings
    from bot.db.engine import init_db

    settings = load_settings()
    engine, session_factory = await init_db(settings.database_url)

    print()
    print("=" * 50)
    print("  Smart Group Bot - Local Run")
    print("=" * 50)
    print("  Mode: Telegram polling")
    print("  Press Ctrl+C to stop")
    print("=" * 50)
    print()

    try:
        await start_bot(settings, session_factory)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.dispose()
        log.info("all services stopped")


if __name__ == "__main__":
    try:
        ensure_dependencies()
        preflight()
        ensure_qdrant_ready()
        ensure_neo4j_ready()
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExited.")
    except Exception as exc:
        log.error("Startup failed: %s", exc)
        sys.exit(1)
