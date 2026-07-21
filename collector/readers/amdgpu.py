"""GPU AMD (dGPU ou iGPU) via le hwmon `amdgpu`.

Hors périmètre initial (le squelette visait une RTX 3060 Ti via NVML), mais
sur une machine à iGPU Vega c'est le seul reader GPU qui produise quoi que ce
soit — et un détecteur d'anomalies thermiques sans signal GPU est aveugle sur
la moitié du budget de puissance.
"""

from __future__ import annotations

import pathlib

from .. import registry
from .hwmon import HwmonReader


class AmdGpuReader(HwmonReader):
    """Températures, puissance (PPT), tensions, fréquence, plus l'occupation.

    Les canaux disponibles varient énormément : une dGPU RDNA expose
    temp1..3 (edge/junction/mem), power1_average, fan1_input ; une iGPU Vega
    n'expose que temp1, freq1 et deux tensions. La découverte générique de
    HwmonReader absorbe ça sans configuration.

    `gpu_busy_percent` ne vit pas dans hwmon mais dans le nœud DRM parent :
    c'est l'équivalent de nvmlDeviceGetUtilizationRates().gpu, et c'est la
    métrique qui distingue "chaud parce qu'il travaille" de "chaud sans
    raison" — donc on va la chercher.
    """

    hwmon_name = "amdgpu"
    prefix = "amdgpu"
    channels = ("temp", "power", "fan", "freq", "in")

    def setup(self) -> bool:
        if not super().setup():
            return False
        self._busy = self._find_busy(self.dir)
        if self._busy is not None:
            self.fields = [*self.fields, f"{self.column_prefix}_busy_pct"]
        return True

    @staticmethod
    def _find_busy(hwmon: pathlib.Path | None) -> pathlib.Path | None:
        if hwmon is None:
            return None
        # /sys/class/hwmon/hwmonN/device -> le device PCI, qui porte le compteur.
        candidate = hwmon / "device" / "gpu_busy_percent"
        return candidate if registry.read_int(candidate) is not None else None

    def read(self) -> dict[str, float | None]:
        out = super().read()
        if self._busy is not None:
            raw = registry.read_int(self._busy)
            out[f"{self.column_prefix}_busy_pct"] = None if raw is None else float(raw)
        return out
