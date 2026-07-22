"""GPU NVIDIA via NVML (nvidia-ml-py), pas nvidia-smi."""

from __future__ import annotations

import logging
from collections.abc import Callable

from .base import Reader

log = logging.getLogger(__name__)


class NvmlReader(Reader):
    """Températures, puissance, horloges, occupation et raisons de throttling.

    Pourquoi NVML et pas nvidia-smi : nvidia-smi fork un process et met
    ~200 ms. À 1 Hz on passerait 20 % du CPU à mesurer le CPU. NVML est un
    appel de bibliothèque, ~50 µs.

    `gpu_throttle_mask` est la métrique la plus précieuse du lot : le hardware
    dit lui-même POURQUOI il se limite (thermique, power cap, voltage
    reliability). C'est un label gratuit pour le modèle. On stocke le bitmask
    brut en float et on le décomposera en flags au feature engineering — le
    décomposer maintenant reviendrait à choisir les features avant d'avoir vu
    les données.

    Sur GeForce Ampere (3060 Ti), hotspot et memory junction ne sont pas
    exposés : ce n'est pas un problème de configuration, NVML ne les publie
    pas. Chaque métrique est donc sondée une fois dans setup() et retirée de
    `fields` si le driver répond NotSupported — plutôt qu'une colonne de NULL
    sur 14 jours.
    """

    name = "nvml"

    def __init__(self, index: int = 0):
        self.index = index
        self.fields = []
        self._nvml = None
        self._handle = None
        self._probes: dict[str, Callable[[], float | None]] = {}

    def setup(self) -> bool:
        try:
            import pynvml
        except ImportError:
            log.info("pynvml absent (pip install nvidia-ml-py), reader nvml désactivé")
            return False

        try:
            pynvml.nvmlInit()
        except Exception as exc:  # NVMLError_LibraryNotFound, driver absent...
            log.info("nvmlInit a échoué (%s), reader nvml désactivé", exc)
            return False

        self._nvml = pynvml
        try:
            if pynvml.nvmlDeviceGetCount() <= self.index:
                raise IndexError(f"pas de GPU NVIDIA d'index {self.index}")
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(self.index)
        except Exception as exc:
            log.info("aucun GPU NVIDIA utilisable (%s)", exc)
            self.close()
            return False

        self._probes = self._supported(self._candidates(pynvml, self._handle))
        self.fields = list(self._probes)
        if not self.fields:
            self.close()
            return False
        return True

    @staticmethod
    def _candidates(nv, h) -> dict[str, Callable[[], float | None]]:
        # nvidia-ml-py a renommé ClocksThrottleReasons -> ClocksEventReasons.
        throttle = getattr(
            nv,
            "nvmlDeviceGetCurrentClocksEventReasons",
            getattr(nv, "nvmlDeviceGetCurrentClocksThrottleReasons", None),
        )
        probes: dict[str, Callable[[], float | None]] = {
            "gpu_temp": lambda: float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)),
            "gpu_power_w": lambda: nv.nvmlDeviceGetPowerUsage(h) / 1000.0,
            "gpu_clock_sm": lambda: float(nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_SM)),
            "gpu_clock_mem": lambda: float(nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_MEM)),
            "gpu_util": lambda: float(nv.nvmlDeviceGetUtilizationRates(h).gpu),
            "gpu_mem_util": lambda: float(nv.nvmlDeviceGetUtilizationRates(h).memory),
            "gpu_fan_pct": lambda: float(nv.nvmlDeviceGetFanSpeed(h)),
            "gpu_mem_used_mb": lambda: nv.nvmlDeviceGetMemoryInfo(h).used / (1024 * 1024),
        }
        if throttle is not None:
            probes["gpu_throttle_mask"] = lambda: float(throttle(h))
        return probes

    @staticmethod
    def _supported(
        probes: dict[str, Callable[[], float | None]]
    ) -> dict[str, Callable[[], float | None]]:
        kept = {}
        for field, probe in probes.items():
            try:
                probe()
            except Exception as exc:
                log.info("métrique %s non supportée par ce GPU (%s)", field, exc)
                continue
            kept[field] = probe
        return kept

    def read(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for field, probe in self._probes.items():
            try:
                out[field] = probe()
            except Exception:
                # Une métrique qui disparaît (Xid, reset GPU) ne doit pas faire
                # échouer le reader entier : ReaderState le désactiverait au
                # bout de 3 ticks alors que les autres métriques sont bonnes.
                out[field] = None
        return out

    def close(self) -> None:
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:
                pass
            self._nvml = None
            self._handle = None
