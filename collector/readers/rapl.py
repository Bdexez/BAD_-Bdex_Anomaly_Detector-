"""Puissance CPU dérivée des compteurs RAPL (/sys/class/powercap)."""

from __future__ import annotations

import logging
import pathlib
import re
import time

from .. import registry
from .base import Reader

log = logging.getLogger(__name__)

#: Au-delà, c'est un artefact (reset de compteur au resume, dt aberrant) et
#: pas un CPU. Aucun package x86 grand public ne tient 1 kW.
MAX_PLAUSIBLE_W = 1000.0

_ZONE_RE = re.compile(r"intel-rapl:(\d+)(?::(\d+))?$")


class RaplReader(Reader):
    """Puissance par zone RAPL, en watts.

    Malgré le nom "intel", intel_rapl_msr pilote aussi les MSR AMD : sur un
    Ryzen on trouve bien /sys/class/powercap/intel-rapl:0 (package-0).

    energy_uj est un compteur d'ÉNERGIE cumulée en µJ, pas une puissance.
    On dérive :

        P (W) = ΔE (µJ) / Δt (µs)

    Trois pièges, tous déjà payés :

    1. WRAP. Le compteur reboucle à max_energy_range_uj (~262 J, soit ~4 s à
       65 W : ça arrive en permanence). Quand E2 < E1 :
           ΔE = (max - E1) + E2
       Sans ça, une puissance négative aberrante toutes les quelques secondes,
       et le détecteur d'anomalies passe sa vie à détecter ce bug-là.

    2. PERMISSIONS. energy_uj est en 0400 root depuis Platypus (side-channel
       permettant d'inférer des clés crypto via la conso). Le daemon tourne en
       root via systemd ; ailleurs, setup() renvoie False au lieu d'échouer
       86 400 fois par jour.

    3. SUSPEND. Au resume, le compteur repart de zéro alors que
       CLOCK_MONOTONIC n'a pas avancé : ΔE énorme / Δt minuscule. Le wrap ne
       distingue pas ce cas, donc on borne à MAX_PLAUSIBLE_W et on renvoie
       None — un trou dans les données vaut mieux qu'un pic inventé.

    Le premier read() renvoie None : une dérivée a besoin de deux points.
    """

    name = "rapl"

    def __init__(self, root: pathlib.Path | None = None):
        self.root = root or registry.POWERCAP_ROOT
        self.fields = []
        self._zones: dict[str, pathlib.Path] = {}
        self._max: dict[str, int] = {}
        self._prev: dict[str, tuple[int, float]] = {}

    def setup(self) -> bool:
        zones = self._discover()
        if not zones:
            log.info("aucune zone RAPL lisible (droits root ? CPU non supporté ?)")
            return False
        self._zones = zones
        self.fields = list(zones)
        return True

    def _discover(self) -> dict[str, pathlib.Path]:
        try:
            dirs = sorted(self.root.glob("intel-rapl:*"))
        except OSError:
            return {}

        matches = [(d, m) for d in dirs if (m := _ZONE_RE.fullmatch(d.name))]
        # Une zone sans second index est un package ; deux packages (bi-socket)
        # imposent de préfixer les colonnes pour ne pas les confondre.
        multi = sum(1 for _, m in matches if m.group(2) is None) > 1

        zones: dict[str, pathlib.Path] = {}
        for zone, match in matches:
            energy = zone / "energy_uj"
            # Une seule lecture de test : elle échoue en PermissionError si on
            # n'est pas root, ce qui est le cas nominal hors systemd.
            if registry.read_int(energy) is None:
                continue
            maximum = registry.read_int(zone / "max_energy_range_uj")
            if not maximum:
                log.warning("%s sans max_energy_range_uj, zone ignorée", zone)
                continue
            field = self._field_name(match, registry.read_text(zone / "name"), multi)
            zones[field] = energy
            self._max[field] = maximum
        return zones

    @staticmethod
    def _field_name(match: re.Match[str], zone_name: str | None, multi: bool) -> str:
        pkg_index, sub_index = match.group(1), match.group(2)
        raw = zone_name or (f"zone{pkg_index}" if sub_index is None else f"sub{sub_index}")
        stem = "pkg" if raw.startswith("package") else registry.slug(raw)
        if multi:
            stem = f"pkg{pkg_index}_{stem}" if stem != "pkg" else f"pkg{pkg_index}"
        return f"rapl_{stem}_watts"

    def read(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for field, path in self._zones.items():
            energy = registry.read_int(path)
            now = time.monotonic()
            if energy is None:
                out[field] = None
                self._prev.pop(field, None)
                continue
            previous = self._prev.get(field)
            self._prev[field] = (energy, now)
            out[field] = None if previous is None else self._watts(field, previous, energy, now)
        return out

    def _watts(
        self, field: str, previous: tuple[int, float], energy: int, now: float
    ) -> float | None:
        prev_energy, prev_time = previous
        dt = now - prev_time
        if dt <= 0:
            return None
        delta = energy - prev_energy
        if delta < 0:  # wrap du compteur
            delta += self._max[field]
        watts = (delta * 1e-6) / dt
        if watts > MAX_PLAUSIBLE_W:
            log.debug("%s: %.0f W écarté (reset de compteur ?)", field, watts)
            return None
        return watts
