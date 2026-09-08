"""Catalogue des erreurs matérielles et noyau détectables sous Linux.

Ce fichier est volontairement **des données, pas du code** : une règle = un
motif, un genre, une gravité, et une phrase disant ce que ça veut dire. Tu peux
en ajouter une sans toucher au moteur, et surtout tu peux relire la liste pour
juger de ce qui est couvert.

« Toutes les erreurs possibles » n'existe pas comme liste finie : un pilote peut
inventer demain un message qu'aucun motif ne connaît. La couverture repose donc
sur DEUX étages complémentaires :

1. les règles ci-dessous, qui *classent* une erreur en genre exploitable
   (`mce`, `gpu_reset`, `ecc_ue`…) — c'est ce qui rend le dataset labellisable ;
2. un attrape-tout : **tout** enregistrement que le noyau lui-même marque en
   priorité ≤ 3 (err, crit, alert, emerg) et qu'aucune règle ne reconnaît est
   enregistré en `kernel_error` avec son texte brut.

L'étage 2 garantit qu'on ne rate rien de ce que le noyau considère comme une
erreur ; l'étage 1 détermine ce qu'on saura en faire. Les compteurs de
`readers/errcounters.py` forment un troisième étage, indépendant du texte : ils
attrapent les erreurs que le noyau comptabilise sans forcément les journaliser.

Priorités syslog : 0 emerg, 1 alert, 2 crit, 3 err, 4 warning, 5 notice,
6 info, 7 debug.
"""

from __future__ import annotations

import re
from typing import NamedTuple

CRITICAL = "critical"
ERROR = "error"
WARNING = "warning"

#: Priorité syslog à partir de laquelle le noyau parle d'une erreur.
KERNEL_ERROR_LEVEL = 3
#: Genre attribué à une erreur noyau qu'aucune règle ne reconnaît.
CATCH_ALL_KIND = "kernel_error"


class Rule(NamedTuple):
    kind: str
    severity: str
    pattern: re.Pattern[str]
    note: str


def _rule(kind: str, severity: str, pattern: str, note: str) -> Rule:
    return Rule(kind, severity, re.compile(pattern, re.IGNORECASE), note)


# --------------------------------------------------------------------------
# Famille 1 — CPU et mémoire : les erreurs qui disent « le calcul était faux »
# --------------------------------------------------------------------------
CPU_RULES = (
    _rule(
        "mce_uncorrected", CRITICAL,
        r"machine check exception|mce:.*uncorrect|hardware error.*uncorrect"
        r"|processor context corrupt",
        "Erreur machine non corrigée : un calcul a été faux. Cause n°1 d'un OC "
        "instable, juste avant le gel.",
    ),
    _rule(
        "mce", ERROR,
        r"\bmce:\s*\[hardware error\]|machine check events logged"
        r"|\bmce:\s*\[hardware error\].*corrected",
        "Erreur machine corrigée : le matériel s'est rattrapé. Isolée elle est "
        "bénigne, en série elle annonce la panne.",
    ),
    _rule(
        "whea", CRITICAL,
        r"hardware error from apei|\bghes\b.*error|apei.*hardware error"
        r"|hardware error device",
        "APEI/GHES : l'équivalent Linux du WHEA de Windows. Le firmware signale "
        "une erreur matérielle que le CPU n'a pas gérée seul.",
    ),
    _rule(
        "ecc_ue", CRITICAL,
        r"edac.*\bue\b|uncorrected error.*memory|ue memory read error"
        r"|uncorrectable.*(dimm|memory)",
        "Erreur mémoire NON corrigée : donnée perdue. Barrette à remplacer.",
    ),
    _rule(
        "ecc_ce", WARNING,
        r"edac.*\bce\b|corrected error.*memory|ce memory read error"
        r"|corrected.*(dimm|memory)",
        "Erreur mémoire corrigée par l'ECC. Sans ECC (cas courant en desktop) "
        "cette erreur-là serait passée inaperçue — et aurait corrompu la donnée.",
    ),
    _rule(
        # `microcode:` avec deux-points = le chargeur du noyau. Sans cette
        # précision, « iwlwifi: Microcode SW error detected » — un plantage de
        # firmware Wi-Fi — serait classé comme un problème de CPU.
        "microcode", WARNING,
        r"\bmicrocode: .*(fail|error|mismatch)|microcode update.*fail",
        "Chargement de microcode raté : le CPU tourne avec des errata connus.",
    ),
)

