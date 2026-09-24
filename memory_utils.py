"""Small, bounded-memory helpers; no bot credentials or remote calls at import time."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

CHUNK_SIZE = 64 * 1024


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


async def stream_telegram_photo(photo, destination: Path) -> None:
    """Stream a normal Telegram get_file() photo to disk, never response.read()."""
    url = photo.file_path
    if not url or urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("Telegram photo has no HTTP download URL")
    destination = Path(destination)
    timeout = aiohttp.ClientTimeout(total=120, connect=10, sock_read=30)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout, raise_for_status=True, read_bufsize=CHUNK_SIZE,
            trust_env=True,
        ) as session:
            async with session.get(url) as response:
                with destination.open("wb") as output:
                    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                        output.write(chunk)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        destination.unlink(missing_ok=True)
        raise RuntimeError("Telegram photo download failed (HTTP/connection error)") from None
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def memory_snapshot() -> dict[str, int]:
    """Linux process RSS vs cgroup-v2 total/anonymous/file bytes, when available."""
    values = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                values["rss_bytes"] = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        values["cgroup_bytes"] = int(Path("/sys/fs/cgroup/memory.current").read_text())
        for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines():
            name, size = line.split()
            if name in ("anon", "file"):
                values[f"cgroup_{name}_bytes"] = int(size)
    except (OSError, ValueError):
        pass
    return values


async def memory_reporter(app) -> None:
    """One small stdout-only sample every five minutes; 0 disables reporting."""
    interval = env_int("MEMORY_LOG_INTERVAL", 300)
    if not interval:
        return
    logger = logging.getLogger(__name__)
    while True:
        sample = memory_snapshot()
        sample.update(
            tg_queue=app.update_queue.qsize(),
            tg_users=len(app.user_data),
            tg_chats=len(app.chat_data),
        )
        logger.info("[Memory] %s", " ".join(f"{k}={v}" for k, v in sample.items()))
        await asyncio.sleep(interval)
