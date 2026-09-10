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

> 🛠️ **As a service**: see [`systemd/bad-collector.service`](../systemd/bad-collector.service).

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

