# EEG-Seizure-Prediction

## 1 Data

### 1.1 Source data information

This project currently uses the **SIENA Scalp EEG** dataset as the source data, stored under `data/raw/siena/`. The working subset contains **21 EDF recordings** from **5 subjects** (`PN06`, `PN09`, `PN10`, `PN13`, `PN14`), selected from the SIENA `RECORDS` index and verified by the download audit (`siena_download_audit_summary_2026-05-01.txt`, passed 21/21, failed 0/21). Metadata in `subject_info.csv` indicates the dataset includes multi-subject scalp EEG recordings with seizure annotations listed in per-subject `Seizures-list-*.txt` files and demographic/clinical descriptors (age, gender, seizure type, localization, lateralization, channel count, seizure counts, and recording duration).

During training, it was further observed that the available SIENA subset alone was not sufficient to provide enough data volume for stable model development. For this reason, the dataset was later expanded with an additional seizure-related EEG source obtained from Kaggle, so the overall data origin of the project is no longer limited to a single public corpus. In practice, SIENA serves as the initial well-structured clinical source with explicit seizure timing annotations, while the later Kaggle supplement is used to alleviate the small-sample problem and improve the diversity of training examples.

Data types and content in this repo include: raw EDF signals (`data/raw/siena/**/*.edf`), seizure event text annotations (`Seizures-list-*.txt`), source metadata tables (`subject_info.csv`, `RECORDS`, checksums and audit logs), and processed artifacts under `data/processed/rtms_eeg_preproc/` (e.g., cleaned FIF files, per-record `summary.json`, and preprocessing reports). For example, `PN06-1` has 512 Hz sampling, 9615 s duration, 37 channels in total, and 30 normalized EEG channels after excluding non-EEG channels during preprocessing.

### 1.2 EDA

EDA is implemented in `scripts/single_edf_eda.py` and executed per EDF file with reproducible outputs under `outputs/single_edf_eda/`. The pipeline performs dual-reader loading and consistency checks (`pyedflib` and `mne`), channel metadata extraction (name/type/unit/sampling rate/physical-digital range), channel-level quality statistics (mean, std, peak-to-peak, robust outlier ratio, flatline ratio, saturation ratio), PSD feature extraction via Welch method (delta/theta/alpha/beta/gamma band powers and ratios, low-frequency drift ratio, high-frequency noise ratio, 50/60 Hz line components), within-group channel correlation analysis, seizure-list time parsing and in-range validation, and automatic figure/table export for QA traceability.

The QA results from `outputs/single_edf_eda/` show that files are generally readable and structurally consistent (for example, `PN06-1` has no file-level flags or parsing errors, and metadata from two readers is consistent). At the same time, non-neural or auxiliary channels can contain persistent flatline behavior (e.g., `SPO2`, `HR`, `MK` in `PN06-1`), and some channels show clear line-noise concentration (notably `P9` and `P10`, with very high 50 Hz ratio and near-perfect mutual correlation). These observations match the preprocessing summaries in `data/processed/rtms_eeg_preproc/**/summary.json`, where `P9/P10` are repeatedly identified as line-noise-affected channels. Overall, the dataset is suitable for modeling, but explicit noise-aware handling is necessary to avoid learning artifacts as predictive shortcuts.

### 1.3 Preprocess

Some of the resaerch did not apply preprocessing before segmenting windows and feeding data into the CNN model. At the same time, the EDA results in this project confirm that real noise and interference are present. This design choice is understandable because, in real on-device scenarios, interference is unavoidable and the model should adapt to such conditions. However, one risk is that the model may overfit to noise patterns, which can act as a form of feature leakage. Considering this trade-off, one possible approach is to prepare both preprocessed and raw data: use preprocessed data for longer-epoch training to learn stable physiological features, then perform short-epoch, low-learning-rate fine-tuning on raw data to improve robustness under realistic noise conditions. In this reproduction, a preprocessed dataset was also generated, and models were trained using both the original article's method and the proposed method, so that the practical impact of preprocessing on real predictive performance could be compared.

