"""Small, dependency-free runtime diagnostics for the local stand.

The product has to run on Windows as well as Linux.  Keep the collector in
the standard library: unavailable host counters are returned as ``None``
instead of making the administrator screen fail.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


STARTED_MONOTONIC = time.monotonic()
_events: deque[dict] = deque(maxlen=500)
_installed = False
_sample_lock = threading.Lock()
_last_wall = time.monotonic()
_last_cpu = time.process_time()

_CREDENTIALS = re.compile(r"(://[^:/\s]+:)([^@/\s]+)(@)")
_TOKENS = re.compile(
    r"(?i)\b(session_secret|llm_api_key|authorization|password)\b\s*[:=]\s*([^\s,;]+)"
)


def _safe(message: str) -> str:
    message = _CREDENTIALS.sub(r"\1[скрыто]\3", message)
    message = _TOKENS.sub(r"\1=[скрыто]", message)
    return message.replace("\x00", "")[:2000]


class DiagnosticHandler(logging.Handler):
    """Bounded warning/error journal safe enough to expose to an admin."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _events.append({
                "at": datetime.fromtimestamp(record.created, timezone.utc),
                "level": record.levelname,
                "source": record.name[:120],
                "message": _safe(record.getMessage()),
            })
        except Exception:  # logging must never take the service down
            pass


def install_diagnostics() -> None:
    global _installed
    if _installed:
        return
    handler = DiagnosticHandler(level=logging.WARNING)
    logging.getLogger().addHandler(handler)
    _installed = True


def recent_events(*, limit: int = 100) -> list[dict]:
    return list(_events)[-max(1, min(limit, 500)):][::-1]


def _rss_bytes() -> int | None:
    try:
        if sys.platform.startswith("linux"):
            resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            )
            return int(counters.WorkingSetSize)
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except (OSError, ValueError, AttributeError, ImportError, IndexError):
        return None


def _memory() -> tuple[int | None, int | None]:
    try:
        if sys.platform.startswith("linux"):
            fields: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                name, value = line.split(":", 1)
                fields[name] = int(value.strip().split()[0]) * 1024
            return fields.get("MemTotal"), fields.get("MemAvailable")
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
            return int(status.total_physical), int(status.available_physical)
        page = int(os.sysconf("SC_PAGE_SIZE"))
        return int(os.sysconf("SC_PHYS_PAGES")) * page, None
    except (OSError, ValueError, AttributeError, ImportError):
        return None, None


def sample_metrics() -> dict:
    """Return a cheap point-in-time sample; the first CPU sample is zero."""
    global _last_wall, _last_cpu
    now = time.monotonic()
    cpu_now = time.process_time()
    with _sample_lock:
        wall_delta = max(now - _last_wall, 0.001)
        cpu_percent = max(0.0, (cpu_now - _last_cpu) / wall_delta * 100.0)
        _last_wall, _last_cpu = now, cpu_now

    cores = os.cpu_count() or 1
    try:
        load_1m_percent = os.getloadavg()[0] / cores * 100.0
    except (AttributeError, OSError):
        load_1m_percent = None
    memory_total, memory_available = _memory()
    disk = shutil.disk_usage(Path.cwd())
    return {
        "at": datetime.now(timezone.utc),
        "uptime_seconds": max(0.0, now - STARTED_MONOTONIC),
        "cpu_percent": round(cpu_percent, 2),
        "load_1m_percent": round(load_1m_percent, 2) if load_1m_percent is not None else None,
        "cpu_cores": cores,
        "rss_bytes": _rss_bytes(),
        "memory_total_bytes": memory_total,
        "memory_available_bytes": memory_available,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "threads": threading.active_count(),
    }
