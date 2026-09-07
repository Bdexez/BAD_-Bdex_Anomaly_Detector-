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

## 🧭 Context, or why this is not a Kaggle notebook

Without context, the model learns "hot GPU = anomaly" and alerts as soon as you
start a game. With context, it learns "hot GPU **while nothing is running** =
anomaly".

`is_gaming` is a design decision, not an obvious one. It is **three-valued** here:

| Value   | Meaning                                                                          |
|:-------:|----------------------------------------------------------------------------------|
| `1`     | the active window carries a known hint (`steam_app`, `gamescope`, `lutris`, `wine`…) |
| `0`     | the active window is known and matches no hint                                    |
| `NULL`  | no information (daemon started outside a graphical session, compositor unreachable) |

Writing `0` in the last case would amount to asserting "no game" when we know
nothing. For the same reason, `active_window` and `top_proc_name` are stored as
**raw TEXT**: you cannot re-label data you never collected, whereas the heuristic
can be recomputed at will.

The compositor is queried through the Hyprland IPC socket (not `hyprctl`, which
forks) and at most every 5 seconds.

## 🧪 Experimental protocols

### Identifying the 6 `gigabyte_wmi` probes

The driver does not label them. They therefore stay named by index, and the
mapping is determined experimentally — the rename will happen afterwards through
`ALTER TABLE RENAME COLUMN`, which is no reason to guess now.

1. `stress-ng --cpu $(nproc) --timeout 5m` → the probe that rises fastest and
   falls fastest is on the VRM side;
2. GPU load alone → the probe that follows is on the PCIe/chipset side;
3. machine idle, window open → the probe that follows ambient temperature.

> 📝 To be recorded here once it is done.

### Targeted anomaly families

| Label             | How it is provoked                                   |
|-------------------|------------------------------------------------------|
| `fan_curve_low`   | fan curve lowered in the BIOS                        |
| `airflow_blocked` | air intake grille obstructed                         |
| `oc_unstable`     | OC/undervolt out of margin → WHEA in `events`        |
| `stress_ng`       | synthetic reference load                             |
| `gaming`          | real load                                            |
| `normal`          | everything else                                      |

## 🚨 Error detection

This is the `events` table: a point-in-time event — an MCE lasts a microsecond —
cannot exist in a series sampled at 1 Hz. It needs a channel of its own.

"Every possible error" does not exist as a finite list: a driver may invent a
message tomorrow that no pattern knows about. Coverage therefore rests on **three
stages**, each catching the others' blind spots.

### Stage 1 — Classifying kernel messages

38 rules in `collector/errors.py`, arranged into six families: CPU/memory (MCE,
APEI/GHES, ECC, microcode), stability (panic, oops, lockups, RCU stall, GPF),
memory (OOM), GPU (NVIDIA Xid with code translation, AMD resets and faults, video
ECC), storage (NVMe, ATA, block, FS corruption) and bus (PCIe AER, link, USB,
thermal threshold, power, ACPI, firmware, suspend).

The file is **data, not code**: a pattern, a kind, a severity and a sentence of
explanation. Adding a rule does not require touching the engine.

Two sources, chosen at startup:

| Source            | When                          | Deduplication key      |
|-------------------|-------------------------------|------------------------|
| `/dev/kmsg`       | root (default under systemd)  | sequence number        |
| `journalctl -k -f`| without privileges, if systemd| journald cursor        |

Neither forks per tick: `/dev/kmsg` is a descriptor opened once, `journalctl` a
single process for the daemon's whole life.

> 🔁 **Startup replays the entire buffer of the current boot.** Errors that
> occurred before the collector was launched — at boot, or while the service was
> stopped — are recovered with their original timestamp. The deduplication key
> makes this replay idempotent: restarting three times does not record the same
> MCE three times.

### Stage 2 — The catch-all

Any record the kernel itself marks at priority ≤ 3 (err, crit, alert, emerg) and
that no rule recognises is recorded as `kernel_error` with its raw text. That is
what avoids only detecting the failures you had already imagined.

The trade-off: the kernel emits at "error" priority messages that describe a
normal state (`TDX not supported`, `RAS: Correctable Errors collector
initialized`…). An explicit `BENIGN` list filters them out — without it, every
boot would add the same non-events, and a constant present in 100 % of the rows
teaches a model nothing. It is completed machine by machine, and `--scan` exists
precisely for that.

### Stage 3 — Hardware counters

Independent of the text, and that is what makes them valuable:

