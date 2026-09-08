"""Température NVMe via le hwmon `nvme`.

Ajout au périmètre initial : un SSD qui throttle thermiquement fait grimper la
pression I/O (cf. PsiReader) sans qu'aucune sonde CPU ne bouge. C'est
exactement la corrélation qu'un détecteur d'anomalies doit pouvoir apprendre,
et elle coûte ici deux lignes.
"""

from __future__ import annotations

from .hwmon import HwmonReader


class NvmeReader(HwmonReader):
    """Composite + sondes par capteur. Instancier une fois par disque.

    Plusieurs NVMe donnent plusieurs hwmon nommés `nvme` : l'instance 0 écrit
    dans nvme_*, l'instance 1 dans nvme1_*.
    """

    hwmon_name = "nvme"
    prefix = "nvme"
    channels = ("temp",)
