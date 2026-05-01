# Single-EDF EDA/QC Toolkit

This repository provides tools to run exploratory data analysis (EDA) and quality control (QC) on EDF files, with practical workflows validated on Siena-style data layout.

## Scripts

- `scripts/single_edf_eda.py`
  - Runs EDA/QC for one EDF file.
  - Produces `ana.txt`, `errors.jsonl`, optional CSV tables, and optional plots.
- `scripts/generate_eda_report.py`
  - Builds a Markdown report draft from `ana.txt`.
  - Reports can then be manually rewritten for deeper interpretation.

## Data

### Source Data

All current work in this project is organized under `data/`, with source EDF data expected in:

- `data/raw/siena/...`

Current analysis/rewrite workflow is based on these source EDF files. The generated outputs and reports are produced from this `data/raw/siena` source tree.

## Outputs

Typical per-case outputs are stored in:

- `outputs/single_edf_eda/...`

Common artifacts:
- `ana.txt` (core analysis summary)
- `errors.jsonl` (step-level non-fatal errors/warnings)
- `tables/*.csv` (channel/statistics tables)
- `plots/*.png` (figures)
- `EDA_report.md` (human-readable report)

## Environment

Use the existing Conda environment.

Typical dependencies:
- `mne`
- `pyedflib`
- `numpy`
- `pandas`
- `scipy`
- `matplotlib`

## Quick Start

```bash
python scripts/single_edf_eda.py --edf data/raw/siena/PN06/PN06-1.edf
```

```bash
python scripts/single_edf_eda.py \
  --edf <path_to_edf> \
  --out outputs/single_edf_eda/<edf_stem> \
  --seizure-list <optional_txt> \
  --plot-seconds 30 \
  --psd-max-minutes 10 \
  --corr-max-minutes 5 \
  --max-plot-channels-per-page 24 \
  --overwrite \
  --verbose
```

```bash
python scripts/generate_eda_report.py --ana outputs/single_edf_eda/<case>/ana.txt
```
