"""What this machine can run: GPU, VRAM, CPU and memory.

Read once at startup. nvidia-smi is the reliable source of VRAM on Windows (WMI
reports it through a 32-bit field that tops out at 4 GB), and CTranslate2 is
the authority on whether CUDA inference will actually work.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000


@dataclass
class SystemInfo:
    cpu: str
    threads: int
    ram_gb: float
    gpu: str | None
    vram_gb: float | None
    cuda: bool
    cuda_compute_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError:
        import platform

        return platform.processor() or "Unknown CPU"


def _ram_gb() -> float:
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0.0
    return round(status.ullTotalPhys / 2**30, 1)


def _nvidia_gpu() -> tuple[str | None, float | None]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode != 0 or not result.stdout.strip():
        return None, None

    # First GPU only: faster-whisper runs on device 0 unless told otherwise.
    parts = [part.strip() for part in result.stdout.strip().splitlines()[0].split(",")]
    name = parts[0] or None
    try:
        return name, round(float(parts[1]) / 1024, 1)
    except (IndexError, ValueError):
        return name, None


def detect() -> SystemInfo:
    import ctranslate2

    try:
        cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda = False
    compute_types = sorted(ctranslate2.get_supported_compute_types("cuda")) if cuda else []

    gpu, vram = _nvidia_gpu()
    info = SystemInfo(
        cpu=_cpu_name(),
        threads=os.cpu_count() or 1,
        ram_gb=_ram_gb(),
        gpu=gpu,
        vram_gb=vram,
        cuda=cuda,
        cuda_compute_types=compute_types,
    )
    log.info("system: %s", info)
    return info
