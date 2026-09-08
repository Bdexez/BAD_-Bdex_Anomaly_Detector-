"""Compteurs d'erreurs matérielles exposés par le noyau.

Troisième étage de détection, indépendant du texte des messages noyau — et
c'est ce qui en fait la valeur :

* il fonctionne **sans privilèges**, là où /dev/kmsg exige root ;
* il attrape ce que le noyau comptabilise **sans forcément le journaliser**
  (une erreur PCIe corrigée n'écrit rien dans dmesg si le rate-limit a frappé,
  mais le compteur, lui, avance) ;
* un compteur est à la fois un **événement** (il bouge → ligne dans `events`)
  et une **feature** (son delta par tick est une colonne de `samples`).

Chaque source est découverte au démarrage : ce qui n'existe pas sur la machine
ne crée pas de colonne. Un portable AMD n'a ni `thermal_throttle` (Intel) ni
EDAC (pas d'ECC) ; un desktop avec carte dédiée aura des compteurs RAS GPU que
l'iGPU n'a pas.
"""

from __future__ import annotations

import logging
import pathlib
import re
from collections.abc import Callable
from typing import NamedTuple

from .. import errors, registry
from .base import Reader

log = logging.getLogger(__name__)

PROC = pathlib.Path("/proc")
SYS = pathlib.Path("/sys")


class Counter(NamedTuple):
    field: str
    kind: str
    severity: str
    note: str
    probe: Callable[[], int | None]


def _sum_int_files(paths: list[pathlib.Path]) -> int | None:
    """Somme de compteurs entiers répartis sur plusieurs fichiers."""
    values = [v for p in paths if (v := registry.read_int(p)) is not None]
    return sum(values) if values else None


def _sum_named_values(paths: list[pathlib.Path]) -> int | None:
    """Somme des valeurs de fichiers au format `Nom  valeur` par ligne.

    C'est la forme des compteurs AER (`RxErr 0`, `BadTLP 2`…) et RAS amdgpu
    (`ue: 0`, `ce: 3`).
    """
    total = None
    for path in paths:
        text = registry.read_text(path)
        if text is None:
            continue
        for line in text.splitlines():
            parts = line.replace(":", " ").split()
            if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
                total = (total or 0) + int(parts[-1])
    return total


def _interrupt_totals(path: pathlib.Path = PROC / "interrupts") -> dict[str, int]:
    """Totaux par ligne symbolique de /proc/interrupts, sommés sur tous les CPU.

    Les lignes qui nous intéressent ne sont pas numérotées : `MCE:`, `TRM:`,
    `THR:`, `DFR:`, `NMI:`. Elles sont lisibles sans privilèges, présentes sur
    Intel comme sur AMD, et remises à zéro à chaque boot.
    """
    text = registry.read_text(path)
    if text is None:
        return {}
    totals: dict[str, int] = {}
    for line in text.splitlines():
        label, _, rest = line.partition(":")
        label = label.strip()
        if not label or label.isdigit() or not rest:
            continue
        total = 0
        for token in rest.split():
            if token.lstrip("-").isdigit():
                total += int(token)
            else:
                break  # la suite est la description textuelle
        totals[label] = total
    return totals