# --------------------------------------------------------------------------
# Famille 2 — Stabilité du noyau : ce que produit un OC ou un undervolt trop bas
# --------------------------------------------------------------------------
STABILITY_RULES = (
    _rule(
        "panic", CRITICAL,
        r"kernel panic|fatal exception|system halted",
        "Panique noyau. Sur une machine overclockée, c'est le verdict.",
    ),
    _rule(
        "oops", CRITICAL,
        r"\boops:|kernel bug at|bug: unable to handle|invalid opcode:"
        r"|unable to handle kernel (paging|null)",
        "Oops/BUG noyau : bug logiciel, ou instruction corrompue par une "
        "instabilité électrique. Le contexte tranche.",
    ),
    _rule(
        "gpf", ERROR,
        r"general protection fault|stack segment:|double fault",
        "Faute de protection générale : souvent le premier symptôme visible "
        "d'un undervolt CPU hors marge.",
    ),
    _rule(
        "hard_lockup", CRITICAL,
        r"watchdog.*hard lockup|nmi watchdog.*cpu\s*#?\d+ (hard )?locked up",
        "Verrou matériel : un cœur ne répond plus aux interruptions.",
    ),
    _rule(
        "soft_lockup", ERROR,
        r"watchdog.*bug: soft lockup|soft lockup - cpu#\d+ stuck",
        "Verrou logiciel : un cœur monopolisé sans céder la main.",
    ),
    _rule(
        "rcu_stall", ERROR,
        r"rcu.*(self-detected stall|detected stalls|stall ended)",
        "Blocage RCU : un cœur n'a pas progressé pendant des secondes. "
        "Classique juste avant un gel complet.",
    ),
    _rule(
        "hung_task", ERROR,
        r"task .*blocked for more than \d+ seconds|hung_task",
        "Tâche bloquée > 120 s. Pointe souvent vers un stockage en perdition.",
    ),
    _rule(
        "nmi", ERROR,
        r"uhhuh\. nmi received|nmi: (pci system|iock) error|dazed and confused",
        "NMI matérielle non attendue : alimentation, RAM ou bus.",
    ),
    _rule(
        "segfault", WARNING,
        r"segfault at |traps: .*trap (int3|divide|invalid)",
        "Segfault utilisateur. Isolé c'est un bug applicatif ; en rafale sur "
        "des binaires variés, c'est le matériel qu'il faut suspecter.",
    ),
)

# --------------------------------------------------------------------------
# Famille 3 — Mémoire et pression
# --------------------------------------------------------------------------
MEMORY_RULES = (
    _rule(
        "oom", ERROR,
        r"out of memory: kill|oom-kill:|invoked oom-killer|oom_reaper",
        "Le noyau a tué un process faute de mémoire.",
    ),
    _rule(
        "alloc_failure", WARNING,
        r"page allocation (failure|stalls)|slab out of memory",
        "Allocation refusée : fragmentation ou pression mémoire durable.",
    ),
)

# --------------------------------------------------------------------------
# Famille 4 — GPU. Les deux marques ne parlent pas la même langue.
# --------------------------------------------------------------------------
GPU_RULES = (
    _rule(
        "gpu_xid", CRITICAL,
        r"nvrm:\s*xid|nvrm: gpu at pci|nvrm:.*rmini",
        "Xid NVIDIA : le code dit la cause exacte (13 = erreur graphique, "
        "31 = accès mémoire illégal, 79 = GPU tombé du bus, 62/63/64 = ECC).",
    ),
    _rule(
        "gpu_reset", CRITICAL,
        r"gpu reset|amdgpu.*(reset|recover)|drm.*resetting|gpu recovery"
        r"|failed to reset|gpu hang",
        "Reset GPU : le pilote a dû réinitialiser la carte. Sur un GPU "
        "overclocké, c'est l'équivalent de la MCE côté CPU.",
    ),
    _rule(
        "gpu_fault", ERROR,
        r"gpu fault detected|vm_l2_protection_fault|page fault \(src_id"
        r"|\[gfxhub\]|\[mmhub\]|ring .*timeout|fence.*timeout|job timed out",
        "Faute mémoire ou timeout de ring GPU : calcul GPU perdu.",
    ),
    _rule(
        "gpu_ecc", CRITICAL,
        r"ras.*(ue|uncorrectable)|gpu ecc error|sram ecc|xgmi.*error",
        "ECC GPU : erreur mémoire vidéo non corrigée.",
    ),
    _rule(
        "gpu_thermal", ERROR,
        r"gpu.*(over ?temp|thermal.*(trip|shutdown|throttl))"
        r"|amdgpu.*temperature.*(high|critical)",
        "Le GPU a atteint sa limite thermique matérielle.",
    ),
    _rule(
        "drm_error", WARNING,
        r"\[drm:[^\]]+\] \*error\*|drm_kms_helper.*error|atomic update failed",
        "Erreur DRM générique : affichage, modeset, ou pilote en difficulté.",
    ),
)

