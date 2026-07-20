"""CPU : températures du die AMD, depuis le hwmon `k10temp`."""

from __future__ import annotations

from .hwmon import HwmonReader


class K10TempReader(HwmonReader):
    """Tctl et, quand le CPU les expose, Tccd1..N.

    Les colonnes viennent des labels (`temp1_label` = "Tctl") et non d'un
    numéro codé en dur : sur un Zen 4 desktop on a temp1=Tctl et temp3=Tccd1
    (temp2 n'existe pas), sur un Zen+ mobile on n'a que Tctl. Le même code
    doit produire k10temp_tctl [+ k10temp_tccd1] sans savoir sur quoi il tourne.

    Valeurs en milli-degrés → ×1e-3 (fait par HwmonReader).

    Tctl != Tccd1 : Tctl est le capteur de contrôle utilisé par le boost,
    Tccd1 la vraie température du die. Les deux sont intéressantes, elles ne
    divergent pas de la même façon quand un ventirad s'encrasse. Sur Zen 4
    desktop, Tctl n'a pas d'offset (contrairement aux Threadripper) : rien à
    corriger ici.
    """

    hwmon_name = "k10temp"
    prefix = "k10temp"
    channels = ("temp",)
