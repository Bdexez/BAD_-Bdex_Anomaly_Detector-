"""Contexte d'usage : ce que la machine était en train de faire.

LE reader qui fait la différence. Sans contexte, le modèle apprend « GPU chaud
= anomalie » et spamme dès qu'on lance un jeu. Avec contexte, il apprend « GPU
chaud alors que rien ne tourne = anomalie ». C'est toute la différence entre un
projet qui marche et un notebook Kaggle.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import socket
import time

from .. import registry
from .base import Reader

log = logging.getLogger(__name__)

CLK_TCK = os.sysconf("SC_CLK_TCK")

#: Fragments cherchés dans la classe/le titre de la fenêtre active (minuscules).
#: Volontairement court : chaque entrée est une hypothèse sur le dataset, et une
#: heuristique fausse pollue tout ce qui sera collecté derrière.
GAMING_HINTS = (
    "steam_app", "gamescope", "lutris", "heroic", "bottles",
    "wine", "proton", "minecraft", "stalker",
)


class ContextReader(Reader):
    """uptime, process le plus gourmand, nombre de process, activité GPU, fenêtre active.

    `is_gaming` est une décision de design, pas une évidence. On la garde
    tri-valuée : 1 (indice trouvé), 0 (fenêtre active connue et non-jeu),
    NULL (aucune information sur la fenêtre active — daemon lancé hors session
    graphique, compositeur injoignable). Écrire 0 dans ce dernier cas
    reviendrait à affirmer « pas de jeu » alors qu'on ne sait rien : c'est
    précisément le genre de faux label qui rend un dataset inexploitable.

    Pour la même raison on stocke `active_window` et `top_proc_name` en TEXT
    brut : on ne peut pas ré-étiqueter des données qu'on n'a pas collectées.
    L'heuristique, elle, se recalcule à volonté au feature engineering.

    Pas de dépendance à psutil : tout vient de /proc. Une dépendance de moins à
    installer dans l'environnement root du daemon, et le scan de /proc coûte
    quelques millisecondes par tick sur ~500 process.

    Le compositeur est interrogé par socket IPC (pas `hyprctl`, qui forke) et
    au plus toutes les WINDOW_TTL secondes : la fenêtre active ne change pas
    assez vite pour justifier un aller-retour par tick.
    """

    name = "context"
    fields = [
        "uptime_s",
        "top_proc_cpu",
        "top_proc_name",
        "n_procs",
        "is_gaming",
        "is_compositing",
        "active_window",
    ]
    text_fields = frozenset({"top_proc_name", "active_window"})

    #: secondes entre deux interrogations du compositeur
    WINDOW_TTL = 5.0

    def __init__(self, proc_root: pathlib.Path = pathlib.Path("/proc")):
        self.proc_root = proc_root
        self._prev_cpu: dict[int, tuple[int, int]] = {}
        self._prev_time: float | None = None
        self._window: str | None = None
        self._window_at = 0.0
        self._gpu_busy: pathlib.Path | None = None

    def setup(self) -> bool:
        self._gpu_busy = self._find_gpu_busy()
        if self._gpu_busy is None:
            log.info("aucun compteur d'occupation GPU trouvé, is_compositing sera NULL")
        return (self.proc_root / "uptime").exists()

    @staticmethod
    def _find_gpu_busy() -> pathlib.Path | None:
        """gpu_busy_percent : amdgpu l'expose côté DRM. NVIDIA passe par NVML."""
        for path in sorted(pathlib.Path("/sys/class/drm").glob("card*/device/gpu_busy_percent")):
            if registry.read_int(path) is not None:
                return path
        return None

    # -- fenêtre active -----------------------------------------------------

    @staticmethod
    def _hypr_sockets() -> list[pathlib.Path]:
        """Le daemon tourne en root : il n'hérite ni de HYPRLAND_INSTANCE_SIGNATURE
        ni de XDG_RUNTIME_DIR. On retrouve donc la socket par le système de fichiers."""
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        roots = [pathlib.Path(runtime)] if runtime else list(pathlib.Path("/run/user").glob("*"))
        sockets = [s for root in roots for s in root.glob("hypr/*/.socket.sock")]
        return sorted(sockets, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

    def _query_window(self) -> str | None:
        for sock_path in self._hypr_sockets():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.2)
                    sock.connect(str(sock_path))
                    sock.sendall(b"j/activewindow")
                    chunks = []
                    while chunk := sock.recv(8192):
                        chunks.append(chunk)
            except OSError:
                continue
            try:
                window = json.loads(b"".join(chunks) or b"{}")
            except json.JSONDecodeError:
                continue
            if not window:
                return ""  # session vivante, mais aucune fenêtre focalisée
            return f"{window.get('class', '')}|{window.get('title', '')}"
        return None

    def _active_window(self) -> str | None:
        now = time.monotonic()
        if now - self._window_at >= self.WINDOW_TTL:
            self._window = self._query_window()
            self._window_at = now
        return self._window

    # -- /proc --------------------------------------------------------------

    def _scan_procs(self, now: float) -> tuple[int, float | None, str | None]:
        """(nombre de process, %CPU du plus gourmand, son nom)."""
        current: dict[int, tuple[int, int]] = {}
        names: dict[int, str] = {}
        count = 0
        for entry in self.proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            count += 1
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue  # process mort entre iterdir() et read() : normal
            # `comm` est entre parenthèses et peut contenir espaces et
            # parenthèses ("(sd-pam)", "Web Content") : on coupe au DERNIER ')'.
            open_paren, close_paren = stat.find("("), stat.rfind(")")
            if open_paren < 0 or close_paren < open_paren:
                continue
            comm = stat[open_paren + 1 : close_paren]
            rest = stat[close_paren + 2 :].split()
            if len(rest) < 20:
                continue
            try:
                pid = int(entry.name)
                jiffies = int(rest[11]) + int(rest[12])  # utime + stime
                starttime = int(rest[19])  # discrimine une réutilisation de PID
            except ValueError:
                continue
            current[pid] = (jiffies, starttime)
            names[pid] = comm

        top_pct, top_name = None, None
        if self._prev_time is not None and (dt := now - self._prev_time) > 0:
            top_pct = 0.0
            for pid, (jiffies, starttime) in current.items():
                previous = self._prev_cpu.get(pid)
                if previous is None or previous[1] != starttime:
                    continue
                pct = (jiffies - previous[0]) / CLK_TCK / dt * 100.0
                if pct > top_pct:
                    top_pct, top_name = pct, names.get(pid)

        self._prev_cpu = current
        self._prev_time = now
        return count, top_pct, top_name

    def read(self) -> dict[str, float | str | None]:
        now = time.monotonic()
        n_procs, top_pct, top_name = self._scan_procs(now)

        window = self._active_window()
        is_gaming: float | None = None
        if window is not None:
            haystack = window.lower()
            is_gaming = float(any(hint in haystack for hint in GAMING_HINTS))

        busy = registry.read_int(self._gpu_busy) if self._gpu_busy else None
        is_compositing: float | None = None
        if busy is not None:
            is_compositing = float(busy > 0 and is_gaming != 1.0)

        uptime = registry.read_text(self.proc_root / "uptime")
        try:
            uptime_s = float(uptime.split()[0]) if uptime else None
        except (IndexError, ValueError):
            uptime_s = None

        return {
            "uptime_s": uptime_s,
            "top_proc_cpu": top_pct,
            "top_proc_name": top_name,
            "n_procs": float(n_procs),
            "is_gaming": is_gaming,
            "is_compositing": is_compositing,
            "active_window": window,
        }
