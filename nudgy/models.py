"""Which local model fits this computer.

CATALOGUE lists open models Nudgy has been tested with, best first. Each says how much graphics memory
(VRAM) it needs to run fully on the GPU, and how much ordinary memory (RAM) it needs on the CPU instead.
recommended() picks the best one this computer can run; settings "ai.local_model" overrides it.

When a better model comes out, it is added here (after testing it on Nudgy's own tasks), and `nudgy models`
shows that a better choice is available.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    name: str               # Ollama name
    tested: bool            # measured on Nudgy's tasks
    size_gb: float          # download size
    vram_gb: float          # graphics memory to run entirely on the GPU
    ram_gb: float           # memory to run on the CPU instead (slower)
    note: str


# Best first. "tested" ones were measured on Nudgy's own tasks (commands, drafts, steps; docs/models.md);
# the others are placed by size and the vendor's results, and still need that check.
CATALOGUE = [
    Model("qwen3.5:9b", False, 6.6, 8.0, 16.0, "best quality; needs an 8 GB graphics card or 16 GB of memory"),
    Model("gemma4:e4b", True, 6.6, 7.0, 12.0, "concise drafts and good steps; a little slower"),
    Model("qwen3.5:4b", True, 3.3, 4.0, 8.0, "fast and good all round; fits a 4 GB graphics card"),
    Model("qwen3.5:2b", False, 1.9, 2.5, 6.0, "for laptops without a graphics card"),
    Model("qwen3.5:0.8b", False, 1.0, 1.5, 4.0, "the smallest; for very old machines"),
]
FALLBACK = "qwen3.5:4b"


def gpu_memory_gb() -> float:
    """Graphics memory of the biggest NVIDIA or AMD card, or 0 when there is none (or it can't be read)."""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout
            sizes = [float(x) for x in out.split() if x.replace(".", "").isdigit()]
            if sizes:
                return max(sizes) / 1024
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    best = 0.0
    try:
        import glob
        for path in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):     # AMD
            with open(path) as f:
                best = max(best, int(f.read().strip()) / 1024 ** 3)
    except (OSError, ValueError):
        pass
    return best


def ram_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            found = re.search(r"MemTotal:\s+(\d+) kB", f.read())
        return int(found.group(1)) / 1024 ** 2 if found else 0.0
    except OSError:
        return 0.0


def recommended(vram: float | None = None, ram: float | None = None) -> Model:
    """The best catalogue model that runs fully on the GPU; else the best that fits in memory on the CPU."""
    vram = gpu_memory_gb() if vram is None else vram
    ram = ram_gb() if ram is None else ram
    for model in CATALOGUE:
        if vram >= model.vram_gb:
            return model
    for model in CATALOGUE:
        if model.vram_gb <= 2.5 and ram >= model.ram_gb:   # without a GPU only the small ones are quick enough
            return model
    return CATALOGUE[-1]


def installed(ollama_url: str = "http://127.0.0.1:11434") -> list[str]:
    """Models Ollama has, or [] if Ollama isn't running."""
    try:
        with urllib.request.urlopen(ollama_url.rstrip("/") + "/api/tags", timeout=5) as response:
            return [m["name"] for m in json.load(response).get("models", [])]
    except (OSError, ValueError, KeyError):
        return []


def current() -> str:
    """The local model Nudgy uses: your choice in settings, or the recommended one."""
    from . import settings as prefs
    chosen = prefs.section("ai")["local_model"]
    return chosen or recommended().name