# --------------------------------------------------------------------------
# Famille 5 — Stockage. Un SSD qui throttle fait grimper la pression I/O sans
# qu'aucune sonde CPU ne bouge : la corrélation vaut la peine d'être capturée.
# --------------------------------------------------------------------------
STORAGE_RULES = (
    _rule(
        "nvme_error", ERROR,
        r"nvme\d*[a-z]*\d*: .*(i/o|timeout|abort|reset|controller is down"
        r"|failed to set apst|not ready|disabling device)",
        "Erreur NVMe : timeout, reset de contrôleur ou I/O perdue.",
    ),
    _rule(
        "ata_error", ERROR,
        r"ata\d+(\.\d+)?: (exception|failed command|serror|hard resetting)"
        r"|link is slow to respond",
        "Erreur ATA/SATA : câble, alimentation ou disque en fin de vie.",
    ),
    _rule(
        "block_error", ERROR,
        r"blk_update_request|buffer i/o error|critical (medium|target|nexus) error"
        r"|i/o error, dev \w+",
        "I/O bloc en échec : secteur illisible ou périphérique disparu.",
    ),
    _rule(
        "fs_error", CRITICAL,
        r"ext4-fs error|btrfs.*(error|checksum)|xfs.*(internal error|corruption)"
        r"|remounting filesystem read-only|f2fs.*error",
        "Erreur de système de fichiers : corruption sur disque. "
        "Souvent la trace laissée par un gel dû à l'OC.",
    ),
    _rule(
        "smart_warning", WARNING,
        r"smart.*(fail|threshold|warning)|critical warning.*0x",
        "SMART/NVME critical warning : le disque s'annonce lui-même défaillant.",
    ),
)

# --------------------------------------------------------------------------
# Famille 6 — Bus, alimentation, firmware
# --------------------------------------------------------------------------
BUS_RULES = (
    _rule(
        "pcie_fatal", CRITICAL,
        r"aer:.*(fatal|uncorrected)|pcie bus error.*(fatal|uncorrected)",
        "Erreur PCIe non corrigée : lien instable. Sur une carte graphique, "
        "c'est souvent un riser, une alim faible, ou un BCLK poussé trop loin.",
    ),
    _rule(
        "pcie_corrected", WARNING,
        r"aer:.*corrected|pcie bus error.*corrected|badtlp|baddllp|rxerr",
        "Erreur PCIe corrigée par retransmission. Le lien tient, mais il souffre.",
    ),
    _rule(
        "pcie_link", ERROR,
        r"pcieport.*link (down|is down|retrain)|card not present|link training"
        r"|downgraded.*link|bandwidth.*limited",
        "Lien PCIe descendu ou dégradé : un périphérique a disparu du bus.",
    ),
    _rule(
        "usb_error", WARNING,
        r"usb.*(device descriptor read.*error|unable to enumerate|cannot enable"
        r"|over-?current)",
        "Erreur USB, souvent d'alimentation. Une surintensité peut faire "
        "chuter une ligne partagée avec autre chose.",
    ),
    _rule(
        "thermal_trip", CRITICAL,
        r"core temperature above threshold|package temperature above threshold"
        r"|critical temperature reached|thermal.*(shutdown|trip point)"
        r"|temperature above threshold, cpu clock throttled",
        "Seuil thermique matériel franchi : le CPU s'est bridé lui-même. "
        "LE signal recherché pour 'ventirad encrassé' et 'flux d'air obstrué'.",
    ),
    _rule(
        "power_error", ERROR,
        r"undervoltage detected|power supply.*(fail|error)|battery.*(critical|fail)"
        r"|acpi.*power.*error",
        "Anomalie d'alimentation signalée par le firmware.",
    ),
    _rule(
        "acpi_error", WARNING,
        r"acpi (bios )?error|acpi.*(aml error|_osc failed|namespace lookup failure"
        r"|exception|ignoring invalid)",
        "Erreur ACPI : firmware de la carte mère. Bruyant sur beaucoup de "
        "cartes, à filtrer si ça pollue.",
    ),
    _rule(
        "firmware_error", WARNING,
        r"direct firmware load.*fail|firmware: failed to load|failed to load firmware"
        r"|microcode sw error|firmware crash",
        "Firmware d'un périphérique non chargé ou planté (Wi-Fi, GPU, contrôleur).",
    ),
    _rule(
        "suspend_error", ERROR,
        r"pm: suspend.*(fail|abort)|freezing of tasks failed|resume.*fail"
        r"|acpi.*(sleep|wakeup).*error",
        "Veille ou reprise ratée. Casse aussi les compteurs RAPL au réveil.",
    ),
    _rule(
        "netdev_error", WARNING,
        r"netdev watchdog.*transmit.*timed out|nic link is down|tx hang",
        "Carte réseau bloquée : le pilote a dû la réinitialiser.",
    ),
)

