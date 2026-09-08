"""Pressure Stall Information : /proc/pressure/{cpu,io,memory}."""

from __future__ import annotations

import pathlib

from .base import Reader

PRESSURE_ROOT = pathlib.Path("/proc/pressure")

#: (fichier, ligne, colonne de sortie).
#: `cpu` a bien une ligne `full` depuis 5.13, mais elle vaut structurellement 0
#: sur un système non contraint par un cgroup : on ne la collecte pas.
SOURCES: tuple[tuple[str, str, str], ...] = (
    ("cpu", "some", "psi_cpu_some"),
    ("io", "some", "psi_io_some"),
    ("io", "full", "psi_io_full"),
    ("memory", "some", "psi_mem_some"),
    ("memory", "full", "psi_mem_full"),
)


class PsiReader(Reader):
    """`some avg10` pour les trois ressources, plus `full avg10` pour io/memory.

    Format du fichier :
        some avg10=0.00 avg60=0.00 avg300=0.00 total=12902583
        full avg10=0.00 ...

    Pourquoi c'est le signal sous-estimé du lot : PSI mesure la CONTENTION,
    pas l'utilisation. Un CPU à 100 % peut avoir un PSI nul (personne
    n'attend). Un CPU à 40 % avec un PSI à 30 veut dire que des tâches sont
    bloquées à attendre. C'est exactement ce qui apparaît quand un système
    dérive alors que les métriques classiques restent vertes.

    On prend avg10 : à 1 Hz, `total` (cumulé, en µs) serait dérivable et plus
    précis, mais avg10 est déjà lissé par le noyau et suffit à 14 jours
    d'horizon. Le brut reste récupérable plus tard si besoin.

    PSI demande CONFIG_PSI_DEFAULT_DISABLE=n ou psi=1 au boot : setup() rend
    False quand /proc/pressure est absent.
    """

    name = "psi"

    def __init__(self, root: pathlib.Path | None = None):
        self.root = root or PRESSURE_ROOT
        self.fields = []
        self._sources: list[tuple[pathlib.Path, str, str]] = []

    def setup(self) -> bool:
        for filename, line, column in SOURCES:
            path = self.root / filename
            if self._parse(path).get(line) is not None:
                self._sources.append((path, line, column))
        self.fields = [column for _, _, column in self._sources]
        return bool(self.fields)

    @staticmethod
    def _parse(path: pathlib.Path) -> dict[str, float]:
        try:
            content = path.read_text()
        except OSError:
            return {}
        out: dict[str, float] = {}
        for raw in content.splitlines():
            parts = raw.split()
            if not parts:
                continue
            for token in parts[1:]:
                key, _, value = token.partition("=")
                if key == "avg10":
                    try:
                        out[parts[0]] = float(value)
                    except ValueError:
                        pass
        return out

    def read(self) -> dict[str, float | None]:
        cache: dict[pathlib.Path, dict[str, float]] = {}
        out: dict[str, float | None] = {}
        for path, line, column in self._sources:
            if path not in cache:
                cache[path] = self._parse(path)
            out[column] = cache[path].get(line)
        return out
