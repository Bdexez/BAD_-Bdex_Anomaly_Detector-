"""CPU et mémoire depuis /proc/stat, /proc/meminfo, /proc/loadavg, cpufreq."""

from __future__ import annotations

import pathlib
import re

from .. import registry
from .base import Reader

CPU_ROOT = pathlib.Path("/sys/devices/system/cpu")
_CPU_LINE_RE = re.compile(r"cpu(\d*)$")

#: Par ordre de préférence. cpuinfo_avg_freq (amd-pstate, noyaux récents) est
#: une *mesure* de la fréquence effective moyenne ; scaling_cur_freq n'est
#: qu'une consigne du gouverneur, et sur AMD P-State elle ment franchement.
#: La vraie fréquence effective générale demanderait de lire les MSR
#: (APERF/MPERF) — hors périmètre, mais c'est la limite honnête à connaître.
FREQ_FILES = ("cpuinfo_avg_freq", "scaling_cur_freq")


class ProcReader(Reader):
    """Utilisation globale et par thread, charge, mémoire, fréquence moyenne.

    /proc/stat donne des compteurs cumulés en jiffies : il faut dériver entre
    deux ticks, comme RAPL. L'état précédent vit dans self, et le premier
    read() renvoie None pour les utilisations.

        util = 1 - Δ(idle + iowait) / Δtotal

    Le nombre de threads est découvert dans setup(), pas codé en dur : le
    schéma d'une machine 8 threads ne doit pas hériter des colonnes d'une
    machine 12 threads.

    Les cores individuels comptent : un seul core qui throttle pendant que les
    autres tournent normalement est un signal que la moyenne écrase
    complètement.
    """

    name = "proc"

    def __init__(self, root: pathlib.Path = pathlib.Path("/proc")):
        self.root = root
        self.fields = []
        self._cpus: list[str] = []
        self._prev: dict[str, tuple[int, int]] = {}
        self._freq_paths: list[pathlib.Path] = []

    def setup(self) -> bool:
        stat = self._read_stat()
        if not stat:
            return False
        self._cpus = [key for key in stat if key != "cpu"]
        self._freq_paths = self._find_freq_paths()

        self.fields = ["cpu_util", "load1", "mem_used_mb", "swap_used_mb"]
        if self._freq_paths:
            self.fields.append("cpu_freq_avg")
        self.fields += [f"cpu{i}_util" for i in range(len(self._cpus))]
        return True

    def _find_freq_paths(self) -> list[pathlib.Path]:
        for filename in FREQ_FILES:
            paths = [
                p
                for p in sorted(CPU_ROOT.glob("cpu[0-9]*/cpufreq/" + filename))
                if registry.read_int(p) is not None
            ]
            if paths:
                return paths
        return []

    def _read_stat(self) -> dict[str, tuple[int, int]]:
        """{"cpu": (idle, total), "cpu0": ..., ...} en jiffies cumulés."""
        try:
            content = (self.root / "stat").read_text()
        except OSError:
            return {}
        out: dict[str, tuple[int, int]] = {}
        for line in content.splitlines():
            parts = line.split()
            if not parts or not _CPU_LINE_RE.fullmatch(parts[0]):
                continue
            try:
                values = [int(v) for v in parts[1:]]
            except ValueError:
                continue
            if len(values) < 5:
                continue
            # user nice system idle iowait irq softirq steal guest guest_nice
            idle = values[3] + values[4]
            # guest et guest_nice sont déjà comptés dans user/nice : les
            # additionner compterait le temps invité deux fois.
            total = sum(values[:8])
            out[parts[0]] = (idle, total)
        return out

    def _meminfo(self) -> dict[str, int]:
        try:
            content = (self.root / "meminfo").read_text()
        except OSError:
            return {}
        out: dict[str, int] = {}
        for line in content.splitlines():
            key, _, rest = line.partition(":")
            value = rest.split()
            if value and value[0].isdigit():
                out[key] = int(value[0])  # kB
        return out

    def _load1(self) -> float | None:
        text = registry.read_text(self.root / "loadavg")
        if not text:
            return None
        try:
            return float(text.split()[0])
        except (IndexError, ValueError):
            return None

    def read(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        stat = self._read_stat()

        for label, key in [("cpu_util", "cpu")] + [
            (f"cpu{i}_util", name) for i, name in enumerate(self._cpus)
        ]:
            out[label] = self._util(key, stat.get(key))

        out["load1"] = self._load1()

        mem = self._meminfo()
        total, available = mem.get("MemTotal"), mem.get("MemAvailable")
        out["mem_used_mb"] = None if total is None or available is None else (total - available) / 1024
        swap_total, swap_free = mem.get("SwapTotal"), mem.get("SwapFree")
        out["swap_used_mb"] = (
            None if swap_total is None or swap_free is None else (swap_total - swap_free) / 1024
        )

        if self._freq_paths:
            freqs = [f for p in self._freq_paths if (f := registry.read_int(p)) is not None]
            out["cpu_freq_avg"] = sum(freqs) / len(freqs) / 1000 if freqs else None  # kHz -> MHz
        return out

    def _util(self, key: str, current: tuple[int, int] | None) -> float | None:
        previous = self._prev.get(key)
        if current is None:
            self._prev.pop(key, None)
            return None
        self._prev[key] = current
        if previous is None:
            return None
        idle_delta = current[0] - previous[0]
        total_delta = current[1] - previous[1]
        if total_delta <= 0:
            return None
        return max(0.0, min(1.0, 1.0 - idle_delta / total_delta))
