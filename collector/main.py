"""Boucle de collecte à cadence fixe.

Lancement :
    sudo python -m collector.main --db data/metrics.db --period 1.0

Sans root, RAPL est simplement désactivé (energy_uj est en 0400) : le reste
collecte normalement.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import platform
import signal
import time

from . import errors as error_catalogue
from . import readers as R
from .readers.base import Reader, ReaderState
from .registry import boot_id, find_all_hwmon, list_hwmon
from .storage import Storage

log = logging.getLogger("collector")

#: Compromis perte/usure : à 1 Hz, 30 samples = ~30 s de perte max sur kill -9.
#: Un commit par seconde userait le SSD pour rien.
FLUSH_EVERY = 30
#: ...mais avec --period 60, 30 samples feraient 30 min de données en RAM.
#: On flushe donc aussi au temps, le premier des deux qui tombe.
FLUSH_INTERVAL = 30.0
#: Garde-fou si la base devient inécrivable : on jette plutôt que d'OOM.
MAX_BUFFER = 10_000

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"


def candidate_readers(boot: str = "") -> list[Reader]:
    """Tous les readers envisagés, dans l'ordre où ils écriront leurs colonnes.

    Aucun n'est obligatoire : chacun décide dans setup() s'il a quelque chose
    à offrir sur cette machine. C'est ce qui permet au même dépôt de tourner
    sur un desktop 7600X + RTX 3060 Ti et sur un portable Ryzen à iGPU.
    """
    return [
        R.KmsgReader(boot=boot),
        R.ErrorCounterReader(),
        R.K10TempReader(),
        R.GigabyteWmiReader(),
        R.RaplReader(),
        R.NvmlReader(),
        R.AmdGpuReader(),
        *(R.NvmeReader(i) for i in range(len(find_all_hwmon("nvme")))),
        R.PsiReader(),
        R.ProcReader(),
        R.ContextReader(),
    ]


def build_readers(on_disable=None, emitter=None, boot: str = "") -> list[ReaderState]:
    """Instancie et teste chaque reader ; ne garde que ceux qui répondent.

    L'émetteur d'événements est branché AVANT setup() : le rejeu du tampon
    noyau a lieu pendant la configuration, et ses erreurs doivent atterrir en
    base comme les autres.
    """
    active: list[ReaderState] = []
    for reader in candidate_readers(boot):
        reader.emitter = emitter
        try:
            ok = reader.setup()
        except Exception as exc:
            log.error("setup %s a échoué: %s", reader.name, exc)
            continue
        if ok:
            active.append(ReaderState(reader, on_disable=on_disable))
            log.info("reader actif: %s -> %s", reader.name, ", ".join(reader.fields))
        else:
            log.info("reader indisponible: %s", reader.name)
    return active


def now_ms() -> int:
    return int(time.time() * 1000)


class Collector:
    def __init__(self, db: str, period: float, schema: pathlib.Path = SCHEMA):
        self.storage = Storage(db, schema)
        self.period = period
        self.running = True
        # Le boot_id est résolu avant les readers : KmsgReader s'en sert comme
        # préfixe de clé de déduplication (les numéros de séquence kmsg ne sont
        # uniques qu'à l'intérieur d'un boot).
        self.boot = boot_id()
        self.storage.register_boot(self.boot, now_ms(), platform.release())
        self.readers = build_readers(
            on_disable=self._record_failure,
            emitter=self._store_event,
            boot=self.boot,
        )

        for state in self.readers:
            reader = state.reader
            self.storage.ensure_columns(
                reader.fields, {f: reader.column_type(f) for f in reader.fields}
            )

        log.info("hwmon détectés: %s", ", ".join(sorted(list_hwmon())) or "aucun")
        self.storage.add_event(now_ms(), "collector", "start", "info", f"period={period}s")

    def _store_event(
        self,
        source: str,
        kind: str,
        severity: str,
        message: str,
        ts: int | None = None,
        dedup: str | None = None,
    ) -> bool:
        """Canal d'événements offert aux readers. Retourne False sur doublon."""
        stored = self.storage.add_event(ts or now_ms(), source, kind, severity, message, dedup)
        if stored and severity in (error_catalogue.CRITICAL, error_catalogue.ERROR):
            log.warning("[%s] %s: %s", severity, kind, message[:200])
        return stored

    def _record_failure(self, name: str, message: str) -> None:
        self.storage.add_event(now_ms(), "collector", "reader_fail", "warning", f"{name}: {message}")

    def stop(self, *_):
        log.info("arrêt demandé")
        self.running = False

    def tick(self) -> dict:
        row: dict = {"ts": now_ms(), "boot_id": self.boot}
        for state in self.readers:
            row.update(state.read())
        return row

    def flush(self, buffer: list[dict]) -> None:
        if not buffer:
            return
        try:
            self.storage.insert_samples(buffer)
        except Exception:
            # Base pleine, disque en lecture seule, corruption : on log et on
            # continue. Perdre un batch vaut mieux que perdre le daemon — et
            # les prochains ticks peuvent très bien repasser.
            log.exception("écriture de %d samples impossible", len(buffer))
        buffer.clear()

    def run(self, duration: float | None = None) -> None:
        buffer: list[dict] = []
        # Cadence sans dérive : on vise des deadlines absolues, on ne fait pas
        # sleep(period) après un travail de durée variable — sinon on perd
        # quelques ms par tick et les timestamps glissent sur 14 jours.
        start = time.monotonic()
        deadline = start
        last_flush = start
        try:
            while self.running:
                deadline += self.period
                buffer.append(self.tick())

                now = time.monotonic()
                if len(buffer) >= FLUSH_EVERY or now - last_flush >= FLUSH_INTERVAL:
                    self.flush(buffer)
                    last_flush = now
                elif len(buffer) >= MAX_BUFFER:
                    log.error("buffer saturé (%d), samples abandonnés", len(buffer))
                    buffer.clear()

                if duration is not None and now - start >= duration:
                    break

                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    # On a pris du retard (I/O lent, système saturé). On
                    # resynchronise au lieu d'accumuler une dette et de partir
                    # en boucle serrée.
                    log.debug("tick en retard de %.3fs", -delay)
                    deadline = time.monotonic()
        except KeyboardInterrupt:
            log.info("interruption clavier")
        finally:
            self.close(buffer)

    def close(self, buffer: list[dict] | None = None) -> None:
        self.flush(buffer or [])
        for state in self.readers:
            try:
                state.reader.close()
            except Exception:
                log.exception("close() de %s a échoué", state.reader.name)
        self.storage.add_event(now_ms(), "collector", "stop", "info", "")
        log.info("%d samples en base", self.storage.count_samples())
        self.storage.close()
        log.info("terminé proprement")










