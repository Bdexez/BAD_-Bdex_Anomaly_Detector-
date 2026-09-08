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


class _JournalBackend:
    """`journalctl -k -b -f` en JSON : un process, pas un par tick."""

    name = "journalctl"
    #: Suivi seul : `--since=now` évite de re-livrer l'historique, que
    #: backfill() a déjà lu de façon déterministe.
    FOLLOW = ("journalctl", "-k", "-f", "-o", "json", "--no-pager", "--since=now")
    #: Rejeu : process court qui se termine, donc lecture jusqu'à EOF.
    REPLAY = ("journalctl", "-k", "-b", "-o", "json", "--no-pager")

    def __init__(self):
        self.proc = subprocess.Popen(
            self.FOLLOW,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        os.set_blocking(self.proc.stdout.fileno(), False)
        self._buffer = b""

    def records(self, limit: int) -> Iterator[Record]:
        if self.proc.poll() is not None:
            raise OSError(f"journalctl s'est arrêté (code {self.proc.returncode})")
        while True:
            try:
                chunk = self.proc.stdout.read(65536)
            except BlockingIOError:
                chunk = None
            if not chunk:
                break
            self._buffer += chunk
            if len(self._buffer) > 8 << 20:  # garde-fou : 8 Mo de retard
                self._buffer = self._buffer[-(1 << 20):]

        count = 0
        while count < limit and b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            record = self._parse(line)
            if record is not None:
                count += 1
                yield record

    @staticmethod
    def _parse(line: bytes) -> Record | None:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        message = entry.get("MESSAGE")
        if isinstance(message, list):  # journald encode le binaire en liste d'octets
            message = bytes(message).decode("utf-8", "replace")
        if not isinstance(message, str):
            return None
        try:
            ts_ms = int(entry.get("__REALTIME_TIMESTAMP", 0)) // 1000
            level = int(entry.get("PRIORITY", 6))
        except (TypeError, ValueError):
            return None
        cursor = entry.get("__CURSOR")
        return Record(ts_ms=ts_ms, level=level, text=message.strip(), dedup=cursor)

    def backfill(self) -> Iterator[Record]:
        """Relit le boot courant via un process qui se termine.

        Lire l'historique dans le flux `-f` obligerait à deviner quand il est
        fini ; ici l'EOF le dit.
        """
        try:
            done = subprocess.run(
                self.REPLAY,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=BACKFILL_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("rejeu journald impossible: %s", exc)
            return
        for line in done.stdout.splitlines()[:MAX_BACKFILL_RECORDS]:
            record = self._parse(line)
            if record is not None:
                yield record

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()


class KmsgReader(Reader):
    """Classe chaque message noyau et enregistre les erreurs dans `events`.

    Les colonnes produites comptent les erreurs **de ce tick**. Un événement
    ponctuel ne peut pas vivre dans une série à 1 Hz — d'où la table `events` —
    mais son décompte, lui, est une feature légitime : une rafale d'erreurs
    corrigées juste avant un gel est exactement le motif qu'on veut apprendre.
    """

    name = "kmsg"
    fields = ["kmsg_errors", "kmsg_criticals", "kmsg_messages"]

    def __init__(self, boot: str = "", backend=None):
        self.boot = boot
        self.backend = backend
        self.unavailable_reason = ""
        self.backfilled = 0

    def setup(self) -> bool:
        if self.backend is None:
            self.backend = self._open_backend()
        if self.backend is None:
            return False
        # Rejeu du tampon existant : les erreurs du boot, et celles survenues
        # pendant que le service était arrêté, ont autant de valeur que les
        # suivantes. La clé de déduplication rend l'opération rejouable.
        counts = self._consume(self.backend.backfill())
        self.backfilled = int(counts["kmsg_errors"] + counts["kmsg_criticals"])
        if self.backfilled:
            log.info(
                "%d erreur(s) noyau récupérée(s) dans le tampon existant", self.backfilled
            )
        return True

    def _open_backend(self):
        try:
            return _KmsgBackend(boot=self.boot)
        except PermissionError:
            reason = "/dev/kmsg refusé (kernel.dmesg_restrict=1, il faut root)"
        except OSError as exc:
            reason = f"/dev/kmsg indisponible ({exc})"

        if shutil.which("journalctl"):
            try:
                backend = _JournalBackend()
            except OSError as exc:
                self.unavailable_reason = f"{reason} ; journalctl a échoué ({exc})"
                return None
            log.info("%s ; repli sur journalctl", reason)
            return backend

        self.unavailable_reason = f"{reason} ; journalctl absent"
        return None

    def _consume(self, records: Iterator[Record]) -> dict[str, float]:
        """Classe un flux d'enregistrements, enregistre les erreurs, compte."""
        counts = {"kmsg_errors": 0.0, "kmsg_criticals": 0.0, "kmsg_messages": 0.0}
        stored = 0
        suppressed = 0
        for record in records:
            counts["kmsg_messages"] += 1
            rule = errors.classify(record.text, record.level)
            if rule is None:
                continue
            if rule.severity == errors.CRITICAL:
                counts["kmsg_criticals"] += 1
            else:
                counts["kmsg_errors"] += 1

            if stored >= MAX_EVENTS_PER_TICK:
                suppressed += 1
                continue
            message = record.text
            if rule.kind == "gpu_xid" and (detail := errors.describe_xid(message)):
                message = f"{detail} — {message}"
            if self.emit(
                rule.kind,
                message=message,
                severity=rule.severity,
                ts=record.ts_ms,
                dedup=record.dedup,
            ):
                stored += 1

        if suppressed:
            # Une tempête reste un fait à enregistrer, même sans garder chaque ligne.
            self.emit(
                "event_storm",
                message=f"{suppressed} message(s) d'erreur supplémentaires non détaillés",
                severity=errors.WARNING,
            )
        return counts

    def read(self) -> dict[str, float | None]:
        return self._consume(self.backend.records(MAX_RECORDS_PER_TICK))

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()
            self.backend = None