In related EEG preprocessing studies, bad-channel rejection and interpolation are often decided at the level of the whole recording. However, in long-term epilepsy monitoring, where a single record may last for hours, some artifacts that make a channel temporarily unreliable are more likely to appear only during limited time spans rather than persist throughout the entire session. This intuition is also supported by the timeline-style diagnostics in `outputs/p2p_timeline_20260503_220733/PN06-1_timeline.png` and similar plots, which show that abnormal peak-to-peak behavior can be temporally localized instead of globally stable. Based on this observation, a more reasonable strategy is to divide the long EEG stream into multiple medium-length segments, which is also closer to the scale commonly used in classical preprocessing studies, and then perform bad-channel identification and interpolation separately within each segment.

To examine the effect of different preprocessing strategies, the experiments were organized around three data conditions. The first condition is the raw baseline, which follows the original no-preprocessing idea as closely as possible: EEG is segmented into prediction windows directly from the original recording so that the model is exposed to realistic sensor noise, motion contamination, and line interference. This condition is useful as the deployment-oriented reference because it tests whether the network can still detect preictal structure without any explicit denoising.

The second condition is the rule-based preprocessing pipeline implemented in `scripts/siena_eeg_preprocess.py`, and its outputs are stored as `preprocessed_eeg_v1.fif` plus a per-record mapping file. In this pipeline, non-EEG channels are first removed, then the recording is divided into 60 s segments. For each segment, 50 Hz line-noise power is estimated on the unfiltered signal, after which a 50 Hz notch filter is applied. A channel is then marked as segment-level bad if it shows abnormally strong line noise, low correlation with peer EEG channels, or more than 10 s of local abnormal behavior detected in 1 s sliding windows using flatline and excessive peak-to-peak thresholds. Segments with more than 5 bad channels are discarded entirely; otherwise, bad channels are repaired with spherical-spline interpolation, and remaining short local artifacts are replaced by averaging peer channels only within the affected windows. After valid segments are concatenated, a final 0.5-60 Hz band-pass filter is applied. This version is intended to preserve as much physiological activity as possible while explicitly suppressing artifacts that were repeatedly identified by the EDA stage.

The third condition is the ICA-enhanced preprocessing branch, stored as `preprocessed_eeg_v2_ica.fif`. It inherits the entire V1 pipeline and then applies FastICA to the cleaned signal. If `mne-icalabel` is available, components are classified with ICLabel and components with eye-artifact or muscle-artifact probability >= 0.90 are removed; otherwise, a fallback heuristic based on frontal-channel correlation and high-frequency muscle power is used. In this way, V2 targets residual structured artifacts that may survive channel-level interpolation, especially ocular and myogenic contamination.

After preprocessing, `scripts/build_siena_windows.py` builds the learning dataset for both `v1` and `v2` under `data/processed/siena_slices/SPH5m_PIL30m/`. Windows of 10 s, 20 s, and 30 s are generated, with preictal labels defined by `SPH=5 min` and `PIL=30 min`. Because segment rejection shortens the processed timeline, each preprocessed file is accompanied by a mapping JSON that converts processed window positions back to original recording time; only windows fully contained inside a kept segment are retained. This design ensures that seizure-relative labels remain aligned with the source EDF timeline even after artifact rejection and segment removal.

## 2 Modeling

### 2.1 Model structure

The base CNN used in this project is implemented in `scripts/CNN_base.py`. Its input is an EEG window tensor with shape `(B, C, T)`, where `B` is batch size, `C` is the number of retained EEG channels after preprocessing, and `T` is the number of samples in the 10 s, 20 s, or 30 s analysis window. In the default SIENA setting described earlier, preprocessing keeps 30 EEG channels, so a typical input is `(B, 30, T)`. Before convolution, the tensor is expanded to `(B, 1, C, T)` so that the network can treat the EEG window as a single-channel 2D map whose vertical axis is electrode/channel index and horizontal axis is time.