#: Ordre = priorité de correspondance. Le premier motif qui accroche gagne, donc
#: le spécifique (mce_uncorrected) doit précéder le générique (mce).
RULES: tuple[Rule, ...] = (
    CPU_RULES + STABILITY_RULES + MEMORY_RULES + GPU_RULES + STORAGE_RULES + BUS_RULES
)

#: Codes Xid NVIDIA les plus parlants. Le reste est enregistré en brut.
XID_MEANINGS = {
    13: "erreur d'exécution graphique",
    31: "accès mémoire GPU illégal",
    43: "arrêt du canal par le pilote",
    48: "erreur double bit (ECC)",
    61: "microcontrôleur interne bloqué",
    62: "erreur double bit non contenue",
    63: "page ECC retirée",
    64: "échec de retrait de page ECC",
    69: "erreur moteur graphique",
    79: "GPU tombé du bus (alimentation ou PCIe)",
    93: "erreur non fatale du moteur",
    109: "erreur de contexte du canal",
}
_XID_RE = re.compile(r"xid\s*\(?[^)]*\)?\s*:?\s*(\d+)", re.IGNORECASE)


#: Messages que le noyau émet en priorité « erreur » alors qu'ils décrivent un
#: état normal — absence d'une fonctionnalité, sonde qui répond « non supporté »,
#: initialisation d'un collecteur d'erreurs. Sans cette liste, l'attrape-tout
#: enregistrerait à chaque boot les mêmes non-événements, et une constante
#: présente dans 100 % des lignes n'apprend rien à un modèle.
#:
#: À compléter machine par machine : `python -m collector.main --scan` montre ce
#: qui a été classé sur le boot courant, ce qui rend l'ajustement immédiat.
BENIGN = re.compile(
    r"tdx: tdx not supported"
    r"|sgx: there are zero epc sections"
    r"|sgx disabled by bios"
    r"|acpi:.*(successfully|acquired and loaded)"
    r"|ras: correctable errors collector initialized"
    r"|edac (mc|amd64):.*(ver:|not present|probe|drivers are available)"
    r"|thermal_sys: failed to find"
    r"|failed to get (bios|ec) "
    r"|no (irq |)handler for vector"
    r"|reservation of .* failed"
    r"|error parsing pcc subspaces"
    r"|check_flush_dependency",
    re.IGNORECASE,
)


def classify(text: str, level: int | None = None) -> Rule | None:
    """Range un message noyau dans un genre, ou None s'il n'a rien d'une erreur.

    `level` est la priorité syslog. Un message que le noyau marque en err ou
    pire mais qu'aucune règle ne reconnaît ressort en `kernel_error` : c'est ce
    qui évite de ne détecter que les pannes qu'on avait déjà imaginées.
    """
    if BENIGN.search(text):
        return None
    for rule in RULES:
        if rule.pattern.search(text):
            return rule
    if level is not None and level <= KERNEL_ERROR_LEVEL:
        severity = CRITICAL if level <= 2 else ERROR
        return Rule(CATCH_ALL_KIND, severity, _XID_RE, "Erreur noyau non classée.")
    return None


def describe_xid(text: str) -> str | None:
    """Traduit un Xid NVIDIA en cause probable, quand le code est connu."""
    match = _XID_RE.search(text)
    if not match:
        return None
    code = int(match.group(1))
    return f"Xid {code}: {XID_MEANINGS.get(code, 'code non répertorié')}"


def catalogue() -> list[tuple[str, str, str]]:
    """(genre, gravité, explication) — utilisé par `--doctor`."""
    seen: dict[str, tuple[str, str, str]] = {}
    for rule in RULES:
        seen.setdefault(rule.kind, (rule.kind, rule.severity, rule.note))
    return list(seen.values())
