"""Contrat commun à tous les readers.

Un reader = une source de métriques. Il déclare ses colonnes, il se configure
au démarrage, il renvoie un dict à chaque tick. Il n'a JAMAIS le droit de
lever une exception dans read() : le collector doit survivre à la perte d'un
capteur.
"""

from __future__ import annotations

import abc
import logging
import math
from collections.abc import Callable

from ..storage import COLUMN_TYPES, check_identifier

log = logging.getLogger(__name__)


class Reader(abc.ABC):
    #: identifiant court, sert de préfixe aux colonnes (ex: "k10temp")
    name: str = ""

    #: colonnes produites, préfixées (ex: ["k10temp_tctl", "k10temp_tccd1"])
    #: DOIT être stable après setup() : le schéma en dépend. setup() a le droit
    #: de la remplir (découverte matérielle), plus personne après.
    fields: list[str] = []

    #: sous-ensemble de `fields` à stocker en TEXT plutôt qu'en REAL.
    #: Sert aux étiquettes brutes qu'on refuse d'encoder trop tôt
    #: (cf. ContextReader.active_window).
    text_fields: frozenset[str] = frozenset()

    def setup(self) -> bool:
        """Découverte et validation. Retourne False si la source est absente.

        Un reader qui retourne False est désactivé proprement au lieu de
        polluer la base avec des NULL sur 14 jours.
        """
        return True

    @abc.abstractmethod
    def read(self) -> dict[str, float | str | None]:
        """Un échantillon. Clés ⊆ self.fields. Valeurs None si indisponible."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def column_type(self, field: str) -> str:
        return "TEXT" if field in self.text_fields else "REAL"

    #: Injecté par le collector avant setup(). Signature :
    #: (source, kind, severity, message, ts_ms, dedup) -> bool
    emitter: Callable[[str, str, str, str, int | None, str | None], bool] | None = None

    def emit(
        self,
        kind: str,
        message: str = "",
        severity: str = "warning",
        ts: int | None = None,
        dedup: str | None = None,
    ) -> bool:
        """Signale un événement discret (erreur noyau, reset GPU, compteur qui bouge).

        Une série temporelle ne peut pas représenter un événement ponctuel : une
        MCE dure une microseconde et n'apparaîtra dans aucune colonne
        échantillonnée à 1 Hz. D'où ce canal séparé vers la table `events`.

        `ts` est celui de l'événement, pas celui du tick : une erreur retrouvée
        dans le tampon noyau au démarrage doit garder sa date d'origine, sinon
        toute corrélation avec les séries est fausse.
        """
        if self.emitter is None:
            log.info("[%s] %s: %s", self.name, kind, message)
            return False
        return self.emitter(self.name, kind, severity, message, ts, dedup)


class ReaderState:
    """Enveloppe un reader et gère ses pannes.

    3 échecs consécutifs → désactivation + event en base. On préfère perdre
    une source que de logger 86 400 tracebacks par jour.

    Filtre aussi les clés hors-schéma : un reader qui renvoie une colonne
    non déclarée ferait exploser l'INSERT (colonne inexistante) et emporterait
    tout le batch, y compris les données des autres readers.
    """

    MAX_FAILURES = 3
    RESERVED_FIELDS = frozenset({"sample_id", "ts", "boot_id"})

    def __init__(
        self,
        reader: Reader,
        on_disable: Callable[[str, str], None] | None = None,
    ):
        self.reader = reader
        self.failures = 0
        self.enabled = True
        self.on_disable = on_disable
        if not isinstance(reader.fields, (list, tuple)):
            raise ValueError("fields doit être une liste de noms de colonnes")
        self.fields = tuple(reader.fields)
        for field in self.fields:
            if not isinstance(field, str):
                raise ValueError(f"nom de colonne invalide: {field!r}")
            check_identifier(field)
            if field in self.RESERVED_FIELDS:
                raise ValueError(f"colonne réservée au collector: {field}")
        self._allowed = frozenset(self.fields)
        if len(self._allowed) != len(self.fields):
            raise ValueError("colonnes déclarées en double")
        if isinstance(reader.text_fields, str):
            raise ValueError("text_fields doit contenir des noms de colonnes")
        self._text_fields = frozenset(reader.text_fields)
        if not self._text_fields <= self._allowed:
            raise ValueError("text_fields contient des colonnes non déclarées")
        self.column_types = {field: reader.column_type(field) for field in self.fields}
        for field, kind in self.column_types.items():
            if kind not in COLUMN_TYPES:
                raise ValueError(f"type de colonne invalide pour {field}: {kind!r}")
            if (kind == "TEXT") != (field in self._text_fields):
                raise ValueError(f"type incohérent avec text_fields pour {field}")
        self._warned_unknown = False
        self._warned_invalid: set[str] = set()

    def read(self) -> dict[str, float | str | None]:
        if not self.enabled:
            return {}
        try:
            data = self.reader.read()
            if not isinstance(data, dict):
                raise TypeError(f"read() a renvoyé {type(data).__name__}, pas un dict")
        except Exception as exc:
            return self._fail(exc)

        unknown = data.keys() - self._allowed
        if unknown:
            if not self._warned_unknown:
                log.warning(
                    "reader %s renvoie des clés non déclarées, ignorées: %s",
                    self.reader.name,
                    ", ".join(sorted(map(repr, unknown))),
                )
                self._warned_unknown = True
            data = {k: v for k, v in data.items() if k in self._allowed}

        # Une valeur incompatible avec SQLite ne doit pas emporter les mesures
        # saines de tous les readers du batch. Les NaN/inf ne sont pas non plus
        # des observations exploitables : on conserve un trou explicite.
        clean = {}
        for field, value in data.items():
            if value is None:
                valid = True
            elif field in self._text_fields:
                valid = isinstance(value, str)
                if valid:
                    try:
                        value.encode("utf-8")
                    except UnicodeError:
                        valid = False
            elif isinstance(value, int):
                valid = -(2**63) <= value < 2**63
            else:
                valid = isinstance(value, float) and math.isfinite(value)
            clean[field] = value if valid else None
            if not valid and field not in self._warned_invalid:
                log.warning("reader %s: valeur invalide pour %s, remplacée par NULL",
                            self.reader.name, field)
                self._warned_invalid.add(field)

        self.failures = 0
        return clean

    def _fail(self, exc: Exception) -> dict[str, float | str | None]:
        self.failures += 1
        log.warning(
            "reader %s a échoué (%d): %s", self.reader.name, self.failures, exc
        )
        if self.failures >= self.MAX_FAILURES:
            self.enabled = False
            log.error("reader %s désactivé", self.reader.name)
            if self.on_disable is not None:
                try:
                    self.on_disable(self.reader.name, f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001 - le log ne doit jamais tuer la boucle
                    log.exception("impossible d'enregistrer la panne de %s", self.reader.name)
        return {}