The key architectural idea is a two-axis convolution design. The network does not mix time and space immediately with square kernels. Instead, it first applies temporal-only convolutions with kernel shape `1 x k`, which slide only along the time axis while preserving the full channel layout. This stage learns within-channel temporal motifs such as rhythmic bursts, preictal trend changes, or transient waveform patterns without prematurely blending information across electrodes. After temporal features are formed, the network switches to spatial-only convolutions with kernel shape `k x 1`, which slide across the channel axis while keeping the time axis fixed. This second stage is used to model cross-electrode structure, such as whether correlated activity emerges across multiple channels or whether a temporal pattern becomes spatially distributed in a seizure-relevant way.

All convolutions use the custom `Conv2dSame` wrapper, which applies dynamic SAME padding before the convolution. As a result, the convolution itself preserves feature-map size, and spatial or temporal downsampling is controlled explicitly by pooling rather than by the convolution stride. Each block follows the order:

`Conv2dSame -> BatchNorm2d (optional) -> activation -> pooling`

An important implementation detail is that these architectural choices are intentionally exposed as an interface rather than hard-coded into one immutable network. In `scripts/CNN_base.py`, the constructor receives the temporal block widths, temporal output channels, temporal pooling widths, spatial block heights, spatial output channels, spatial pooling heights, batch-normalization switch, activation type, pooling type, convolution bias option, and output mode from the training/config side. In other words, the model skeleton is fixed as "temporal stack -> spatial stack -> global average pooling -> linear classifier", but the detailed hyperparameters of each stage are passed in from outside and can be changed without rewriting the model definition.

The description below therefore corresponds to the default instantiation currently returned by `get_default_cnn_kwargs()` in `scripts/train.py`. In that default configuration, batch normalization is enabled, the activation is `ReLU`, pooling is `MaxPool2d`, convolution bias is disabled, and the classifier returns logits rather than probabilities.

The default detailed architecture is:

| Stage   | Type                    | Out channels           | Kernel                  | Pool      | Role                                                               |
| ------- | ----------------------- | ---------------------- | ----------------------- | --------- | ------------------------------------------------------------------ |
| Input   | EEG window              | 1 pseudo-image channel | -                       | -         | reshape `(B, C, T)` to `(B, 1, C, T)`                          |
| Block 1 | Temporal conv           | 4                      | `1 x 8`               | `1 x 8` | extract short temporal motifs independently per EEG channel        |
| Block 2 | Temporal conv           | 16                     | `1 x 16`              | `1 x 4` | expand temporal feature depth and capture longer temporal patterns |
| Block 3 | Temporal conv           | 16                     | `1 x 8`               | `1 x 4` | refine temporal representation before channel mixing               |
| Block 4 | Spatial conv            | 16                     | `16 x 1`              | `4 x 1` | aggregate neighboring-channel structure across electrodes          |
| Block 5 | Spatial conv            | 16                     | `16 x 1`              | `4 x 1` | deepen cross-channel spatial integration                           |
| Head    | Global pooling + linear | 2 classes              | adaptive `1 x 1` + FC | -         | convert feature maps to seizure/non-seizure logits                 |

This means the architecture is anisotropic by construction: temporal abstraction is performed first, and only afterwards does the model learn spatial relations between electrodes. Pooling follows the same separation principle. Temporal blocks downsample only along time with pool sizes `(1, 8)`, `(1, 4)`, and `(1, 4)`, while spatial blocks downsample only along the channel axis with pool sizes `(4, 1)` and `(4, 1)`. This separation keeps the interpretation of each stage clear: early layers answer "what waveform pattern is present over time?", and later layers answer "how is that pattern distributed across EEG channels?". If a NAS-decoded architecture or another custom configuration is supplied, these concrete channel counts, kernel sizes, and pooling sizes can change, but the two-axis design principle remains the same.

After the five convolutional blocks, the model uses `AdaptiveAvgPool2d((1, 1))`, which collapses the remaining channel-axis and time-axis dimensions into one global descriptor per output feature channel. The final linear layer maps that 16-dimensional pooled representation to two output logits. Because of adaptive global average pooling, the network can accept variable-length windows in principle; in practice, the project still trains on fixed-duration windows so that each experiment remains comparable across preprocessing branches and dataset splits.

### 2.2 Training and NAS strategy

