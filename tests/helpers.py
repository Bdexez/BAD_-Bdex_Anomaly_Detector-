"""Fabriques de faux sysfs/procfs.

On ne teste pas le matériel : on teste que le code lit correctement une
arborescence donnée. Ce qui veut dire qu'on peut tester le wrap RAPL sans
attendre 4 secondes de charge, et la découverte gigabyte_wmi sans carte
Gigabyte.
"""

from __future__ import annotations

import pathlib


def write(path: pathlib.Path, content: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def make_hwmon(root: pathlib.Path, index: int, name: str, files: dict[str, str]):
    hwmon = root / f"hwmon{index}"
    write(hwmon / "name", name + "\n")
    for filename, content in files.items():
        write(hwmon / filename, content)
    return hwmon


def make_proc_stat(pid: int, comm: str, utime: int, stime: int, starttime: int) -> str:
    fields = ["S"] + ["0"] * 29
    fields[11] = str(utime)     # champ 14
    fields[12] = str(stime)     # champ 15
    fields[19] = str(starttime)  # champ 22
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"
