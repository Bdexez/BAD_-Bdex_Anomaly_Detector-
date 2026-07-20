"""Socle commun aux readers hwmon.

Tous les capteurs hwmon exposent la même grammaire : `<canal><N>_input` pour
la valeur, `<canal><N>_label` (optionnel) pour le nom, et une unité fixée par
le canal. Écrire quatre fois la même boucle de découverte serait quatre fois
l'occasion de se tromper d'échelle — d'où cette classe.

Découverte au setup(), jamais au read() : le schéma SQL dépend de `fields`,
qui doit être figé avant le premier tick.
"""

from __future__ import annotations

import logging
import pathlib

from .. import registry
from .base import Reader

log = logging.getLogger(__name__)

#: canal -> (fichiers valeur possibles, facteur, suffixe de colonne)
#: hwmon est en unités entières : milli-degrés, milli-volts, micro-watts, Hz.
CHANNELS: dict[str, tuple[tuple[str, ...], float, str]] = {
    "temp": (("input",), 1e-3, ""),          # m°C  -> °C
    "in": (("input",), 1e-3, "_v"),          # mV   -> V
    "fan": (("input",), 1.0, "_rpm"),        # RPM
    "power": (("average", "input"), 1e-6, "_w"),   # µW -> W
    "freq": (("input",), 1e-6, "_mhz"),      # Hz   -> MHz
}


class HwmonReader(Reader):
    """Lit tous les canaux demandés d'un hwmon résolu par son `name`."""

    #: contenu attendu du fichier `name` (ex: "k10temp")
    hwmon_name: str = ""
    #: préfixe des colonnes ; par défaut hwmon_name
    prefix: str = ""
    #: canaux à collecter, dans l'ordre
    channels: tuple[str, ...] = ("temp",)
    #: True = colonnes nommées par index (temp1..temp6) même si un label existe.
    #: Utile quand les labels du driver sont faux ou absents.
    ignore_labels: bool = False

    def __init__(self, instance: int = 0):
        self.instance = instance
        self.name = self.hwmon_name if instance == 0 else f"{self.hwmon_name}#{instance}"
        self.dir: pathlib.Path | None = None
        self.fields = []
        self._paths: dict[str, tuple[pathlib.Path, float]] = {}

    @property
    def column_prefix(self) -> str:
        base = self.prefix or self.hwmon_name
        return base if self.instance == 0 else f"{base}{self.instance}"

    def setup(self) -> bool:
        devices = registry.find_all_hwmon(self.hwmon_name)
        if len(devices) <= self.instance:
            return False
        self.dir = devices[self.instance]
        self._paths = self._discover(self.dir)
        self.fields = list(self._paths)
        if not self.fields:
            log.warning("%s trouvé (%s) mais aucun canal lisible", self.name, self.dir)
            return False
        return True

    def _discover(self, hwmon: pathlib.Path) -> dict[str, tuple[pathlib.Path, float]]:
        found: dict[str, tuple[pathlib.Path, float]] = {}
        for channel in self.channels:
            suffixes, scale, unit = CHANNELS[channel]
            for index in self._channel_indexes(hwmon, channel):
                value_path = next(
                    (
                        p
                        for suffix in suffixes
                        if (p := hwmon / f"{channel}{index}_{suffix}").exists()
                    ),
                    None,
                )
                # Un canal peut n'exposer qu'un label (amdgpu: power1_label sans
                # power1_average). Pas de fichier valeur = pas de colonne.
                if value_path is None or registry.read_int(value_path) is None:
                    continue
                column = self._column_name(hwmon, channel, index, unit, found)
                found[column] = (value_path, scale)
        return found

    @staticmethod
    def _channel_indexes(hwmon: pathlib.Path, channel: str) -> list[int]:
        indexes = set()
        for path in hwmon.glob(f"{channel}*_*"):
            head = path.name.split("_", 1)[0]
            digits = head[len(channel):]
            if digits.isdigit():
                indexes.add(int(digits))
        return sorted(indexes)

    def _column_name(
        self,
        hwmon: pathlib.Path,
        channel: str,
        index: int,
        unit: str,
        taken: dict[str, object],
    ) -> str:
        label = None
        if not self.ignore_labels:
            raw = registry.read_text(hwmon / f"{channel}{index}_label")
            label = registry.slug(raw) if raw else None
        stem = label or f"{channel}{index}"
        column = f"{self.column_prefix}_{stem}{unit}"
        # Deux canaux peuvent porter le même label (deux "Composite").
        if column in taken:
            column = f"{self.column_prefix}_{stem}{index}{unit}"
        return column

    def read(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for column, (path, scale) in self._paths.items():
            raw = registry.read_int(path)
            out[column] = None if raw is None else raw * scale
        return out
