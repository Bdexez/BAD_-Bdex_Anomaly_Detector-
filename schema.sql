PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- Un boot = un contexte. Les chemins hwmon renumérotent, les compteurs RAPL
-- repartent de zéro, et une "dérive lente" ne se compare qu'à boot_id près.
CREATE TABLE IF NOT EXISTS boots (
    boot_id   TEXT PRIMARY KEY,   -- /proc/sys/kernel/random/boot_id
    ts_first  INTEGER NOT NULL,   -- epoch ms du premier sample de ce boot
    kernel    TEXT,
    notes     TEXT
);

-- Format wide : ~50 colonnes REAL, 1 ligne/seconde.
-- 14 jours à 1 Hz = ~1.2M lignes. En narrow (ts, metric, value) ce serait 60M.
-- Les colonnes sont ajoutées dynamiquement par storage.ensure_columns(), donc
-- ce fichier ne décrit que le squelette invariant.
CREATE TABLE IF NOT EXISTS samples (
    ts       INTEGER PRIMARY KEY,   -- epoch ms
    boot_id  TEXT NOT NULL
);
-- Presque toutes les requêtes d'analyse sont "cette fenêtre, ce boot" :
-- sans cet index, filtrer un boot force un scan complet de 1.2M lignes.
CREATE INDEX IF NOT EXISTS idx_samples_boot ON samples(boot_id, ts);

-- Événements discrets : erreurs noyau, resets GPU, OOM, MCE, ECC, PCIe AER,
-- plus les pannes de readers signalées par le collector lui-même.
-- C'est ici que vit la "famille 3" (instabilité d'OC).
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    source   TEXT NOT NULL,   -- 'kmsg' | 'errcounters' | 'nvml' | 'collector'
    kind     TEXT NOT NULL,   -- voir collector/errors.py pour le catalogue
    severity TEXT,            -- 'critical' | 'error' | 'warning' | 'info'
    message  TEXT,
    dedup    TEXT             -- clé stable de la source (seq kmsg, curseur journald)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);
-- Rejouer le tampon noyau au démarrage doit être idempotent : sans cet index,
-- redémarrer le collector trois fois enregistrerait trois fois la même MCE.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup ON events(dedup) WHERE dedup IS NOT NULL;

-- Tes annotations d'expériences. C'est TA valeur ajoutée : le dataset labellisé.
-- Alimenté par `python -m collector.label`.
CREATE TABLE IF NOT EXISTS labels (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start INTEGER NOT NULL,
    ts_end   INTEGER,          -- NULL = expérience en cours
    label    TEXT NOT NULL,    -- 'normal' | 'fan_curve_low' | 'airflow_blocked' | 'oc_unstable' | 'stress_ng' | 'gaming'
    notes    TEXT
);
CREATE INDEX IF NOT EXISTS idx_labels_ts ON labels(ts_start, ts_end);