The training pipeline in `scripts/train.py` keeps the optimization and evaluation protocol fixed while using an RNN-based NAS controller to refine the detailed CNN instantiation inside the two-axis model family described in Section 2.1. In other words, the overall strategy is to train CNN candidates from scratch under a stable training setup, and to use the controller to search for the most suitable temporal/spatial hyperparameter combination for seizure prediction.

The parameters that are directly specified in `train.py` rather than tuned by the RNN controller are the following. Unless the CLI overrides them, the default values are:

| Category        | Parameter                | Default value                   | Meaning                                                      |
| --------------- | ------------------------ | ------------------------------- | ------------------------------------------------------------ |
| Dataset         | `data_root`            | `data/processed/siena_slices` | root directory of windowed EEG slices                        |
| Dataset         | `dataset_layout`       | `siena`                       | use SIENA split logic by default                             |
| Dataset         | `window`               | `win10s`                      | training windows are 10 s by default                         |
| Dataset         | `version`              | `v1`                          | use preprocessing branch `v1` by default                   |
| Reproducibility | `seed`                 | `42`                          | random seed for split and training                           |
| Loader          | `batch_size`           | `64`                          | mini-batch size                                              |
| Loader          | `num_workers`          | `0`                           | dataloader worker count                                      |
| Optimization    | `epochs`               | `8`                           | each CNN candidate is trained for 8 epochs                   |
| Optimization    | `lr`                   | `1e-4`                        | Adam learning rate for the CNN                               |
| Split           | `test_ratio`           | `0.2`                         | held-out test proportion                                     |
| Split           | `val_ratio`            | `0.2`                         | validation proportion for Kaggle layout                      |
| Sampling        | `train_sampler`        | `balanced-over`               | class balancing strategy in the training loader              |
| Loss            | `class_weight`         | `none`                        | default cross-entropy without explicit class weighting       |
| Selection       | `reward`               | `balanced`                    | validation objective used for model selection and NAS reward |
| Selection       | `far_penalty`          | `1e-3`                        | penalty coefficient in the balanced reward                   |
| NAS loop        | `nas_rounds`           | `15`                          | number of controller update rounds                           |
| NAS loop        | `nas_samples`          | `8`                           | number of sampled CNNs per round                             |
| Controller opt  | `controller_lr`        | `1e-3`                        | Adam learning rate for the RNN controller                    |
| Controller opt  | `controller_grad_clip` | `5.0`                         | gradient clipping threshold for the controller               |
| Controller opt  | `baseline_decay`       | `0.9`                         | exponential moving-average factor for the REINFORCE baseline |

Several training choices are also hard-coded in the implementation even though they are not exposed as CLI arguments. CNN candidates are optimized with Adam, the base loss is cross-entropy, predictions are generated by `argmax` over the two logits, and the validation score used for checkpoint selection is computed at every epoch. In SIENA mode, the validation split is not random: records `PN06-1` and `PN14-4` are kept as a permanent validation set, while the remaining records are split into train and test subsets. The default training sampler is `balanced-over`, which oversamples the minority class so that each training epoch is approximately class-balanced.

The default selection objective deserves special mention because it drives both local model selection and controller learning. With `reward="balanced"`, `train.py` defines the score as:

`balanced score = sensitivity - 1e-3 * FAR`

where FAR is the false-alarm rate measured as false positives per negative hour. During training of each CNN candidate, validation metrics are computed after every epoch, and the epoch with the highest selection score is kept as the representative checkpoint for that candidate. This means the optimization target is not just raw loss minimization; instead, it is explicitly biased toward high sensitivity while discouraging excessive false alarms.

The parameters that are tuned by the RNN controller are not the generic optimizer settings above, but the detailed CNN architecture hyperparameters. The search space is defined in `scripts/arch_decode.py` and contains 19 discrete decisions:

