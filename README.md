# BAD — Bdex Anomaly Detector

Collecte de télémétrie matérielle à 1 Hz sur Linux, en vue de construire un
dataset labellisé et d'y entraîner un détecteur d'anomalies (ventirad encrassé,
courbe de ventilation trop basse, flux d'air obstrué, OC instable).

Ce dépôt contient l'**étage de collecte**. Sans données propres et
contextualisées, l'étage modèle n'a rien à apprendre — c'est donc celui-là qu'on
soigne en premier.

## Principe

Un *reader* = une source de métriques. Il déclare ses colonnes, se configure au
démarrage, renvoie un dict par tick, et **ne lève jamais d'exception dans
`read()`**. Le collector doit survivre à la perte d'un capteur.

Trois règles structurent tout le reste :

1. **Rien n'est codé en dur.** Les chemins hwmon sont résolus par le contenu du
   fichier `name`, jamais par le numéro (`hwmon2` est `k10temp` aujourd'hui et
   `hwmon4` après le prochain reboot). Le nombre de threads CPU, les zones RAPL
   et les sondes de température sont découverts au démarrage.
2. **Une source absente se désactive, elle ne remplit pas la base de NULL.**
   `setup()` renvoie `False` et le reader disparaît du schéma. Le même dépôt
   tourne sur un desktop 7600X + RTX 3060 Ti et sur un portable Ryzen à iGPU.
3. **On ne devine jamais un label.** Quand l'information manque, la colonne vaut
   `NULL`, pas `0` — un faux label pollue tout le dataset en aval.

## Utilisation

```bash
# Ce que CETTE machine permet de mesurer et de détecter (à lancer sur chacune).
python -m collector.main --doctor

# Classe toutes les erreurs du boot courant, sans rien écrire en base.
python -m collector.main --scan

# Collecte (sans root : tout sauf RAPL).
python -m collector.main --db data/metrics.db --period 1.0

# Avec RAPL (energy_uj est en 0400).
sudo python -m collector.main --db data/metrics.db --period 1.0

# Essai court.
python -m collector.main --db /tmp/essai.db --period 0.5 --duration 30 -v
```

En service : voir `systemd/bad-collector.service`.

### Annoter les expériences

C'est la valeur ajoutée du projet : la vérité terrain qu'aucun capteur ne donne.

```bash
python -m collector.label start airflow_blocked -n "grille avant obstruée"
# ... l'expérience tourne ...
python -m collector.label end
python -m collector.label list
```

### Tests

```bash
python -m unittest discover -s tests -t .
```

Les tests montent de faux `/sys` et `/proc` : on vérifie le wrap RAPL sans
attendre 4 secondes de charge, et la découverte `gigabyte_wmi` sans carte
Gigabyte.

## Organisation

```
collector/
  main.py          boucle à cadence fixe, cycle de vie des readers
  storage.py       SQLite (schéma évolutif, batch, events dédupliqués, labels)
  registry.py      résolution sysfs (hwmon par nom, lectures tolérantes)
  errors.py        CATALOGUE des erreurs détectables (données, pas code)
  label.py         CLI d'annotation des expériences
  readers/
    base.py        contrat Reader + ReaderState (pannes, canal d'événements)
    kmsg.py        erreurs noyau (/dev/kmsg ou journalctl)
    errcounters.py compteurs d'erreurs matérielles (sans root)
    hwmon.py       socle de découverte hwmon partagé
    k10temp.py     températures CPU AMD
    gigabyte_wmi.py  6 sondes carte mère Gigabyte
    amdgpu.py      GPU/iGPU AMD
    nvme.py        températures SSD
    nvml.py        GPU NVIDIA (NVML, pas nvidia-smi)
    rapl.py        puissance CPU dérivée des compteurs d'énergie
    psi.py         Pressure Stall Information
    proc.py        CPU/mémoire/fréquence depuis /proc
    context.py     ce que la machine était en train de faire
schema.sql         squelette invariant de la base
```

## Modèle de données

Format **wide** : ~50 colonnes `REAL`, une ligne par seconde. 14 jours à 1 Hz
font ~1,2 M lignes ; en narrow (`ts, metric, value`) ce serait 60 M pour la même
information. Les colonnes sont ajoutées dynamiquement (`ALTER TABLE ADD COLUMN`
est O(1) en SQLite), donc ajouter un reader ne casse pas une base existante :
les anciennes lignes portent simplement `NULL`.

| Table    | Rôle |
|----------|------|
| `samples` | les séries, une ligne par tick, indexées par `(boot_id, ts)` |
| `boots`   | un boot = un contexte (chemins renumérotés, compteurs RAPL remis à zéro) |
| `events`  | événements discrets : WHEA, MCE, OOM, reset GPU, panne de reader |
| `labels`  | fenêtres d'expériences annotées à la main |

**SQLite et pas PostgreSQL** : un seul writer, local, pas de réseau, pas de
concurrence. En WAL, SQLite encaisse 1 Hz sans transpirer et le fichier unique
se copie et se versionne trivialement. Postgres ici serait de la complexité
gratuite.

## Ce que chaque reader collecte

| Reader | Colonnes | Notes |
|--------|----------|-------|
| `kmsg` | `kmsg_errors`, `kmsg_criticals`, `kmsg_messages` | erreurs noyau classées, écrites dans `events` |
| `errcounters` | `err_mce`, `err_thermal_irq`, `err_aer_*`, `err_ecc_*`… | compteurs matériels, sans privilèges |
| `k10temp` | `k10temp_tctl`, `k10temp_tccd1`… | nommées d'après `tempN_label`, pas d'après `N` |
| `gigabyte_wmi` | `gigabyte_temp1..6` | sondes non labellisées, cf. protocole ci-dessous |
| `rapl` | `rapl_pkg_watts`, `rapl_core_watts` | puissance dérivée, root requis |
| `nvml` | `gpu_temp`, `gpu_power_w`, `gpu_throttle_mask`… | GPU NVIDIA |
| `amdgpu` | `amdgpu_edge`, `amdgpu_sclk_mhz`, `amdgpu_busy_pct`… | GPU/iGPU AMD |
| `nvme` | `nvme_composite`… | un reader par disque |
| `psi` | `psi_cpu_some`, `psi_io_full`… | contention, pas utilisation |
| `proc` | `cpu_util`, `cpuN_util`, `load1`, `mem_used_mb`, `cpu_freq_avg` | dérivées de jiffies |
| `context` | `uptime_s`, `top_proc_cpu`, `is_gaming`, `active_window`… | le contexte d'usage |

Trois d'entre eux méritent un mot.

**RAPL** n'est pas un capteur de puissance mais un compteur d'énergie cumulée en
µJ. On dérive `P = ΔE / Δt`, et le compteur **reboucle** à
`max_energy_range_uj` (~262 J, soit ~4 s à 65 W : ça arrive en permanence). Sans
traitement du wrap, on obtient des puissances négatives toutes les quelques
secondes — et le détecteur d'anomalies passe sa vie à détecter ce bug-là. Le cas
symétrique (compteur remis à zéro au réveil de veille, alors que
`CLOCK_MONOTONIC` n'a pas avancé) donne un pic aberrant : il est borné et
remplacé par `NULL`. Un trou dans les données vaut mieux qu'un pic inventé.

**PSI** mesure la **contention**, pas l'utilisation. Un CPU à 100 % peut avoir un
PSI nul (personne n'attend) ; un CPU à 40 % avec un PSI à 30 signifie que des
tâches sont bloquées. C'est exactement le signal qui apparaît quand un système
dérive alors que les métriques classiques restent vertes.

**NVML plutôt que `nvidia-smi`** : `nvidia-smi` forke un process et met ~200 ms.
À 1 Hz, on passerait 20 % du CPU à mesurer le CPU. NVML est un appel de
bibliothèque, ~50 µs. `gpu_throttle_mask` y est la métrique la plus précieuse :
le hardware dit lui-même *pourquoi* il se limite (thermique, power cap, voltage
reliability). C'est un label gratuit, stocké en bitmask brut et décomposé plus
tard au feature engineering.

## Le contexte, ou pourquoi ce n'est pas un notebook Kaggle

Sans contexte, le modèle apprend « GPU chaud = anomalie » et alerte dès qu'on
lance un jeu. Avec contexte, il apprend « GPU chaud **alors que rien ne tourne**
= anomalie ».

`is_gaming` est une décision de design, pas une évidence. Elle est ici
**tri-valuée** :

* `1` — la fenêtre active porte un indice connu (`steam_app`, `gamescope`,
  `lutris`, `wine`…) ;
* `0` — la fenêtre active est connue et ne correspond à aucun indice ;
* `NULL` — aucune information (daemon lancé hors session graphique, compositeur
  injoignable).

Écrire `0` dans le dernier cas reviendrait à affirmer « pas de jeu » alors qu'on
ne sait rien. Pour la même raison, `active_window` et `top_proc_name` sont
stockés **en TEXT brut** : on ne peut pas ré-étiqueter des données qu'on n'a pas
collectées, alors que l'heuristique, elle, se recalcule à volonté.

Le compositeur est interrogé par socket IPC Hyprland (pas `hyprctl`, qui forke)
et au plus toutes les 5 secondes.

## Protocoles expérimentaux

### Identifier les 6 sondes `gigabyte_wmi`

Le driver ne les labellise pas. Elles restent donc nommées par index, et le
mapping se détermine à l'expérience — le renommage se fera après coup par
`ALTER TABLE RENAME COLUMN`, ce n'est pas une raison pour deviner maintenant.

1. `stress-ng --cpu $(nproc) --timeout 5m` → la sonde qui monte le plus vite et
   redescend le plus vite est côté VRM ;
2. charge GPU seule → la sonde qui suit est côté PCIe/chipset ;
3. machine au repos, fenêtre ouverte → la sonde qui suit l'ambiante.

À consigner ici quand ce sera fait.

### Familles d'anomalies visées

| Label | Provocation |
|-------|-------------|
| `fan_curve_low` | courbe de ventilation abaissée dans le BIOS |
| `airflow_blocked` | grille d'entrée d'air obstruée |
| `oc_unstable` | OC/undervolt hors marge → WHEA dans `events` |
| `stress_ng` | charge synthétique de référence |
| `gaming` | charge réelle |
| `normal` | tout le reste |

## Détection d'erreurs

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

| Source | Quand | Clé de déduplication |
|--------|-------|----------------------|
| `/dev/kmsg` | root (défaut sous systemd) | numéro de séquence |
| `journalctl -k -f` | sans privilèges, si systemd | curseur journald |

Aucune des deux ne forke par tick : `/dev/kmsg` est un descripteur ouvert une
fois, `journalctl` un seul process pour toute la vie du daemon.

**Le démarrage rejoue tout le tampon du boot courant.** Les erreurs survenues
avant le lancement du collector — au boot, ou pendant que le service était
arrêté — sont récupérées avec leur date d'origine. La clé de déduplication rend
ce rejeu idempotent : redémarrer trois fois n'enregistre pas trois fois la même
MCE.

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

* ils fonctionnent **sans privilèges**, là où `/dev/kmsg` exige root ;
* ils attrapent ce que le noyau compte **sans forcément le journaliser** — une
  erreur PCIe corrigée n'écrit rien dans `dmesg` si le rate-limit a frappé, mais
  le compteur, lui, avance.

| Colonne | Source | Ce que ça détecte |
|---------|--------|-------------------|
| `err_mce` | `/proc/interrupts` MCE | Machine Check comptée par le CPU |
| `err_thermal_irq` | `/proc/interrupts` TRM | seuil thermique franchi ← ventirad encrassé |
| `err_threshold_irq`, `err_deferred_irq` | THR, DFR | seuils et erreurs différées AMD |
| `err_nmi` | NMI | alimentation, RAM, watchdog |
| `err_oom_kill` | `/proc/vmstat` | process tués faute de mémoire |
| `err_ecc_ce`, `err_ecc_ue` | EDAC | erreurs mémoire corrigées / non corrigées |
| `err_aer_corr/nonfatal/fatal` | PCIe AER | qualité du lien PCIe |
| `err_cpu_throttle`, `err_pkg_throttle` | `thermal_throttle` | bridages thermiques (Intel) |
| `err_gpu_ras` | RAS amdgpu | ECC mémoire vidéo |
| `err_disk_io` | `ioerr_cnt` | erreurs d'I/O SCSI |
| `err_nvme_state` | `/sys/class/nvme/*/state` | contrôleur hors de l'état `live` |

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

## Limites connues

Les dire vaut mieux que faire semblant.

* **Fréquence CPU.** `cpuinfo_avg_freq` (amd-pstate, noyaux récents) est une
  mesure ; `scaling_cur_freq`, utilisé en repli, n'est qu'une consigne du
  gouverneur et ment franchement sur AMD P-State. La vraie fréquence effective
  demanderait de lire les MSR APERF/MPERF — hors périmètre.
* **PSI.** On stocke `avg10`, déjà lissé par le noyau. Le compteur `total`
  (cumulé, en µs) serait dérivable et plus précis ; il reste récupérable plus
  tard si l'horizon de 14 jours le demande.
* **`top_proc_cpu`** est un pourcentage d'**un** cœur : 400 % est possible sur un
  process multithread. Le scan de `/proc` coûte quelques millisecondes par tick
  sur ~500 process.
* **Timestamps en `epoch ms`, clé primaire.** Deux ticks dans la même
  milliseconde se remplacent. À 1 Hz c'est théorique ; avec `--period 0.001` ça
  ne le serait plus.
* **`is_gaming`** repose sur une liste d'indices volontairement courte. Chaque
  entrée est une hypothèse sur le dataset ; c'est pour ça que le brut est
  conservé à côté.
* **iGPU.** `amdgpu_busy_pct` d'une iGPU mesure une puce qui partage son budget
  thermique et sa mémoire avec le CPU : les corrélations n'y ont pas le même
  sens que sur une carte dédiée.
* **Horodatage des erreurs kmsg.** Les enregistrements `/dev/kmsg` sont datés en
  µs depuis le boot sur une horloge qui ne compte pas le temps suspendu : après
  une veille, un événement d'avant peut être daté de quelques minutes en avance.
  Le backend journald, lui, donne un horodatage absolu exact.
* **Erreurs pendant que le collector est arrêté.** Le rejeu couvre le tampon du
  **boot courant** uniquement. Une erreur survenue lors d'un boot précédent
  n'est pas récupérée — `journalctl -k -b -1` reste à faire à la main.
* **La liste `BENIGN` est empirique.** Elle a été construite sur les messages
  réellement observés ; un pilote présent seulement sur l'autre machine peut
  introduire un nouveau faux positif. C'est à ça que sert `--scan` avant de
  lancer une collecte de 14 jours.

## Dépendances

Aucune obligatoire : `/proc`, `/sys` et la stdlib (`sqlite3` inclus) suffisent.
`nvidia-ml-py` est optionnel et n'intéresse que les machines NVIDIA — sans lui,
`NvmlReader` se désactive proprement au démarrage.
