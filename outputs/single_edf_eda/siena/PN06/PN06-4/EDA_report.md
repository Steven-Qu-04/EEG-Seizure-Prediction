# PN06-4 EDF EDA 深度人工分析报告

## 1. 文件信息与完整性

| 字段 | 值 | 解读 |
|---|---:|---|
| EDF 文件 | `data/raw/siena/PN06/PN06-4.edf` | 原始数据可读取 |
| 文件大小 | 266,172,928 bytes | 非空，体量与约2小时脑电相符 |
| 记录时长 | 7025.0 s（约117.1 min） | 长程记录 |
| 通道数 | 37 | 含EEG、EKG、SPO2、HR、MK及编号通道 |
| 采样率 | 512 Hz（单一采样率） | 相关性分析无需跨组拼接 |
| 记录起始 | 2016-01-01 11:16:09 | pyedflib/mne 一致 |
| 文件级 flags | 空 | 无结构性致命错误 |

结论：文件结构完整，可用于后续建模前的质量筛查，但信号层面存在显著质量风险（详见后文）。

---

## 2. 元数据检查（通道、单位、范围）

### 2.1 通道命名与类型
- 通道命名整体规范，无空名/重名。
- EEG 相关通道齐全（如 Fp1/F3/C3/.../P10）。
- 生理辅助通道包括 `EKG EKG`、`SPO2`、`HR`、`MK`、`1`、`2`。

### 2.2 量程与单位
- EEG 通道单位为 `uV`，physical range 大多 `[-4096, 4095.875]`，digital range `[-32768, 32767]`。
- EKG physical range 显著更大（约 ±500k uV 级别），符合心电振幅量级差异。
- 未发现 physical/digital min-max 反转等非法范围。

### 2.3 注释与事件
- EDF 内 annotation 为空。
- seizure list 检出 1 次发作：`5939s ~ 6002s`（63s），且在记录范围内。

---

## 3. 通道级统计与QC解读

## 3.1 全局现象
- 几乎全部 EEG 通道 `flatline_ratio ≈ 0.627`（约 62.7%）。
- `flatline_longest_sec` 多在约 `1590s`（26.5 分钟）级别。
- `SPO2` 与 `HR` 全程 0 值，flatline_ratio = 1.0。
- `MK` 近乎常量，flatline_ratio ≈ 0.999997。

这说明：记录中存在长时间“近静止/平台段”或采集链路抑制段，影响有效信息密度。

### 3.2 关键异常通道（按风险）

| 通道 | 关键统计 | 风险解释 |
|---|---|---|
| `SPO2` | std=0, p2p=0, flatline=1.0 | 全程无有效血氧波形，辅助生理信息不可用 |
| `HR` | std=0, p2p=0, flatline=1.0 | 全程无有效心率轨迹 |
| `EKG EKG` | std=0.0767, p2p=0.2332, high_variance/extreme_p2p | 振幅远高于EEG，且与部分通道相关偏高，需防串扰解释 |
| `2` | std=2.67e-4, high_variance | 可能为非标准导联或高噪声辅助通道 |
| 全部 EEG 主通道 | flatline≈0.627 | 大段平线显著压缩可用动态脑电 |

### 3.3 NaN/Inf 与饱和
- 所有通道 NaN/Inf = 0。
- saturation_ratio 全部接近 0，说明“裁剪饱和”不是主要问题。

---

## 4. 频谱（PSD）解读

### 4.1 频带结构
- 多数EEG通道以 `delta` 占比最高（约 0.24~0.57 不等）。
- `alpha/beta` 在部分导联有中等占比（如 P3/P4/T6 一带），但整体不突出。
- `gamma` 占比整体较低（多数 <0.03）。

### 4.2 漂移/高频噪声指标
- 多数通道 low_freq_drift_ratio 在 0.13~0.29。
- 个别通道极高：`P9/P10` low_freq_drift_ratio ~0.77，提示超低频主导（常见于基线漂移或准直流段）。
- 高频噪声比整体中低，但 `Fz/Pz/Cp6/P9/P10` 可见异常增高（部分 >0.3~0.7），提示频谱形态不典型。

### 4.3 工频污染
- 50/60Hz 比例多数较低。
- `Fz` 50Hz 比例偏高（~0.404）需关注电源耦合。

结论：频谱总体呈“低频主导 + 多导联长平台段”特征，纯净度一般，不宜直接当作稳定清洁EEG样本。

---

## 5. 通道间相关性解读

| 指标 | 数值 | 解释 |
|---|---:|---|
| mean_abs_corr | 0.2608 | 中低水平，不是全局同步塌陷 |
| max_corr | 0.9989 | 存在近重复/强耦合通道对 |
| min_corr | -0.5251 | 局部导联有显著反相关 |

高相关对：
- `EEG P9` vs `EEG P10`: 0.9989（几乎同波形，可能空间邻近+共同噪声）
- `EKG EKG` vs `MK`: 0.9882（非典型高相关，提示辅助通道耦合或标记逻辑耦连）
- `EEG O1` vs `EKG EKG`: 0.9050（需警惕心电泄漏到EEG）

结论：相关性结构并非全面异常，但存在少数“过高相关”对，建模前应做去伪迹与通道策略筛选。

---

## 6. 发作事件与注释一致性

- EDF annotations：无。
- seizure list：1段发作（5939~6002s，63s）。
- 图上发作时间轴与文本一致，范围合法。

解释：当前发作标签主要依赖外部 seizure list，而非 EDF 内嵌注释。训练/评估时要明确标签来源，避免“注释缺失=无发作”的误判。

---

## 7. 图像逐张人工解读

## 7.1 `raw_preview_page_01.png`
- 前24通道存在可见波动，但多导联波幅较小、动态偏弱。
- `EEG Pz` 视觉上较“粗厚”，与其较大 p2p 相符。
- `EEG Fp1` 在约 4s、17s、30s 附近见尖峰样瞬变，可能为伪迹/瞬时干扰。

## 7.2 `raw_preview_page_02.png`
- 大量通道几乎呈直线，仅保留坐标轴与水平轨迹。
- 该图不是“绘图失败”，而是通道在预览窗口内确实接近常量（与 flatline_ratio、std=0 的统计一致）。
- 特别是 `SPO2`、`HR` 全平。

## 7.3 `channel_std_p2p.png`
- `EKG EKG` 出现显著尖峰（std/p2p 远高于其余通道），主导纵轴尺度。
- `P9/P10/Pz/2` 处于次高层级。
- 该图反映“跨类型通道振幅不可直接横向比较”，需分组解读。

## 7.4 `flatline_saturation.png`
- 多数EEG平线比例统一在约0.63，显示系统性长平台现象。
- `SPO2/HR` 达到1.0，`MK` 近1.0，说明辅助通道信息极其有限。
- saturation 全线接近0，确认并非ADC顶格饱和导致。

## 7.5 `psd_overview.png`
- 频带曲线普遍“delta高、gamma低”。
- 少数曲线在 alpha/beta 略抬升，提示仍保留一定节律信息，但整体受低频支配明显。

## 7.6 `corr_group.png`
- 单一采样率组（512Hz）平均绝对相关约0.26。
- 与“既有局部高相关对，又非全局塌缩”结论一致。

## 7.7 `annotation_distribution.png`
- 图仅显示“No annotations found in EDF”。
- 这是数据缺失提示图，不是空图错误。

## 7.8 `seizure_timeline.png`
- 清晰显示单段发作区间约 5939~6002s。
- 与 `ana.txt` seizure list 事件一致。

---

## 8. 综合结论与建模前建议

## 8.1 综合结论
- 文件结构完整、可读性好，但信号质量存在明显风险：
  - 广泛高 flatline（EEG 约62.7%）；
  - `SPO2/HR` 全程无效；
  - 少量通道存在异常高相关与可能串扰；
  - 频谱低频主导明显。
- 发作标签可用（1段），但来源依赖 seizure list。

## 8.2 建议（执行优先级）
1. 建模时优先剔除 `SPO2`、`HR`，`MK` 仅作标记参考不作生理特征。  
2. EEG 先做分段质量门控：按窗口 flatline 比例筛掉长平台段。  
3. 对 `EKG` 泄漏风险做专项处理（ICA/回归/带阻+参考重构）。  
4. 对 `P9/P10` 等高相关通道设置冗余降维策略。  
5. 训练集标签管理中显式标记“来自 seizure list 而非 EDF annotation”。

---

## 9. 图表引用清单

- `plots/raw_preview_page_01.png`
- `plots/raw_preview_page_02.png`
- `plots/channel_std_p2p.png`
- `plots/flatline_saturation.png`
- `plots/psd_overview.png`
- `plots/corr_group.png`
- `plots/annotation_distribution.png`
- `plots/seizure_timeline.png`
