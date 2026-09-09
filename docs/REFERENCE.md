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

