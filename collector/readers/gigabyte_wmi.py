"""Carte mère Gigabyte : 6 sondes non labellisées, via le hwmon `gigabyte_wmi`."""

from __future__ import annotations

from .hwmon import HwmonReader


class GigabyteWmiReader(HwmonReader):
    """temp1..temp6_input, milli-degrés, sans signification documentée.

    Le driver n'expose aucun label — et quand il en expose un, il est
    générique. On force donc le nommage par index (gigabyte_temp1..6) : mieux
    vaut une colonne honnêtement anonyme qu'un nom inventé qui se retrouvera
    dans les features six mois plus tard.

    Protocole pour lever l'ambiguïté (à consigner dans le README) :
      1. `stress-ng --cpu $(nproc) --timeout 5m` → la sonde qui monte le plus
         vite et redescend le plus vite est côté VRM ;
      2. charge GPU seule → la sonde qui suit est côté PCIe/chipset ;
      3. machine au repos, fenêtre ouverte → la sonde qui suit l'ambiante.
    Renommer les colonnes après coup se fait par un ALTER TABLE RENAME COLUMN,
    ce n'est pas une raison pour deviner maintenant.
    """

    hwmon_name = "gigabyte_wmi"
    prefix = "gigabyte"
    channels = ("temp",)
    ignore_labels = True