- they work **without privileges**, where `/dev/kmsg` requires root;
- they catch what the kernel counts **without necessarily logging it** — a
  corrected PCIe error writes nothing to `dmesg` if the rate limit kicked in, but
  the counter still moves.

| Column                                   | Source                    | What it detects                             |
|------------------------------------------|---------------------------|---------------------------------------------|
| `err_mce`                                | `/proc/interrupts` MCE    | Machine Check counted by the CPU            |
| `err_thermal_irq`                        | `/proc/interrupts` TRM    | thermal threshold crossed ← clogged heatsink |
| `err_threshold_irq`, `err_deferred_irq`  | THR, DFR                  | AMD thresholds and deferred errors          |
| `err_nmi`                                | NMI                       | power supply, RAM, watchdog                 |
| `err_oom_kill`                           | `/proc/vmstat`            | processes killed for lack of memory         |
| `err_ecc_ce`, `err_ecc_ue`               | EDAC                      | corrected / uncorrected memory errors       |
| `err_aer_corr/nonfatal/fatal`            | PCIe AER                  | PCIe link quality                            |
| `err_cpu_throttle`, `err_pkg_throttle`   | `thermal_throttle`        | thermal throttling (Intel)                  |
| `err_gpu_ras`                            | amdgpu RAS               | video memory ECC                            |
| `err_disk_io`                            | `ioerr_cnt`               | SCSI I/O errors                             |
| `err_nvme_state`                         | `/sys/class/nvme/*/state` | controller out of the `live` state          |

Each counter is **both an event and a feature**: its delta per tick becomes a
column of `samples`, and any movement writes a row into `events`. A non-zero value
**at startup** is reported too: a machine already counting 4,000 corrected ECC
errors since boot should say so at the first tick, not wait for the 4,001st.

### Coverage, machine by machine

It is not the same everywhere, and `--doctor` says so — including what is **not**
monitored, which matters as much as the rest: without an EDAC controller, a
corrected memory error leaves no trace anywhere.

Measured on the laptop (Ryzen 5 3500U, without root): 9 active counters;
`err_ecc_*` absent (non-ECC memory), `err_cpu_throttle` absent (Intel counter),
`err_gpu_ras` absent (iGPU), `err_disk_io` absent (NVMe, not SCSI).
On the desktop, expect the GPU RAS counters to appear and, depending on the
motherboard, EDAC.

## ⚠️ Known limitations

Saying them beats pretending.

- **CPU frequency.** `cpuinfo_avg_freq` (amd-pstate, recent kernels) is a
  measurement; `scaling_cur_freq`, used as a fallback, is only a governor setpoint
  and lies outright on AMD P-State. The true effective frequency would require
  reading the APERF/MPERF MSRs — out of scope.
- **PSI.** We store `avg10`, already smoothed by the kernel. The `total` counter
  (cumulative, in µs) would be derivable and more precise; it can still be picked
  up later if the 14-day horizon calls for it.
- **`top_proc_cpu`** is a percentage of **one** core: 400 % is possible for a
  multithreaded process. Scanning `/proc` costs a few milliseconds per tick over
  ~500 processes.
- **Timestamps in `epoch ms`, primary key.** Two ticks within the same
  millisecond overwrite each other. At 1 Hz that is theoretical; with
  `--period 0.001` it would no longer be.
- **`is_gaming`** rests on a deliberately short list of hints. Each entry is a
  hypothesis about the dataset; that is why the raw value is kept alongside.
- **iGPU.** `amdgpu_busy_pct` on an iGPU measures a chip that shares its thermal
  budget and its memory with the CPU: correlations there do not have the same
  meaning as on a discrete card.
- **kmsg error timestamps.** `/dev/kmsg` records are dated in µs since boot on a
  clock that does not count suspended time: after a sleep, an earlier event can be
  dated a few minutes ahead.  The journald backend, by contrast, gives an exact
  absolute timestamp.
- **Errors while the collector is stopped.** The replay covers the buffer of the
  **current boot** only. An error from a previous boot is not recovered —
  `journalctl -k -b -1` still has to be done by hand.
- **The `BENIGN` list is empirical.** It was built from the messages actually
  observed; a driver present only on the other machine can introduce a new false
  positive. That is what `--scan` is for before launching a 14-day collection.

## 📦 Dependencies

None required: `/proc`, `/sys` and the standard library (`sqlite3` included) are
enough. `nvidia-ml-py` is optional and only matters on NVIDIA machines — without
it, `NvmlReader` disables itself cleanly at startup.
