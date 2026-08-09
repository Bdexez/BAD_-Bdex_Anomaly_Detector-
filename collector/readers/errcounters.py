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


