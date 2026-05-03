# Reproduction trial of Precise and low-power closed-loop neuromodulation through algorithm-integrated circuit co-design

## 1 Data

### 1.1 Source data information

This project currently uses the **SIENA Scalp EEG** dataset as the source data, stored under `data/raw/siena/`. The working subset contains **21 EDF recordings** from **5 subjects** (`PN06`, `PN09`, `PN10`, `PN13`, `PN14`), selected from the SIENA `RECORDS` index and verified by the download audit (`siena_download_audit_summary_2026-05-01.txt`, passed 21/21, failed 0/21). Metadata in `subject_info.csv` indicates the dataset includes multi-subject scalp EEG recordings with seizure annotations listed in per-subject `Seizures-list-*.txt` files and demographic/clinical descriptors (age, gender, seizure type, localization, lateralization, channel count, seizure counts, and recording duration).

Data types and content in this repo include: raw EDF signals (`data/raw/siena/**/*.edf`), seizure event text annotations (`Seizures-list-*.txt`), source metadata tables (`subject_info.csv`, `RECORDS`, checksums and audit logs), and processed artifacts under `data/processed/rtms_eeg_preproc/` (e.g., cleaned FIF files, per-record `summary.json`, and preprocessing reports). For example, `PN06-1` has 512 Hz sampling, 9615 s duration, 37 channels in total, and 30 normalized EEG channels after excluding non-EEG channels during preprocessing.

### 1.2 EDA

EDA is implemented in `scripts/single_edf_eda.py` and executed per EDF file with reproducible outputs under `outputs/single_edf_eda/`. The pipeline performs dual-reader loading and consistency checks (`pyedflib` and `mne`), channel metadata extraction (name/type/unit/sampling rate/physical-digital range), channel-level quality statistics (mean, std, peak-to-peak, robust outlier ratio, flatline ratio, saturation ratio), PSD feature extraction via Welch method (delta/theta/alpha/beta/gamma band powers and ratios, low-frequency drift ratio, high-frequency noise ratio, 50/60 Hz line components), within-group channel correlation analysis, seizure-list time parsing and in-range validation, and automatic figure/table export for QA traceability.

The QA results from `outputs/single_edf_eda/` show that files are generally readable and structurally consistent (for example, `PN06-1` has no file-level flags or parsing errors, and metadata from two readers is consistent). At the same time, non-neural or auxiliary channels can contain persistent flatline behavior (e.g., `SPO2`, `HR`, `MK` in `PN06-1`), and some channels show clear line-noise concentration (notably `P9` and `P10`, with very high 50 Hz ratio and near-perfect mutual correlation). These observations match the preprocessing summaries in `data/processed/rtms_eeg_preproc/**/summary.json`, where `P9/P10` are repeatedly identified as line-noise-affected channels. Overall, the dataset is suitable for modeling, but explicit noise-aware handling is necessary to avoid learning artifacts as predictive shortcuts.

### 1.3 Preprocess

The source article did not apply preprocessing before segmenting windows and feeding data into the CNN model. At the same time, the EDA results in this project confirm that real noise and interference are present. This design choice is understandable because, in real on-device scenarios, interference is unavoidable and the model should adapt to such conditions. However, one risk is that the model may overfit to noise patterns, which can act as a form of feature leakage. Considering this trade-off, one possible approach is to prepare both preprocessed and raw data: use preprocessed data for longer-epoch training to learn stable physiological features, then perform short-epoch, low-learning-rate fine-tuning on raw data to improve robustness under realistic noise conditions. In this reproduction, a preprocessed dataset was also generated, and models were trained using both the original article's method and the proposed method, so that the practical impact of preprocessing on real predictive performance could be compared.

#### 1.3.1 Prprocess strategy
