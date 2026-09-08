"""Résolution des chemins sysfs.

Piège n°1 de ce genre de projet : /sys/class/hwmon/hwmon2 est k10temp
aujourd'hui, et hwmon4 après le prochain reboot. L'ordre dépend de l'ordre
de probe des drivers, qui n'est pas déterministe. On résout donc toujours
par le contenu du fichier `name`, jamais par le numéro.

Corollaire moins évident : un `name` n'est pas unique non plus. Deux SSD NVMe
donnent deux hwmon nommés `nvme`. find_hwmon() renvoie donc le premier dans
un ordre *numérique* stable, et find_all_hwmon() renvoie la liste complète.
"""

from __future__ import annotations

import logging
import pathlib
import re

log = logging.getLogger(__name__)

HWMON_ROOT = pathlib.Path("/sys/class/hwmon")
POWERCAP_ROOT = pathlib.Path("/sys/class/powercap")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """"Tctl" -> "tctl", "Composite 0" -> "composite_0", "S0-in" -> "s0_in".

    Sert à fabriquer des noms de colonnes SQL à partir de labels sysfs qui,
    eux, ne sont contraints par rien.
    """
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_")


def _hwmon_index(path: pathlib.Path) -> tuple[int, str]:
    """Clé de tri : hwmon2 avant hwmon10 (sorted() lexicographique fait l'inverse)."""
    match = re.fullmatch(r"hwmon(\d+)", path.name)
    return (int(match.group(1)) if match else 1 << 30, path.name)


def hwmon_dirs() -> list[pathlib.Path]:
    try:
        return sorted(HWMON_ROOT.glob("hwmon*"), key=_hwmon_index)
    except OSError:
        return []


def hwmon_name(hwmon: pathlib.Path) -> str | None:
    return read_text(hwmon / "name")


def find_hwmon(name: str) -> pathlib.Path | None:
    """Retourne le premier répertoire hwmon dont `name` vaut `name`, ou None.

    >>> find_hwmon("k10temp")
    PosixPath('/sys/class/hwmon/hwmon4')
    """
    for hwmon in hwmon_dirs():
        if hwmon_name(hwmon) == name:
            return hwmon
    return None


def find_all_hwmon(name: str) -> list[pathlib.Path]:
    """Tous les hwmon portant ce `name` (deux NVMe = deux hwmon `nvme`)."""
    return [h for h in hwmon_dirs() if hwmon_name(h) == name]


def list_hwmon() -> dict[str, list[pathlib.Path]]:
    """Inventaire {name: [paths]}. Loggé au démarrage pour savoir ce qu'on a trouvé.

    Renvoie une liste par nom, et non un chemin : écraser les doublons ferait
    disparaître silencieusement le second NVMe de l'inventaire.
    """
    found: dict[str, list[pathlib.Path]] = {}
    for hwmon in hwmon_dirs():
        name = hwmon_name(hwmon)
        if name:
            found.setdefault(name, []).append(hwmon)
    return found


def read_text(path: pathlib.Path) -> str | None:
    """Lecture tolérante d'un fichier sysfs texte. None si illisible."""
    try:
        return path.read_text().strip()
    except OSError:
        return None


def read_int(path: pathlib.Path) -> int | None:
    """Lecture tolérante d'un fichier sysfs entier. None si illisible.

    Un capteur peut disparaître à chaud (GPU en runtime PM, périphérique USB
    débranché). On ne veut pas que le collector meure pour ça.
    """
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_float(path: pathlib.Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def boot_id() -> str:
    """Identifiant du boot courant.

    Tolérant : sur un système sans /proc/sys/kernel/random/boot_id (conteneur,
    autre OS), on dégrade en identifiant synthétique plutôt que de tuer le
    collector au démarrage — la colonne est NOT NULL, elle doit valoir quelque
    chose.
    """
    value = read_text(pathlib.Path("/proc/sys/kernel/random/boot_id"))
    if value:
        return value
    log.warning("boot_id indisponible, identifiant synthétique utilisé")
    return "unknown-boot"
