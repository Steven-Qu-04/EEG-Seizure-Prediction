# PN06-5 EDF EDA 深度人工分析报告

## 1. 文件与完整性

| 项目 | 值 | 解释 |
|---|---:|---|
| EDF | `data/raw/siena/PN06/PN06-5.edf` | 可正常读取 |
| 文件大小 | 228,171,264 bytes | 非空，规模合理 |
| 时长 | 6022.0 s（约100.4 min） | 长程记录 |
| 通道数 | 37 | EEG+EKG+SPO2+HR+辅助通道 |
| 采样率 | 512 Hz（单采样率） | 相关性分析直接同组计算 |
| 起始时间 | 2016-01-01 13:24:41 | pyedflib/mne 一致 |
| 文件级 flags | 空 | 无结构层致命错误 |

结论：文件结构完整，但信号质量问题显著（大量平线段、若干辅助通道无效）。

---

## 2. 元数据检查

- 通道名无空值/重复值。
- EEG 单位为 `uV`，量程与数字范围合法。
- EKG 振幅量级显著高于 EEG（符合生理差异）。
- EDF annotations 为空；seizure list 提供 1 段发作（`4783~4827s`, 44s），范围合法。

---

## 3. 通道质量统计解读

## 3.1 全局特征
- 多数 EEG 通道 flatline_ratio 约 `0.615`（61.5%）。
- 最长平线段约 `1345~1346s`（约22.4分钟）。
- `SPO2`、`HR` 全程 0 值（flatline=1.0）。
- `MK` 几乎全平（flatline≈0.999997）。

说明：有效动态信号占比有限，平台段较长，需做严格窗口级筛选。

### 3.2 高风险/重点通道

| 通道 | 关键指标 | 解释 |
|---|---|---|
| `SPO2` | std=0, p2p=0, flatline=1.0 | 全程无有效血氧信号 |
| `HR` | std=0, p2p=0, flatline=1.0 | 全程无有效心率信号 |
| `EKG EKG` | std=0.0716, p2p=0.1807（high_variance/extreme_p2p） | 振幅远高于EEG，存在串扰风险 |
| `2` | std=2.41e-4（high_variance） | 辅助通道噪声/高幅波动显著 |
| 全部主要EEG导联 | flatline≈0.615 | 普遍存在长平台段 |

### 3.3 缺失与饱和
- NaN/Inf 计数全为 0。
- saturation_ratio 基本 0，说明并非“顶格截断”问题。

---

## 4. PSD/频谱解读

- 大多数导联 `delta` 占主导（常见于 0.25~0.58 区间）。
- `alpha/beta` 在部分导联保留中度占比（如 P3/P4/Cp5）。
- `gamma` 普遍较低。
- 低频漂移异常通道：`Cp1`/`Pz`（约0.55~0.58）、`P9/P10`（约0.73），提示强低频主导或准DC成分。
- 工频比值总体不高，但 `Fz` 50Hz 比例偏高（~0.327）。

结论：频谱可见一定脑电节律，但低频占比和平台段影响明显，原始信号纯净度一般。

---

## 5. 通道间相关性

| 指标 | 数值 | 解释 |
|---|---:|---|
| mean_abs_corr | 0.2662 | 中等偏低，不是全局同步塌缩 |
| max_corr | 0.999984 | 存在近重复/强耦合通道 |
| min_corr | -0.2424 | 存在局部反相关系 |

高相关对：
- `EKG EKG` - `MK`: 0.999984（极高，疑似耦连/同源成分）
- `EEG P9` - `EEG P10`: 0.999519（几乎同形）
- `EEG O1` - `EKG EKG`: 0.923877（心电泄漏风险）
- `EEG Pz` 与 `P9/P10`: >0.90（后部导联共同成分显著）

---

## 6. 注释与发作事件

- EDF 内无 annotations，`annotation_distribution` 仅为缺失提示。
- seizure timeline 显示 1 次发作（4783~4827s），与 `ana.txt` 一致。
- 标签来源依赖 seizure list，建模时应单独记录标签来源字段。

---

## 7. 图像逐张人工解读

## 7.1 `raw_preview_page_01.png`
- 前24导联有波动，但幅值普遍偏小。
- `EEG Pz`、`EEG Cp1` 振幅视觉更“厚”，与统计中的较高 p2p 对应。
- `EEG Fp1` 多处尖峰/瞬变（约3.5s、10.7s），更像前额伪迹。

## 7.2 `raw_preview_page_02.png`
- 多通道近水平直线，尤其 `SPO2`、`HR`、`MK` 明显。
- 该“近空白图”与 flatline 统计一致，属于真实数据状态，不是绘图失败。

## 7.3 `channel_std_p2p.png`
- `EKG EKG` 单点远高于其他通道，主导纵轴。
- `P9/P10/Pz`、通道`2` 在 EEG 内属于较高振幅组。

## 7.4 `flatline_saturation.png`
- EEG 群体平线比集中在约 0.615；`SPO2/HR`=1.0，`MK`≈1.0。
- 饱和线接近0，排除硬截幅主导问题。

## 7.5 `psd_overview.png`
- 曲线整体“delta 高、gamma 低”，符合低频主导。
- 少数导联 alpha/beta 小抬升，说明仍有可用节律信息片段。

## 7.6 `corr_group.png`
- 单组512Hz平均绝对相关约0.266，反映“局部高相关 + 全局中等相关”结构。

## 7.7 `annotation_distribution.png`
- 仅显示无annotation提示文本，属于有效信息图（说明注释缺失）。

## 7.8 `seizure_timeline.png`
- 发作窗口清晰，长度约44s，与 seizure list 完全一致。

---

## 8. 综合结论与建模建议

## 8.1 综合结论
- 数据文件可用，但信号质量风险集中在：
  - EEG 大范围长平线（约61.5%）；
  - SPO2/HR 全无效；
  - EKG 与 MK 及部分EEG相关过高，存在串扰风险；
  - 频谱低频占比偏高。

## 8.2 建议
1. 训练前剔除 `SPO2`、`HR`，`MK` 仅保留事件参考功能。  
2. 采用窗口级 QC（flatline/低方差阈值）筛除长平台片段。  
3. 做心电伪迹抑制与高相关通道降冗余（特别是 `P9/P10`）。  
4. 标注体系中区分“EDF annotation缺失”和“seizure list存在”。

---

## 9. 图表清单

- `plots/raw_preview_page_01.png`
- `plots/raw_preview_page_02.png`
- `plots/channel_std_p2p.png`
- `plots/flatline_saturation.png`
- `plots/psd_overview.png`
- `plots/corr_group.png`
- `plots/annotation_distribution.png`
- `plots/seizure_timeline.png`
