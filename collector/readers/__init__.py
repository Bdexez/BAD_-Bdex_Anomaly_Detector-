"""Sources de métriques. Un fichier par famille de capteurs."""

from __future__ import annotations

from .amdgpu import AmdGpuReader
from .base import Reader, ReaderState
from .gigabyte_wmi import GigabyteWmiReader
from .hwmon import HwmonReader
from .k10temp import K10TempReader
from .nvme import NvmeReader

__all__ = [
    "AmdGpuReader",
    "GigabyteWmiReader",
    "HwmonReader",
    "K10TempReader",
    "NvmeReader",
    "Reader",
    "ReaderState",
]
