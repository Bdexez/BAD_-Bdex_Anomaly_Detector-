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

