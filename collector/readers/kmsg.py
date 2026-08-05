"""Erreurs noyau : lecture continue du tampon de messages du noyau.

Deux backends, choisis au démarrage selon ce que la machine autorise :

* **/dev/kmsg** (préféré) — pas de fork, pas de dépendance à systemd, et chaque
  enregistrement porte un numéro de séquence qui sert de clé de déduplication.
  Demande CAP_SYSLOG dès que `kernel.dmesg_restrict=1` (défaut sur Arch), donc
  en pratique root.
* **journalctl -k -f** — un seul process pour toute la vie du daemon (surtout
  pas un fork par tick), utilisable sans privilèges quand l'utilisateur est
  dans le groupe `systemd-journal`. Clé de déduplication : le curseur journald.

Dans les deux cas, le démarrage **rejoue tout le tampon du boot courant** : les
erreurs survenues avant le lancement du collector (au boot, ou pendant que le
service était arrêté) sont récupérées avec leur date d'origine. La clé de
déduplication rend ce rejeu idempotent.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import pathlib
import shutil
import subprocess
import time
from collections.abc import Iterator
from typing import NamedTuple

from .. import errors
from .base import Reader

log = logging.getLogger(__name__)

KMSG_PATH = pathlib.Path("/dev/kmsg")

#: Plafond par tick. Une tempête de messages (boucle de reset GPU, disque en
#: perdition) ne doit pas bloquer la boucle de collecte : le surplus est compté
#: dans la métrique et résumé en un seul événement.
MAX_RECORDS_PER_TICK = 2000
MAX_EVENTS_PER_TICK = 50

#: Le rejeu est une opération bornée et distincte du suivi : on lit jusqu'à
#: épuisement de la source, pas jusqu'à un silence supposé. `journalctl -f`
#: livre son historique au compte-gouttes — s'arrêter au premier blanc en
#: raterait l'essentiel.
MAX_BACKFILL_RECORDS = 50_000
BACKFILL_TIMEOUT = 30.0


class Record(NamedTuple):
    ts_ms: int
    level: int
    text: str
    dedup: str


def _boot_epoch_ms() -> float:
    """Epoch (ms) correspondant à t=0 de l'horloge monotone.

    Les horodatages kmsg sont en µs depuis le boot. La conversion dérive après
    une veille (l'horloge monotone ne compte pas le temps suspendu) : les
    événements d'avant la veille peuvent être datés de quelques minutes en
    avance. Acceptable pour corréler à 1 Hz, à savoir pour ne pas s'étonner.
    """
    return (time.time() - time.monotonic()) * 1000.0


class _KmsgBackend:
    """Lecture directe de /dev/kmsg, sans fork."""

    name = "/dev/kmsg"

    def __init__(self, path: pathlib.Path = KMSG_PATH, boot: str = ""):
        # À l'ouverture, la position est sur le plus ancien enregistrement encore
        # présent : la première salve de lectures constitue donc le rejeu.
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.boot = boot
        self.origin = _boot_epoch_ms()
        self._overrun_logged = False

    def records(self, limit: int) -> Iterator[Record]:
        for _ in range(limit):
            try:
                raw = os.read(self.fd, 8192)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno == errno.EPIPE:
                    # Le tampon circulaire a écrasé notre position. On a perdu
                    # des messages ; la lecture suivante repart proprement.
                    if not self._overrun_logged:
                        log.warning("tampon kmsg dépassé, messages perdus")
                        self._overrun_logged = True
                    continue
                raise
            if not raw:
                return
            record = self._parse(raw.decode("utf-8", "replace"))
            if record is not None:
                yield record

    def _parse(self, raw: str) -> Record | None:
        # "priorité,séquence,horodatage_us,drapeau;texte\n clé=valeur..."
        prefix, _, rest = raw.partition(";")
        parts = prefix.split(",")
        if len(parts) < 3:
            return None
        try:
            priority, sequence, ts_us = int(parts[0]), parts[1], int(parts[2])
        except ValueError:
            return None
        text = rest.split("\n", 1)[0].strip()
        return Record(
            ts_ms=int(self.origin + ts_us / 1000.0),
            level=priority & 7,  # priorité = facilité * 8 + niveau
            text=text,
            dedup=f"{self.boot}:{sequence}",
        )

    def backfill(self) -> Iterator[Record]:
        """À l'ouverture, la position est sur le plus ancien enregistrement
        encore présent : tout le tampon est déjà là, sans attente."""
        yield from self.records(MAX_BACKFILL_RECORDS)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass




