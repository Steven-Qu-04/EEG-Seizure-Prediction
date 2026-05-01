# PN06-1 EDF 单文件 EDA/QC 综合解读报告（人工撰写）

## 0. 数据与产物来源
- EDF 文件：`data/raw/siena/PN06/PN06-1.edf`
- 记录时长：`9615 s`（约 `160.25 min`）
- 通道数：`37`
- 采样率：`512 Hz`（单一采样率）
- 事件附加文件：`Seizures-list-PN06.txt`
- 核心分析记录：`outputs/single_edf_eda/PN06-1/ana.txt`
- 表格：`outputs/single_edf_eda/PN06-1/tables/*.csv`
- 图像：`outputs/single_edf_eda/PN06-1/plots/*.png`

---

## 1. 每个操作与产生数据的详细解读

### 1.1 输入检查与读取一致性检查
执行内容：
- 检查 EDF 文件是否存在、是否可读、文件大小是否为 0。
- 用 `pyedflib` 读取 header 元信息。
- 用 `mne.io.read_raw_edf` 读取 Raw 信号。
- 比较两种 reader 的关键字段（通道数、时长、通道名），若不一致写入 `errors.jsonl` 的 `reader_discrepancy`。

结果解读：
- `FILE_FLAGS = []`：没有发现文件级硬错误（不存在、空文件、不可读）。
- `errors.jsonl` 为空：读取和流程执行中没有捕获到异常。
- `start_time_pyedflib=2016-01-01T04:21:22` 与 `start_time_mne=2016-01-01 04:21:22+00:00`，时间一致（格式不同）。
- 未见 `reader_discrepancy`：双 reader 对关键字段一致，读取链路可信。

### 1.2 元数据检查（CHANNEL_METADATA）
执行内容：
- 统计通道名、是否空名、是否重复名。
- 推断通道类型（EEG/EOG/EMG/ECG/RESP/SpO2/marker/unknown）。
- 读取并标准化单位。
- 读取每通道采样率、physical/digital 上下界，检查范围合法性。

结果解读：
- 无空名、无重名、physical/digital range 均合法。
- 所有通道采样率一致（512Hz），对后续相关性分析有利。
- 典型生理通道：
  - `EKG EKG` 被识别为 `ecg`；
  - `SPO2` 被识别为 `spo2`；
  - 其余大量 EEG 通道名称含 `EEG `前缀，当前规则下多数落入 `unknown`，这不影响统计计算，但会影响“按类型分组”语义准确性。
- `line_freq` 为 `null`：EDF 头未提供工频配置，后续只能从频谱估计 50/60Hz 能量比例。

### 1.3 通道级统计（CHANNEL_QC_STATS）
执行内容：
- 逐通道计算：均值/标准差/极值/峰峰值、分位数、IQR、MAD、NaN/Inf 比例。
- 平线检测：平线总时长、比例、最长平线。
- 饱和比例估计。
- robust z 异常比例。
- 通过分位阈值标记低方差/高方差/极端 p2p。

结果解读（重点）：
- 大多数 EEG 通道幅值在 `10^-5` 量级（单位 uV），统计量分布连续，无 NaN/Inf。
- 明显异常或特殊通道：
  - `SPO2`：`std=0`，`flatline_ratio=1.0`，全程平线。
  - `HR`：`std=0`，`flatline_ratio=1.0`，全程平线。
  - `MK`：常数信号（约 `8e-06`），`flatline_ratio=1.0`，更像标记/状态通道。
  - `EKG EKG`：`std` 和 `p2p` 显著高于 EEG，符合 ECG 振幅高于 EEG 的生理特征。
  - `2` 通道：`std` 较高，且有短平线（约 `1.05 s`），可能混合噪声/事件信息。
  - `EEG P9/P10`：`std` 与 p2p 偏大，并伴随少量平线片段（约 `1.5 s`）。
- 自动通道 flags：
  - `EKG EKG`: `high_variance`, `extreme_p2p`
  - `SPO2`, `HR`: `low_variance`, `high_flatline_ratio`
  - `2`: `high_variance`
  - `MK`: `high_flatline_ratio`

### 1.4 频谱分析（PSD_FEATURES）
执行内容：
- Welch 方法计算 PSD。
- 提取 total power、delta/theta/alpha/beta/gamma 功率及占比。
- 提取低频漂移比例、高频噪声比例、50Hz/60Hz 能量比例。

结果解读（总体）：
- 多数 EEG 通道 `delta_ratio` 在约 `0.58~0.66`，慢波成分占主导。
- `theta/alpha/beta/gamma` 依次递减，频谱形态整体一致，提示主采集链路稳定。
- 特殊频谱现象：
  - `EEG P9/P10` 的 `line_50hz_ratio` 非常高（约 `0.84~0.85`），呈现显著工频污染特征。
  - `EEG Cz/P4` 的 50Hz 比例也偏高（约 `0.09~0.15`），存在局部工频噪声。
  - `SPO2/HR` 全零，band ratio 为 NaN，属于“无有效频谱信息”而非计算失败。
  - `MK` 为常数，total power 极低，band ratio 数值缺乏实际生理意义。

### 1.5 通道间相关性（CORRELATION_SUMMARY + HIGH_CORRELATION_PAIRS）
执行内容：
- 同采样率组内计算相关性（本例仅 512Hz 一组）。
- 过滤无效方差通道后计算均值绝对相关、最大/最小相关、强相关通道对。

结果解读：
- `mean_abs_corr=0.391`：整体中等相关，符合多导 EEG 常见水平。
- `max_corr=0.997`（`P9-P10`）：双侧邻近电极强相关明显。
- 典型高相关对包括：
  - 后部相关：`P3-O1`, `O1-O2`, `O2-T6`, `P9-P10`
  - 局部邻近相关：`F7-T3`, `F7-Fc5`, `T4-Fc6`
- `min_corr=-0.559`：存在部分反相关，对双极活动或局部相位差并不罕见。

### 1.6 Annotation 与 seizure list 对齐
执行内容：
- 读取 EDF annotations 并做越界检查。
- 解析 seizure list 中当前 EDF 的发作区间，并检查是否在记录范围内。

结果解读：
- `ANNOTATION_EVENTS` 为空：该 EDF 内没有 annotations。
- seizure list 中匹配到 1 条：
  - `start=5583s`, `end=5647s`, `duration=64s`, `in_range=True`。
- 因 EDF annotations 为空，无法做“双源一致性”校验（`{}`），这属于数据侧缺失，不是流程错误。

---

## 2. 每一张图的详细解读

### 图1 `raw_preview_page_01.png`（通道 1-24，30秒）
观察：
- 多数 EEG 波形连续、振幅中等、无大段截顶或断裂。
- 可见个别瞬时尖波/缓慢漂移，但未见全屏饱和或大面积平线。
- 同一区域通道（例如后部导联）形态有一定同步，符合相关性统计。

解读：
- 主体 EEG 质量可用，至少在前 30 秒预览中未见系统性采集崩溃。

### 图2 `raw_preview_page_02.png`（通道 25-37，30秒）
观察：
- `SPO2`、`HR`、`MK`基本为水平直线。
- `EKG EKG` 可见规律性波动，振幅显著高于 EEG。
- `1`、`2` 通道有微小起伏，其中 `2` 比 `1` 波动更明显。

解读：
- 与统计结论一致：`SPO2/HR/MK` 在本记录中几乎无有效动态信息。
- `EKG` 通道“高振幅”是合理生理差异，不应按 EEG 噪声处理。

### 图3 `channel_std_p2p.png`（std 与峰峰值）
观察：
- `EKG EKG` 的红线峰值（p2p）远高于其他通道。
- `SPO2/HR/MK` 的 std 和 p2p 接近 0。
- `2`、`P9`、`P10` p2p 相对较高。

解读：
- 图形直观复现了通道 flag 结果。
- `P9/P10` 和 `2` 需要在建模前单独质量审查。

### 图4 `flatline_saturation.png`（平线比例/饱和比例）
观察：
- `SPO2`、`HR`、`MK` 的 flatline ratio = 1。
- 其余通道 flatline 接近 0。
- saturation ratio 基本为 0。

解读：
- 主要问题是“无动态信号”，不是“幅值饱和削顶”。
- 建议把 `SPO2/HR/MK` 作为非 EEG 辅助状态源，或直接从 EEG 建模特征中排除。

### 图5 `psd_overview.png`（前20通道 band ratio 折线）
观察：
- 绝大多数曲线形态近似：`delta` 最高，随后快速下降到 `theta/alpha/beta/gamma`。
- 曲线间离散度有限，提示前 20 通道的谱结构一致性尚可。

解读：
- 无全局性谱异常，但该图不含 `P9/P10`，因此无法反映它们严重 50Hz 污染问题（需结合表格）。

### 图6 `corr_group.png`（采样率组平均绝对相关）
观察：
- 512Hz 组的均值绝对相关约 0.39，单柱清晰可见，不是空图。

解读：
- 与多导 EEG 的中等耦合特征一致，说明通道之间存在可利用的空间关联信息。

### 图7 `annotation_distribution.png`
观察：
- 显示文字 “No annotations found in EDF”。

解读：
- 这是一张“信息占位图”，目的是明确数据侧缺失，而非绘图失败。

### 图8 `seizure_timeline.png`
观察：
- 在 `5583~5647 s` 有一段发作时间条。

解读：
- 与 seizure list 完全一致，且在总时长 9615 秒范围内。
- 后续如果做发作检测，可将该区段用于正样本定位。

---

## 3. 关键风险与可用性判断

高风险/高关注通道：
- `SPO2`, `HR`, `MK`：全程平线，作为时序特征基本不可用。
- `P9`, `P10`：工频污染极重（50Hz 比例约 0.84~0.85），对频域建模干扰很大。
- `2`：高方差且有短平线，需确认通道语义（EEG导联还是辅助导联）。

总体可用性：
- 主体 EEG 通道（除上述异常/特殊通道）质量总体可接受。
- 文件级完整性与读取一致性良好，可进入建模前清洗阶段。

---

## 4. 综合总结与建模前建议

### 综合结论
- 该 EDF 文件结构完整、读取稳定、绝大多数 EEG 通道统计与频谱表现正常。
- 数据中的主要问题不是“整体损坏”，而是“局部通道质量差异显著”：
  - 三个几乎常数通道（`SPO2/HR/MK`）；
  - 两个工频严重污染通道（`P9/P10`）；
  - 一条波动偏高的可疑辅助通道（`2`）。
- seizure list 提供了可靠发作区间（64s），可用于监督任务标注锚点。

### 建议执行顺序
1. 先做通道白名单：保留主要 EEG 导联，剔除或降权 `SPO2/HR/MK` 与可疑 `2`。  
2. 对 `P9/P10` 先尝试陷波 + 复评；若 50Hz 残留仍高，考虑剔除。  
3. 以 seizure 区间（5583~5647s）为中心做片段化抽样，构造正负样本。  
4. 在进入模型前，追加一次“去噪后 QC”并复算 PSD/相关性，确认预处理收益。  