| Group          | Searched parameter | Candidate values                       |
| -------------- | ------------------ | -------------------------------------- |
| Temporal stack | `t_out_1`        | `4, 8, 16`                           |
| Temporal stack | `t_out_2`        | `8, 16, 32`                          |
| Temporal stack | `t_out_3`        | `8, 16, 32`                          |
| Temporal stack | `t_k_1`          | `4, 8, 16`                           |
| Temporal stack | `t_k_2`          | `4, 8, 16`                           |
| Temporal stack | `t_k_3`          | `4, 8, 16`                           |
| Temporal stack | `t_pool_1`       | `1, 2, 4`                            |
| Temporal stack | `t_pool_2`       | `1, 2, 4`                            |
| Temporal stack | `t_pool_3`       | `1, 2, 4`                            |
| Spatial stack  | `s_out_1`        | `8, 16, 32`                          |
| Spatial stack  | `s_out_2`        | `8, 16, 32`                          |
| Spatial stack  | `s_k_1`          | `4, 8, 16`                           |
| Spatial stack  | `s_k_2`          | `4, 8, 16`                           |
| Spatial stack  | `s_pool_1`       | `1, 2, 4`                            |
| Spatial stack  | `s_pool_2`       | `1, 2, 4`                            |
| Block option   | `use_batch_norm` | `0, 1`                               |
| Block option   | `activation`     | `0, 1` mapped to `relu`, `tanh`  |
| Block option   | `pool_type`      | `0, 1` mapped to `max`, `avg`    |
| Block option   | `conv_bias`      | `0, 1` mapped to `False`, `True` |

After sampling, these 19 tokens are decoded into the `EEGCNN` constructor arguments. So what the RNN is really tuning is the detailed instantiation of the temporal stack and spatial stack: channel widths, kernel sizes, pooling sizes, normalization on/off, activation family, pooling family, and whether convolution bias is used. The high-level anisotropic design itself remains fixed; only its discrete hyperparameterization changes.

The RNN controller in `scripts/RNN.py` is a lightweight one-layer LSTM policy network. Its default architecture is:

| Controller component  | Setting                           |
| --------------------- | --------------------------------- |
| Sequence model        | `nn.LSTM`                       |
| Hidden size           | `64`                            |
| Embedding dimension   | `32`                            |
| Number of LSTM layers | `1`                             |
| Entropy coefficient   | `0.01`                          |
| Output heads          | one linear head per decision step |

The controller works autoregressively. It starts from a learned start token, embeds the previous action, feeds that embedding into the LSTM, and at each step uses a step-specific linear head to produce logits over the allowed choices for that hyperparameter. A categorical distribution is formed from those logits, one value is sampled, and the sampled value is fed back as the next token. Repeating this process for all 19 steps produces one complete CNN architecture candidate together with its accumulated log-probability and entropy.

The controller tuning strategy is REINFORCE with entropy regularization. In each NAS round, the controller samples `8` architectures. Each sampled architecture is decoded, validated, checked with a one-batch preflight forward pass, and then trained as an independent CNN for `8` epochs using the fixed optimization settings described above. Its reward is taken from the validation performance of the best epoch, using the same balanced score by default. Invalid architectures are not silently ignored; they receive a fallback reward equal to `min(valid_rewards) - 0.1`, or `-1.0` if the whole round fails. After all rewards in the round are collected, the controller is updated with a policy-gradient loss of the form

`L = -E[log p(a) * (reward - baseline)] - entropy_coeff * E[entropy]`

where the baseline is an exponential moving average of mean round reward with decay `0.9`. Controller gradients are clipped to `5.0` before the Adam update. This setup stabilizes the reward signal, keeps exploration alive through the entropy term, and biases later rounds toward sampling architectures that previously achieved better validation sensitivity/FAR trade-offs.

At the end of the NAS loop, `train.py` retains the top two architectures ranked by validation selection score, saves their decoded JSON architecture files and weights, and evaluates them on the held-out test split. In that sense, the overall training strategy is two-stage: first tune the CNN architecture inside a fixed training protocol using the RNN controller, then report final generalization only for the best validation-ranked CNN candidates.

Finally, a practical limitation observed after training on the SIENA dataset is that the dataset is relatively small, which makes it difficult for the CNN to learn sufficiently stable and discriminative seizure-related features. For this reason, an additional Kaggle EEG dataset was introduced to expand the training data volume and improve feature learning diversity. The extended training experiments with the Kaggle supplement are still in progress.