class ErrorCounterReader(Reader):
    """Deltas par tick des compteurs d'erreurs matérielles.

    Un compteur qui avance déclenche un événement ; sa valeur au démarrage,
    si elle est non nulle, en déclenche un aussi. Une machine qui compte déjà
    4 000 erreurs ECC corrigées depuis le boot le dit dès le premier tick — ce
    que le suivi des deltas seuls ne dirait jamais.
    """

    name = "errcounters"

    def __init__(self, proc: pathlib.Path = PROC, sys_root: pathlib.Path = SYS):
        self.proc = proc
        self.sys = sys_root
        self.fields = []
        self.counters: list[Counter] = []
        self._prev: dict[str, int] = {}
        self._nvme_states: dict[pathlib.Path, str] = {}

    # -- découverte ---------------------------------------------------------

    def _candidates(self) -> list[Counter]:
        irq = lambda label: (lambda: _interrupt_totals(self.proc / "interrupts").get(label))

        edac = sorted(self.sys.glob("devices/system/edac/mc/mc*/[cu]e_count"))
        edac_ce = [p for p in edac if p.name.startswith("ce")]
        edac_ue = [p for p in edac if p.name.startswith("ue")]
        aer_cor = sorted(self.sys.glob("bus/pci/devices/*/aer_dev_correctable"))
        aer_non = sorted(self.sys.glob("bus/pci/devices/*/aer_dev_nonfatal"))
        aer_fat = sorted(self.sys.glob("bus/pci/devices/*/aer_dev_fatal"))
        core_thr = sorted(self.sys.glob("devices/system/cpu/cpu*/thermal_throttle/core_throttle_count"))
        pkg_thr = sorted(self.sys.glob("devices/system/cpu/cpu*/thermal_throttle/package_throttle_count"))
        gpu_ras = sorted(self.sys.glob("class/drm/card*/device/ras/*_err_count"))
        scsi_err = sorted(self.sys.glob("block/*/device/ioerr_cnt"))

        return [
            Counter(
                "err_mce", "mce", errors.CRITICAL,
                "Machine Check Exception comptée par le CPU lui-même. "
                "Indépendante du texte de dmesg, donc increvable.",
                irq("MCE"),
            ),
            Counter(
                "err_thermal_irq", "thermal_trip", errors.ERROR,
                "Interruption d'événement thermique (TRM) : le CPU a atteint "
                "son seuil et s'est bridé. Le signal recherché pour "
                "'ventirad encrassé' et 'flux d'air obstrué'.",
                irq("TRM"),
            ),
            Counter(
                "err_threshold_irq", "mce", errors.WARNING,
                "Seuil d'erreur APIC franchi (THR) : un compteur d'erreurs "
                "corrigées a dépassé sa limite.",
                irq("THR"),
            ),
            Counter(
                "err_deferred_irq", "mce", errors.ERROR,
                "Erreur différée AMD (DFR) : erreur détectée hors du contexte "
                "de l'instruction fautive.",
                irq("DFR"),
            ),
            Counter(
                "err_nmi", "nmi", errors.ERROR,
                "Interruption non masquable : alimentation, RAM, ou watchdog.",
                irq("NMI"),
            ),
            Counter(
                "err_apic", "kernel_error", errors.WARNING,
                "Erreurs d'I/O APIC (ERR).", irq("ERR"),
            ),
            Counter(
                "err_oom_kill", "oom", errors.ERROR,
                "Process tués faute de mémoire (compteur de /proc/vmstat).",
                lambda: self._vmstat().get("oom_kill"),
            ),
            Counter(
                "err_ecc_ce", "ecc_ce", errors.WARNING,
                "Erreurs mémoire corrigées par l'ECC (EDAC).",
                lambda: _sum_int_files(edac_ce),
            ),
            Counter(
                "err_ecc_ue", "ecc_ue", errors.CRITICAL,
                "Erreurs mémoire NON corrigées (EDAC) : donnée perdue.",
                lambda: _sum_int_files(edac_ue),
            ),
            Counter(
                "err_aer_corr", "pcie_corrected", errors.WARNING,
                "Erreurs PCIe corrigées par retransmission, sommées sur tous "
                "les périphériques. Avancent souvent en silence : dmesg "
                "rate-limite, le compteur non.",
                lambda: _sum_named_values(aer_cor),
            ),
            Counter(
                "err_aer_nonfatal", "pcie_fatal", errors.ERROR,
                "Erreurs PCIe non fatales non corrigées.",
                lambda: _sum_named_values(aer_non),
            ),
            Counter(
                "err_aer_fatal", "pcie_fatal", errors.CRITICAL,
                "Erreurs PCIe fatales : lien perdu.",
                lambda: _sum_named_values(aer_fat),
            ),
            Counter(
                "err_cpu_throttle", "thermal_trip", errors.ERROR,
                "Bridages thermiques par cœur (Intel uniquement).",
                lambda: _sum_int_files(core_thr),
            ),
            Counter(
                "err_pkg_throttle", "thermal_trip", errors.ERROR,
                "Bridages thermiques du package (Intel uniquement).",
                lambda: _sum_int_files(pkg_thr),
            ),
            Counter(
                "err_gpu_ras", "gpu_ecc", errors.CRITICAL,
                "Compteurs RAS du GPU AMD (ECC mémoire vidéo).",
                lambda: _sum_named_values(gpu_ras),
            ),
            Counter(
                "err_disk_io", "block_error", errors.ERROR,
                "Erreurs d'I/O comptées par la couche SCSI.",
                lambda: _sum_int_files(scsi_err),
            ),
        ]

    def _vmstat(self) -> dict[str, int]:
        text = registry.read_text(self.proc / "vmstat")
        if text is None:
            return {}
        out = {}
        for line in text.splitlines():
            key, _, value = line.partition(" ")
            if value.strip().lstrip("-").isdigit():
                out[key] = int(value)
        return out

    def setup(self) -> bool:
        for counter in self._candidates():
            try:
                value = counter.probe()
            except Exception as exc:
                log.debug("sonde %s indisponible: %s", counter.field, exc)
                continue
            if value is None:
                continue
            self.counters.append(counter)
            self._prev[counter.field] = value
            if value > 0:
                # État de départ : une machine qui compte déjà 4 000 erreurs ECC
                # depuis le boot doit le dire, pas attendre la 4 001e.
                self.emit(
                    counter.kind,
                    message=f"{counter.field}={value} au démarrage du collector",
                    severity=errors.WARNING,
                )

        self._nvme_states = {p: "" for p in sorted(self.sys.glob("class/nvme/nvme*/state"))}
        self.fields = [c.field for c in self.counters]
        if self._nvme_states:
            self.fields.append("err_nvme_state")
        return bool(self.fields)

    # -- lecture ------------------------------------------------------------

    def read(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for counter in self.counters:
            try:
                value = counter.probe()
            except Exception:
                out[counter.field] = None
                continue
            if value is None:
                out[counter.field] = None
                continue
            previous = self._prev.get(counter.field)
            self._prev[counter.field] = value
            if previous is None or value < previous:
                # Compteur remis à zéro (périphérique retiré puis remis) :
                # on repart de la nouvelle base sans inventer un delta négatif.
                out[counter.field] = 0.0
                continue
            delta = value - previous
            out[counter.field] = float(delta)
            if delta:
                self.emit(
                    counter.kind,
                    message=f"{counter.field} +{delta} (total {value}) — {counter.note}",
                    severity=counter.severity,
                )
        if self._nvme_states:
            out["err_nvme_state"] = float(self._check_nvme())
        return out

    def _check_nvme(self) -> int:
        """Un contrôleur NVMe hors de l'état `live` est une panne en cours."""
        degraded = 0
        for path, previous in self._nvme_states.items():
            state = registry.read_text(path) or "absent"
            if state != "live":
                degraded += 1
            if state != previous and previous != "":
                self.emit(
                    "nvme_error",
                    message=f"{path.parent.name}: état contrôleur {previous} -> {state}",
                    severity=errors.ERROR if state != "live" else errors.WARNING,
                )
            self._nvme_states[path] = state
        return degraded

    def describe(self) -> list[tuple[str, str]]:
        """(colonne, explication) des compteurs actifs — utilisé par `--doctor`."""
        return [(c.field, c.note) for c in self.counters]

    def missing(self) -> list[str]:
        """Compteurs du catalogue que cette machine n'expose pas.

        Savoir ce qui n'est PAS surveillé vaut autant que le reste : sans EDAC,
        une erreur mémoire corrigée ne laisse aucune trace nulle part.
        """
        active = {c.field for c in self.counters}
        return [c.field for c in self._candidates() if c.field not in active]
