"""Host hardware stats (CPU/RAM/GPU/VRAM) for the admin UI.

Works inside an LXC container: psutil reads the container's own cgroup
CPU/memory limits and usage, and nvidia-smi (if the GPU was passed through
into the container) reports GPU utilization + VRAM. Everything degrades
gracefully to "not available" if a piece isn't there.
"""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

_POWER_LOG = Path(__file__).parent / "power_usage.json"
_power_lock = threading.Lock()
_MAX_GAP_HOURS = 0.05     # ~3 min cap per sample so a long gap (restart/sleep) can't fake a huge jump
DEFAULT_POWER_LIMIT_W = 80.0   # this rig's laptop 3050 caps around 80W; used when nvidia-smi reports none


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None   # nvidia-smi reports "[N/A]" on GPUs without power sensors


def _accumulate_energy(power_w):
    """Track cumulative energy draw (Wh) for the primary GPU, persisted to
    disk so the running total survives bot restarts."""
    if power_w is None:
        return None
    now = time.time()
    with _power_lock:
        try:
            state = json.loads(_POWER_LOG.read_text())
        except Exception:
            state = {"totalWh": 0.0, "lastTs": None}
        last_ts = state.get("lastTs")
        if last_ts:
            elapsed_hours = min(max(0.0, now - last_ts) / 3600, _MAX_GAP_HOURS)
            state["totalWh"] = state.get("totalWh", 0.0) + power_w * elapsed_hours
        state["lastTs"] = now
        try:
            _POWER_LOG.write_text(json.dumps(state))
        except Exception as e:
            print(f"[hwstats] couldn't save power log: {e}")
        return round(state["totalWh"], 3)


def _cpu_ram():
    try:
        import psutil
    except ImportError:
        return {"cpuPercent": None, "ramUsedMB": None, "ramTotalMB": None,
                 "error": "psutil not installed (pip install psutil)"}
    try:
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "cpuPercent": psutil.cpu_percent(interval=0.2),
            "cpuCount": psutil.cpu_count(logical=True),
            "ramUsedMB": round(vm.used / 1024 / 1024),
            "ramTotalMB": round(vm.total / 1024 / 1024),
            "swapUsedMB": round(swap.used / 1024 / 1024),
            "swapTotalMB": round(swap.total / 1024 / 1024),
        }
    except Exception as e:
        return {"cpuPercent": None, "ramUsedMB": None, "ramTotalMB": None,
                 "swapUsedMB": None, "swapTotalMB": None, "error": str(e)}


def _gpu():
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {"available": False}
    try:
        out = subprocess.check_output(
            [nvidia_smi,
             "--query-gpu=name,utilization.gpu,memory.used,memory.total,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        gpus = []
        for i, line in enumerate(out.splitlines()):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            name, util, used, total = parts[:4]
            power_draw = _to_float(parts[4]) if len(parts) > 4 else None
            power_limit = _to_float(parts[5]) if len(parts) > 5 else None
            if power_limit is None:
                power_limit = DEFAULT_POWER_LIMIT_W   # nvidia-smi doesn't report a limit on this GPU
            gpu_entry = {
                "name": name,
                "gpuPercent": float(util),
                "vramUsedMB": float(used),
                "vramTotalMB": float(total),
                "powerDrawW": power_draw,
                "powerLimitW": power_limit,
            }
            if i == 0:
                # only track cumulative energy for the primary GPU
                gpu_entry["energyTotalWh"] = _accumulate_energy(power_draw)
            gpus.append(gpu_entry)
        return {"available": bool(gpus), "gpus": gpus}
    except Exception as e:
        return {"available": False, "error": str(e)}


def get_hw_stats():
    stats = _cpu_ram()
    stats["gpu"] = _gpu()
    return stats
