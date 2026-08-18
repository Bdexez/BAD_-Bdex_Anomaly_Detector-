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


def _cpu_model() -> str:
    text = pathlib.Path("/proc/cpuinfo").read_text() if pathlib.Path("/proc/cpuinfo").exists() else ""
    for line in text.splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.machine()


def run_doctor() -> None:
    """`--doctor` : ce que cette machine permet de mesurer et de détecter.

    À lancer sur chaque machine : la couverture n'est pas la même partout, et
    c'est cette sortie qui le dit — y compris ce qui N'EST PAS surveillé.
    """
    print(f"Machine   {platform.node()} · noyau {platform.release()}")
    print(f"CPU       {_cpu_model()}")
    print(f"hwmon     {', '.join(sorted(list_hwmon())) or 'aucun'}")
    print(f"Droits    {'root' if os.geteuid() == 0 else 'utilisateur ordinaire'}")

    print("\nSources de métriques")
    counters = None
    kmsg = None
    for reader in candidate_readers(boot="doctor"):
        try:
            ok = reader.setup()
        except Exception as exc:
            print(f"  [!!] {reader.name:14} erreur de configuration: {exc}")
            continue
        if isinstance(reader, R.ErrorCounterReader) and ok:
            counters = reader
        if isinstance(reader, R.KmsgReader):
            kmsg = reader
        marker = "OK" if ok else "--"
        detail = ", ".join(reader.fields) if ok else "absent de cette machine"
        print(f"  [{marker}] {reader.name:14} {detail[:96]}")
        if reader is not counters and reader is not kmsg:
            reader.close()

    print("\nDétection d'erreurs")
    kinds = error_catalogue.catalogue()
    print(f"  Étage 1  messages noyau : {len(error_catalogue.RULES)} règles, {len(kinds)} genres")
    if kmsg is not None and kmsg.backend is not None:
        print(f"           source active : {kmsg.backend.name}")
        print(f"           tampon du boot courant : {kmsg.backfilled} erreur(s)")
    else:
        reason = kmsg.unavailable_reason if kmsg else "reader absent"
        print(f"           INDISPONIBLE : {reason}")
    print(f"  Étage 2  attrape-tout : toute erreur noyau de priorité <= "
          f"{error_catalogue.KERNEL_ERROR_LEVEL} non classée")
    if counters is not None:
        print(f"  Étage 3  compteurs matériels : {len(counters.counters)} actifs")
        for field, note in counters.describe():
            print(f"           {field:20} {note.splitlines()[0][:70]}")
        absent = counters.missing()
        if absent:
            print(f"           non exposés ici : {', '.join(absent)}")
    else:
        print("  Étage 3  compteurs matériels : aucun")

    for reader in (kmsg, counters):
        if reader is not None:
            reader.close()


def run_scan(top: int = 6) -> None:
    """`--scan` : classe tout le tampon noyau du boot courant, sans écrire en base.

    C'est l'outil à lancer sur chaque machine pour voir *ce qui est réellement
    détecté* — et pour repérer un faux positif avant qu'il ne pollue 14 jours
    de dataset.
    """
    found: list[tuple[str, str, str]] = []

    def collect(source, kind, severity, message, ts=None, dedup=None):
        found.append((kind, severity, message))
        return True

    for reader in (R.KmsgReader(boot="scan"), R.ErrorCounterReader()):
        reader.emitter = collect
        try:
            if not reader.setup():
                reason = getattr(reader, "unavailable_reason", "") or "indisponible"
                print(f"[--] {reader.name}: {reason}")
            reader.close()
        except Exception as exc:
            print(f"[!!] {reader.name}: {exc}")

    if not found:
        print("Aucune erreur détectée sur le boot courant.")
        return

    groups: dict[str, list[tuple[str, str]]] = {}
    for kind, severity, message in found:
        groups.setdefault(kind, []).append((severity, message))

    order = {error_catalogue.CRITICAL: 0, error_catalogue.ERROR: 1}
    ranked = sorted(
        groups.items(), key=lambda kv: (order.get(kv[1][0][0], 2), -len(kv[1]))
    )
    print(f"{len(found)} erreur(s) classée(s) sur le boot courant :\n")
    for kind, entries in ranked:
        severity = entries[0][0]
        print(f"  {severity.upper():8} {kind:18} x{len(entries)}")
        for _, message in entries[:top]:
            print(f"           {message[:110]}")
        if len(entries) > top:
            print(f"           ... et {len(entries) - top} autre(s)")
    criticals = sum(1 for _, sev, _ in found if sev == error_catalogue.CRITICAL)
    print(f"\nVerdict : {criticals} critique(s). "
          + ("Rien de bloquant." if not criticals else "À examiner avant de collecter."))


def main() -> None:
    ap = argparse.ArgumentParser(description="BAD — collecte de télémétrie matérielle")
    ap.add_argument("--db", default="data/metrics.db")
    ap.add_argument("--period", type=float, default=1.0, help="secondes entre deux samples")
    ap.add_argument("--duration", type=float, help="arrêt automatique après N secondes")
    ap.add_argument("--schema", type=pathlib.Path, default=SCHEMA)
    ap.add_argument("--doctor", "--list-sensors", dest="doctor", action="store_true",
                    help="ce que cette machine permet de mesurer et de détecter, puis sortie")
    ap.add_argument("--scan", action="store_true",
                    help="classe les erreurs du boot courant sans rien écrire en base")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.doctor:
        run_doctor()
        return

    if args.scan:
        run_scan()
        return

    if args.period <= 0:
        ap.error("--period doit être > 0")

    collector = Collector(args.db, args.period, args.schema)
    signal.signal(signal.SIGINT, collector.stop)
    signal.signal(signal.SIGTERM, collector.stop)
    collector.run(duration=args.duration)


if __name__ == "__main__":
    main()
