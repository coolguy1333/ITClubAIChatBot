"""Host hardware stats (CPU/RAM/GPU/VRAM) for the admin UI.

Works inside an LXC container: psutil reads the container's own cgroup
CPU/memory limits and usage, and nvidia-smi (if the GPU was passed through
into the container) reports GPU utilization + VRAM. Everything degrades
gracefully to "not available" if a piece isn't there.
"""

import shutil
import subprocess


def _cpu_ram():
    try:
        import psutil
    except ImportError:
        return {"cpuPercent": None, "ramUsedMB": None, "ramTotalMB": None,
                 "error": "psutil not installed (pip install psutil)"}
    try:
        vm = psutil.virtual_memory()
        return {
            "cpuPercent": psutil.cpu_percent(interval=0.2),
            "cpuCount": psutil.cpu_count(logical=True),
            "ramUsedMB": round(vm.used / 1024 / 1024),
            "ramTotalMB": round(vm.total / 1024 / 1024),
        }
    except Exception as e:
        return {"cpuPercent": None, "ramUsedMB": None, "ramTotalMB": None, "error": str(e)}


def _gpu():
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {"available": False}
    try:
        out = subprocess.check_output(
            [nvidia_smi,
             "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        gpus = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 4:
                continue
            name, util, used, total = parts
            gpus.append({
                "name": name,
                "gpuPercent": float(util),
                "vramUsedMB": float(used),
                "vramTotalMB": float(total),
            })
        return {"available": bool(gpus), "gpus": gpus}
    except Exception as e:
        return {"available": False, "error": str(e)}


def get_hw_stats():
    stats = _cpu_ram()
    stats["gpu"] = _gpu()
    return stats
