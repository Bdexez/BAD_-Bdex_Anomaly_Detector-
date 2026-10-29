![BAD — Linux telemetry, context included](docs/visuals/cover.svg)

# BAD · Bdex Anomaly Detector

**Build a hardware anomaly dataset that explains what the machine was doing.**

BAD collects Linux hardware telemetry, workload context and fault events into a local SQLite database. It is designed for labelled experiments around cooling, airflow and system instability, with a collector that keeps running when an optional source is unavailable.

`Python 3.10+` · `Linux /proc + /sys` · `SQLite WAL` · `No required third-party packages`

[Start collecting](#quick-start) · [Design](#design) · [Readers](#data-sources) · [Full reference](docs/REFERENCE.md)

> **Current scope:** telemetry collection, fault classification and experiment labels are implemented. Training a learned anomaly detector is a later stage; this repository prepares the data for it.

## What makes the data useful

| Layer | What it records | Why it matters |
| :--- | :--- | :--- |
| **Hardware** | CPU/GPU temperatures, power, utilization and exposed device sensors | Captures the machine's physical response. |
| **Workload context** | CPU/memory pressure, active processes and usage hints | Helps distinguish a demanding workload from an abnormal response. |
| **Fault evidence** | Classified kernel messages and hardware error-counter deltas | Adds evidence beyond temperature thresholds. |
| **Ground truth** | Manually labelled experiment windows | Preserves what was deliberately changed during an experiment. |

## Quick start

From the repository root, using Python 3.10 or later:

```bash
python -m collector.main --doctor
python -m collector.main --scan
python -m collector.main --db data/metrics.db --period 1.0
```

`--doctor` describes the sources this machine exposes. `--scan` classifies errors from the current boot without starting continuous database collection. Reader availability depends on the hardware, drivers and permissions; RAPL energy readings may require elevated access.

`--period` and `--duration` accept finite, positive seconds. Duration limits and stop signals interrupt the wait between samples, including with long sampling periods.

Annotate an experiment in a second terminal:

```bash
python -m collector.label start baseline -n "normal cooling, usual workload"
python -m collector.label end
python -m collector.label list
python -m collector.label list --all
```

For unattended collection, see the [systemd unit](systemd/bad-collector.service). Optional NVIDIA support uses `nvidia-ml-py`; the rest of the collector can run with the standard library.

`label list --all` includes completed experiments, their boundaries and notes. Empty labels and end times before the start are rejected. Ending several windows validates them together, so an invalid time closes none of them.

## Export a labelled dataset

```bash
python -m collector.export --db data/metrics.db --output data/dataset.csv
python -m collector.export --db data/metrics.db --boot BOOT_ID --columns ts cpu_util mem_used_mb
```

The export reads a consistent snapshot while collection continues, without modifying or migrating the database. It writes one CSV row per sample and a `labels_json` column containing every matching experiment, its notes and time boundaries. An empty array means no annotation; no label is guessed. Missing measurements become empty CSV cells.

Use `--since` and `--until` for an inclusive/exclusive range in epoch milliseconds. Output defaults to stdout; `--output` creates a new file and refuses to overwrite an existing one. See the [export reference](docs/REFERENCE.md#exporting-data) for examples.

## Check dataset quality

Check the dataset before exporting or training:

```bash
python -m collector.report --db data/metrics.db
python -m collector.report --db data/metrics.db --json
```

The report shows metric completeness, annotation coverage, sample windows per boot, repeated timestamps and grouped fault events. It reads a consistent snapshot without modifying the database; overlapping experiment labels count each sample once. These are data quality facts, not inferred anomaly scores.

## Back up an active collection

```bash
python -m collector.backup --db data/metrics.db --output data/metrics-backup.db
```

The SQLite backup includes committed WAL transactions while collection continues, checks the copy and produces a standalone database file. It refuses to overwrite existing files. `--timeout` limits copying (30 seconds by default); pending samples still buffered in RAM are included only after the collector flushes them.

## Design

Each reader owns one source, discovers it during setup and returns a dictionary of values on each tick. A missing source disables itself; read failures are tracked without bringing down the acquisition loop.

| Decision | Implementation |
| :--- | :--- |
| **Discover sensors by identity** | Resolve hwmon devices by their `name` rather than unstable numeric paths. |
| **Keep labels explicit** | Missing ground truth remains `NULL`; the collector does not guess experiment outcomes. |
| **Handle cumulative counters** | Convert RAPL energy deltas into power, account for wraparound, and reject implausible reset spikes. |
| **Recover current-boot events** | Replay available kernel records and deduplicate them before storage. |
| **Retry failed writes** | Keep pending samples and events in bounded memory queues while SQLite is unavailable. |
| **Isolate invalid readings** | Replace invalid values with `NULL` and preserve healthy metrics in the same batch. |
| **Store locally** | A wide time-series table and SQLite WAL suit a single collector without a database server. |

Samples are flushed after 30 new readings or 30 seconds, including when sampling less frequently. Failed batches are retried at these intervals. Each queue holds up to 10,000 entries; saturation drops the oldest entries and logs the loss. Pending data lives in memory, so a forced exit or shutdown during a persistent storage failure can still lose it. Existing databases are migrated automatically while preserving their rows and sensor columns.

Sensor names remain distinct when hardware labels repeat. Long labels receive a stable hash suffix to fit the 63-character column limit. For hwmon power, discovery prefers a readable average and falls back to an instantaneous input; the selected source stays fixed during collection.

## Data sources

| Source group | Readers |
| :--- | :--- |
| CPU, memory and pressure | `proc`, `psi`, `k10temp`, `rapl` |
| GPU | `nvml` for NVIDIA, `amdgpu` for AMD |
| Board and storage sensors | `gigabyte_wmi`, `nvme`, shared `hwmon` discovery |
| Error evidence | `kmsg`, `errcounters` |
| Workload context | `context` |

The schema separates **samples**, **boots**, **events** and **labels**. Columns evolve with discovered readers; unavailable readings remain missing rather than invented values. See [schema.sql](schema.sql) and the [reader catalogue](docs/REFERENCE.md#-what-each-reader-collects).

Each sample has its own `sample_id`; its `ts` remains the original epoch timestamp in milliseconds. Equal timestamps and clock corrections retain every measurement. Older databases receive an atomic, one-time table migration on first use.

## Verification

```bash
python -m unittest discover -s tests -t .
```

The suite exercises fake `/proc` and `/sys` trees, sensor discovery, counter wraparound, kernel events, real SQLite lock contention, legacy migrations, write retries, buffer limits and collection deadlines without requiring the matching hardware.

## Explore the implementation

| Entry point | Responsibility |
| :--- | :--- |
| [`collector/main.py`](collector/main.py) | Acquisition loop and reader lifecycle |
| [`collector/readers/`](collector/readers/) | Source-specific discovery and reads |
| [`collector/errors.py`](collector/errors.py) | Kernel fault classification rules |
| [`collector/storage.py`](collector/storage.py) | SQLite schema evolution, batching, events and labels |
| [`collector/label.py`](collector/label.py) | Experiment annotation CLI |
| [`collector/export.py`](collector/export.py) | Read-only CSV export with experiment annotations |
| [`collector/report.py`](collector/report.py) | Dataset completeness and annotation coverage |
| [`collector/backup.py`](collector/backup.py) | Consistent backup while collection continues |

Coverage is hardware-dependent. Software counters cannot expose every physical failure, and heuristic workload labels need interpretation. The [complete reference](docs/REFERENCE.md) documents measurement limits, experiment protocols and machine-specific coverage.
