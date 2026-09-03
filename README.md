<div align="center">

# 🩺 BAD — Bdex Anomaly Detector

**Hardware telemetry at 1 Hz on Linux, to build a labelled dataset and train an
anomaly detector on it.**

<br>

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&logoColor=black)
![Storage](https://img.shields.io/badge/storage-SQLite%20(WAL)-003B57?logo=sqlite&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-0%20required-success)

</div>

---

Targets: clogged heatsink, fan curve set too low, blocked airflow, unstable
overclock.

This repository contains the **collection stage**. Without clean, contextualised
data, the model stage has nothing to learn from — so that is the one we take care
of first.

<details>
<summary><b>📑 Contents</b></summary>

- [Principle](#principle)
- [Quick start](#-quick-start)
  - [Annotating experiments](#annotating-experiments)
  - [Tests](#tests)
- [Layout](#-layout)
- [Data model](#-data-model)
- [What each reader collects](#-what-each-reader-collects)
- [Context](#-context-or-why-this-is-not-a-kaggle-notebook)
- [Experimental protocols](#-experimental-protocols)
- [Error detection](#-error-detection)
- [Known limitations](#-known-limitations)
- [Dependencies](#-dependencies)

</details>

## Principle

A *reader* = one source of metrics. It declares its columns, configures itself at
startup, returns one dict per tick, and **never raises inside `read()`**. The
collector has to survive losing a sensor.

Three rules structure everything else:

> **1. Nothing is hard-coded.**
> hwmon paths are resolved by the contents of the `name` file, never by number
> (`hwmon2` is `k10temp` today and `hwmon4` after the next reboot). The number of
> CPU threads, the RAPL zones and the temperature probes are discovered at
> startup.

> **2. A missing source disables itself, it does not fill the database with NULLs.**
> `setup()` returns `False` and the reader disappears from the schema. The same
> repository runs on a 7600X + RTX 3060 Ti desktop and on a Ryzen laptop with an
> iGPU.

> **3. A label is never guessed.**
> When the information is missing, the column is `NULL`, not `0` — one wrong
> label pollutes the whole downstream dataset.

## 🚀 Quick start

```bash
# What THIS machine makes it possible to measure and detect (run it on each one).
python -m collector.main --doctor

# Classify every error of the current boot, without writing anything to the database.
python -m collector.main --scan

# Collection (without root: everything except RAPL).
python -m collector.main --db data/metrics.db --period 1.0

# With RAPL (energy_uj is 0400).
sudo python -m collector.main --db data/metrics.db --period 1.0

# Short run.
python -m collector.main --db /tmp/trial.db --period 0.5 --duration 30 -v
```

> 🛠️ **As a service**: see [`systemd/bad-collector.service`](systemd/bad-collector.service).

### Annotating experiments

This is the project's added value: the ground truth no sensor gives you.

```bash
python -m collector.label start airflow_blocked -n "front intake blocked"
# ... the experiment runs ...
python -m collector.label end
python -m collector.label list
```

### Tests

```bash
python -m unittest discover -s tests -t .
```

The tests mount fake `/sys` and `/proc` trees: we check the RAPL wrap without
waiting 4 seconds of load, and the `gigabyte_wmi` discovery without a Gigabyte
board.

## 🗂️ Layout

```
collector/
├─ main.py            fixed-rate loop, reader lifecycle
├─ storage.py         SQLite (evolving schema, batching, deduplicated events, labels)
├─ registry.py        sysfs resolution (hwmon by name, tolerant reads)
├─ errors.py          CATALOGUE of detectable errors (data, not code)
├─ label.py           experiment annotation CLI
└─ readers/
   ├─ base.py         Reader contract + ReaderState (failures, event channel)
   ├─ kmsg.py         kernel errors (/dev/kmsg or journalctl)
   ├─ errcounters.py  hardware error counters (no root needed)
   ├─ hwmon.py        shared hwmon discovery base
   ├─ k10temp.py      AMD CPU temperatures
   ├─ gigabyte_wmi.py 6 Gigabyte motherboard probes
   ├─ amdgpu.py       AMD GPU/iGPU
   ├─ nvme.py         SSD temperatures
   ├─ nvml.py         NVIDIA GPU (NVML, not nvidia-smi)
   ├─ rapl.py         CPU power derived from energy counters
   ├─ psi.py          Pressure Stall Information
   ├─ proc.py         CPU/memory/frequency from /proc
   └─ context.py      what the machine was doing
schema.sql            invariant skeleton of the database
```

## 🗃️ Data model

**Wide** format: ~50 `REAL` columns, one row per second. 14 days at 1 Hz is
~1.2 M rows; in narrow form (`ts, metric, value`) that would be 60 M for the same
information. Columns are added dynamically (`ALTER TABLE ADD COLUMN` is O(1) in
SQLite), so adding a reader does not break an existing database: older rows
simply carry `NULL`.

| Table       | Role                                                                      |
|-------------|---------------------------------------------------------------------------|
| `samples`   | the series, one row per tick, indexed by `(boot_id, ts)`                  |
| `boots`     | a boot = a context (renumbered paths, RAPL counters reset)                |
| `events`    | discrete events: WHEA, MCE, OOM, GPU reset, reader failure                |
| `labels`    | hand-annotated experiment windows                                         |

> 💡 **SQLite and not PostgreSQL** — a single writer, local, no network, no
> concurrency. In WAL mode, SQLite handles 1 Hz without breaking a sweat and the
> single file is trivially copied and versioned. Postgres here would be
> gratuitous complexity.

## 📊 What each reader collects

| Reader          | Columns                                                            | Notes                                                    |
|-----------------|--------------------------------------------------------------------|----------------------------------------------------------|
| `kmsg`          | `kmsg_errors`, `kmsg_criticals`, `kmsg_messages`                  | classified kernel errors, written to `events`            |
| `errcounters`   | `err_mce`, `err_thermal_irq`, `err_aer_*`, `err_ecc_*`…           | hardware counters, no privileges needed                  |
| `k10temp`       | `k10temp_tctl`, `k10temp_tccd1`…                                  | named after `tempN_label`, not after `N`                 |
| `gigabyte_wmi`  | `gigabyte_temp1..6`                                               | unlabelled probes, see the protocol below                |
| `rapl`          | `rapl_pkg_watts`, `rapl_core_watts`                               | derived power, root required                             |
| `nvml`          | `gpu_temp`, `gpu_power_w`, `gpu_throttle_mask`…                   | NVIDIA GPU                                                |
| `amdgpu`        | `amdgpu_edge`, `amdgpu_sclk_mhz`, `amdgpu_busy_pct`…              | AMD GPU/iGPU                                              |
| `nvme`          | `nvme_composite`…                                                 | one reader per disk                                      |
| `psi`           | `psi_cpu_some`, `psi_io_full`…                                    | contention, not utilisation                              |
| `proc`          | `cpu_util`, `cpuN_util`, `load1`, `mem_used_mb`, `cpu_freq_avg`   | derived from jiffies                                     |
| `context`       | `uptime_s`, `top_proc_cpu`, `is_gaming`, `active_window`…         | usage context                                            |

Three of them deserve a word.

<details>
<summary><b>⚡ RAPL — an energy counter, not a power sensor</b></summary>

<br>

**RAPL** is not a power sensor but a cumulative energy counter in µJ. We derive
`P = ΔE / Δt`, and the counter **wraps** at `max_energy_range_uj` (~262 J, i.e.
~4 s at 65 W: it happens constantly). Without handling the wrap, you get negative
power readings every few seconds — and the anomaly detector spends its life
detecting that bug. The symmetric case (counter reset when waking from sleep,
while `CLOCK_MONOTONIC` has not advanced) produces an absurd spike: it is bounded
and replaced by `NULL`. A hole in the data beats an invented spike.

</details>

<details>
<summary><b>📉 PSI — contention, not utilisation</b></summary>

<br>

**PSI** measures **contention**, not utilisation. A CPU at 100 % can have zero
PSI (nobody is waiting); a CPU at 40 % with a PSI of 30 means tasks are blocked.
That is exactly the signal that appears when a system drifts while the classic
metrics stay green.

</details>

<details>
<summary><b>🎮 NVML rather than <code>nvidia-smi</code></b></summary>

<br>

`nvidia-smi` forks a process and takes ~200 ms. At 1 Hz, we would spend 20 % of
the CPU measuring the CPU. NVML is a library call, ~50 µs. `gpu_throttle_mask` is
the most valuable metric there: the hardware itself says *why* it is limiting
itself (thermal, power cap, voltage reliability). That is a free label, stored as
a raw bitmask and decomposed later at feature engineering time.

</details>

## 🧭 Le contexte, ou pourquoi ce n'est pas un notebook Kaggle

Sans contexte, le modèle apprend « GPU chaud = anomalie » et alerte dès qu'on
lance un jeu. Avec contexte, il apprend « GPU chaud **alors que rien ne tourne**
= anomalie ».

`is_gaming` est une décision de design, pas une évidence. Elle est ici
**tri-valuée** :

| Valeur  | Signification                                                                    |
|:-------:|---------------------------------------------------------------------------------|
| `1`     | la fenêtre active porte un indice connu (`steam_app`, `gamescope`, `lutris`, `wine`…) |
| `0`     | la fenêtre active est connue et ne correspond à aucun indice                      |
| `NULL`  | aucune information (daemon lancé hors session graphique, compositeur injoignable) |

Écrire `0` dans le dernier cas reviendrait à affirmer « pas de jeu » alors qu'on
ne sait rien. Pour la même raison, `active_window` et `top_proc_name` sont
stockés **en TEXT brut** : on ne peut pas ré-étiqueter des données qu'on n'a pas
collectées, alors que l'heuristique, elle, se recalcule à volonté.

Le compositeur est interrogé par socket IPC Hyprland (pas `hyprctl`, qui forke)
et au plus toutes les 5 secondes.

## 🧪 Protocoles expérimentaux

### Identifier les 6 sondes `gigabyte_wmi`

Le driver ne les labellise pas. Elles restent donc nommées par index, et le
mapping se détermine à l'expérience — le renommage se fera après coup par
`ALTER TABLE RENAME COLUMN`, ce n'est pas une raison pour deviner maintenant.

1. `stress-ng --cpu $(nproc) --timeout 5m` → la sonde qui monte le plus vite et
   redescend le plus vite est côté VRM ;
2. charge GPU seule → la sonde qui suit est côté PCIe/chipset ;
3. machine au repos, fenêtre ouverte → la sonde qui suit l'ambiante.

> 📝 À consigner ici quand ce sera fait.

### Familles d'anomalies visées

| Label             | Provocation                                          |
|-------------------|------------------------------------------------------|
| `fan_curve_low`   | courbe de ventilation abaissée dans le BIOS          |
| `airflow_blocked` | grille d'entrée d'air obstruée                       |
| `oc_unstable`     | OC/undervolt hors marge → WHEA dans `events`         |
| `stress_ng`       | charge synthétique de référence                      |
| `gaming`          | charge réelle                                        |
| `normal`          | tout le reste                                        |

## 🚨 Détection d'erreurs

C'est la table `events` : un événement ponctuel — une MCE dure une microseconde —
ne peut pas exister dans une série échantillonnée à 1 Hz. Il lui faut un canal
à part.

« Toutes les erreurs possibles » n'existe pas comme liste finie : un pilote peut
inventer demain un message qu'aucun motif ne connaît. La couverture repose donc
sur **trois étages** dont chacun rattrape les angles morts des autres.

### Étage 1 — Classement des messages noyau

38 règles dans `collector/errors.py`, rangées en six familles : CPU/mémoire
(MCE, APEI/GHES, ECC, microcode), stabilité (panic, oops, lockups, RCU stall,
GPF), mémoire (OOM), GPU (Xid NVIDIA avec traduction du code, reset et fautes
AMD, ECC vidéo), stockage (NVMe, ATA, bloc, corruption de FS) et bus
(PCIe AER, lien, USB, seuil thermique, alimentation, ACPI, firmware, veille).

Le fichier est **des données, pas du code** : un motif, un genre, une gravité et
une phrase d'explication. Ajouter une règle ne demande pas de toucher au moteur.

Deux sources, choisies au démarrage :

| Source            | Quand                        | Clé de déduplication  |
|-------------------|------------------------------|-----------------------|
| `/dev/kmsg`       | root (défaut sous systemd)   | numéro de séquence    |
| `journalctl -k -f`| sans privilèges, si systemd  | curseur journald      |

Aucune des deux ne forke par tick : `/dev/kmsg` est un descripteur ouvert une
fois, `journalctl` un seul process pour toute la vie du daemon.

> 🔁 **Le démarrage rejoue tout le tampon du boot courant.** Les erreurs
> survenues avant le lancement du collector — au boot, ou pendant que le service
> était arrêté — sont récupérées avec leur date d'origine. La clé de
> déduplication rend ce rejeu idempotent : redémarrer trois fois n'enregistre pas
> trois fois la même MCE.

### Étage 2 — L'attrape-tout

Tout enregistrement que le noyau marque lui-même en priorité ≤ 3 (err, crit,
alert, emerg) et qu'aucune règle ne reconnaît est enregistré en `kernel_error`
avec son texte brut. C'est ce qui évite de ne détecter que les pannes qu'on
avait déjà imaginées.

Contrepartie : le noyau émet en priorité « erreur » des messages qui décrivent
un état normal (`TDX not supported`, `RAS: Correctable Errors collector
initialized`…). Une liste `BENIGN` explicite les écarte — sans elle, chaque boot
rajouterait les mêmes non-événements, et une constante présente dans 100 % des
lignes n'apprend rien à un modèle. Elle se complète machine par machine, et
`--scan` sert exactement à ça.

### Étage 3 — Les compteurs matériels

Indépendants du texte, et c'est ce qui fait leur valeur :

- ils fonctionnent **sans privilèges**, là où `/dev/kmsg` exige root ;
- ils attrapent ce que le noyau compte **sans forcément le journaliser** — une
  erreur PCIe corrigée n'écrit rien dans `dmesg` si le rate-limit a frappé, mais
  le compteur, lui, avance.

| Colonne                                  | Source                    | Ce que ça détecte                          |
|------------------------------------------|---------------------------|--------------------------------------------|
| `err_mce`                                | `/proc/interrupts` MCE    | Machine Check comptée par le CPU           |
| `err_thermal_irq`                        | `/proc/interrupts` TRM    | seuil thermique franchi ← ventirad encrassé |
| `err_threshold_irq`, `err_deferred_irq`  | THR, DFR                  | seuils et erreurs différées AMD            |
| `err_nmi`                                | NMI                       | alimentation, RAM, watchdog                |
| `err_oom_kill`                           | `/proc/vmstat`            | process tués faute de mémoire              |
| `err_ecc_ce`, `err_ecc_ue`               | EDAC                      | erreurs mémoire corrigées / non corrigées  |
| `err_aer_corr/nonfatal/fatal`            | PCIe AER                  | qualité du lien PCIe                        |
| `err_cpu_throttle`, `err_pkg_throttle`   | `thermal_throttle`        | bridages thermiques (Intel)                |
| `err_gpu_ras`                            | RAS amdgpu                | ECC mémoire vidéo                          |
| `err_disk_io`                            | `ioerr_cnt`               | erreurs d'I/O SCSI                         |
| `err_nvme_state`                         | `/sys/class/nvme/*/state` | contrôleur hors de l'état `live`           |

Chaque compteur est **à la fois un événement et une feature** : son delta par
tick devient une colonne de `samples`, et tout mouvement écrit une ligne dans
`events`. Une valeur non nulle **au démarrage** est signalée aussi : une machine
qui compte déjà 4 000 erreurs ECC corrigées depuis le boot doit le dire au
premier tick, pas attendre la 4 001e.

### Couverture, machine par machine

Elle n'est pas la même partout, et `--doctor` le dit — y compris ce qui n'est
**pas** surveillé, ce qui vaut autant que le reste : sans contrôleur EDAC, une
erreur mémoire corrigée ne laisse de trace nulle part.

Relevé sur le portable (Ryzen 5 3500U, sans root) : 9 compteurs actifs ;
`err_ecc_*` absents (mémoire sans ECC), `err_cpu_throttle` absent (compteur
Intel), `err_gpu_ras` absent (iGPU), `err_disk_io` absent (NVMe, pas SCSI).
Sur le desktop, attends-toi à voir apparaître les compteurs RAS du GPU et,
selon la carte mère, EDAC.

## ⚠️ Limites connues

Les dire vaut mieux que faire semblant.

- **Fréquence CPU.** `cpuinfo_avg_freq` (amd-pstate, noyaux récents) est une
  mesure ; `scaling_cur_freq`, utilisé en repli, n'est qu'une consigne du
  gouverneur et ment franchement sur AMD P-State. La vraie fréquence effective
  demanderait de lire les MSR APERF/MPERF — hors périmètre.
- **PSI.** On stocke `avg10`, déjà lissé par le noyau. Le compteur `total`
  (cumulé, en µs) serait dérivable et plus précis ; il reste récupérable plus
  tard si l'horizon de 14 jours le demande.
- **`top_proc_cpu`** est un pourcentage d'**un** cœur : 400 % est possible sur un
  process multithread. Le scan de `/proc` coûte quelques millisecondes par tick
  sur ~500 process.
- **Timestamps en `epoch ms`, clé primaire.** Deux ticks dans la même
  milliseconde se remplacent. À 1 Hz c'est théorique ; avec `--period 0.001` ça
  ne le serait plus.
- **`is_gaming`** repose sur une liste d'indices volontairement courte. Chaque
  entrée est une hypothèse sur le dataset ; c'est pour ça que le brut est
  conservé à côté.
- **iGPU.** `amdgpu_busy_pct` d'une iGPU mesure une puce qui partage son budget
  thermique et sa mémoire avec le CPU : les corrélations n'y ont pas le même
  sens que sur une carte dédiée.
- **Horodatage des erreurs kmsg.** Les enregistrements `/dev/kmsg` sont datés en
  µs depuis le boot sur une horloge qui ne compte pas le temps suspendu : après
  une veille, un événement d'avant peut être daté de quelques minutes en avance.
  Le backend journald, lui, donne un horodatage absolu exact.
- **Erreurs pendant que le collector est arrêté.** Le rejeu couvre le tampon du
  **boot courant** uniquement. Une erreur survenue lors d'un boot précédent
  n'est pas récupérée — `journalctl -k -b -1` reste à faire à la main.
- **La liste `BENIGN` est empirique.** Elle a été construite sur les messages
  réellement observés ; un pilote présent seulement sur l'autre machine peut
  introduire un nouveau faux positif. C'est à ça que sert `--scan` avant de
  lancer une collecte de 14 jours.

## 📦 Dépendances

Aucune obligatoire : `/proc`, `/sys` et la stdlib (`sqlite3` inclus) suffisent.
`nvidia-ml-py` est optionnel et n'intéresse que les machines NVIDIA — sans lui,
`NvmlReader` se désactive proprement au démarrage.
