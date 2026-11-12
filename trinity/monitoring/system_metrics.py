"""Host metrics (CPU, memory, disk) via psutil."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil


def collect(data_dir: Path) -> dict[str, Any]:
    proc = psutil.Process()
    disk = psutil.disk_usage(str(data_dir if data_dir.exists() else Path.cwd()))
    return {
        "cpu_pct": psutil.cpu_percent(interval=None),
        "mem_pct": psutil.virtual_memory().percent,
        "proc_rss_mb": round(proc.memory_info().rss / 1e6, 1),
        "disk_free_gb": round(disk.free / 1e9, 2),
        "disk_used_pct": disk.percent,
    }


def warnings(metrics: dict[str, Any]) -> list[str]:
    out = []
    if metrics["disk_free_gb"] < 1.0:
        out.append(f"low disk space: {metrics['disk_free_gb']} GB free")
    if metrics["mem_pct"] > 90:
        out.append(f"high memory usage: {metrics['mem_pct']}%")
    return out
