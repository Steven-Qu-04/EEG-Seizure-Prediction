#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List


SECTION_RE = re.compile(r"^# ([A-Z_]+)\s*$", re.MULTILINE)


def parse_ana_text(path: Path) -> Dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    matches = list(SECTION_RE.finditer(text))
    out = {}
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[name] = text[start:end].strip()
    return out


def parse_json_block(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None


def count_rows(tab: str) -> int:
    lines = [x for x in tab.splitlines() if x.strip()]
    if not lines or lines[0].strip() == "<empty>":
        return 0
    return max(0, len(lines) - 1)


def build_plot_refs(plot_dir: Path, names: List[str]) -> str:
    lines = []
    for n in names:
        p = plot_dir / n
        if p.exists():
            lines.append(f"- `{n}`：![](plots/{n})")
        else:
            lines.append(f"- `{n}`：未生成（可能因数据不足或绘图依赖缺失）")
    if not lines:
        lines.append("- 无图像文件。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Generate Chinese EDA report from ana.txt")
    ap.add_argument("--ana", required=True, help="Path to ana.txt")
    ap.add_argument("--out", default=None, help="Path to EDA_report.md")
    args = ap.parse_args()

    ana = Path(args.ana)
    out = Path(args.out) if args.out else ana.parent / "EDA_report.md"
    sections = parse_ana_text(ana)

    meta = parse_json_block(sections.get("FILE_METADATA", "{}")) or {}
    flags = parse_json_block(sections.get("FILE_FLAGS", "[]")) or []
    thresholds = parse_json_block(sections.get("QC_THRESHOLDS", "{}")) or {}
    ch_flags = parse_json_block(sections.get("CHANNEL_FLAGS", "[]")) or []
    consistency = parse_json_block(sections.get("ANNOTATION_SEIZURE_CONSISTENCY", "{}")) or {}
    plots = parse_json_block(sections.get("PLOT_FILES", "[]")) or []

    ch_meta_rows = count_rows(sections.get("CHANNEL_METADATA", ""))
    ch_qc_rows = count_rows(sections.get("CHANNEL_QC_STATS", ""))
    psd_rows = count_rows(sections.get("PSD_FEATURES", ""))
    corr_rows = count_rows(sections.get("CORRELATION_SUMMARY", ""))
    ann_rows = count_rows(sections.get("ANNOTATION_EVENTS", ""))
    sez_rows = count_rows(sections.get("SEIZURE_LIST_EVENTS", ""))

    high_risk_channels = [f"{x.get('channel')}: {','.join(x.get('flags', []))}" for x in ch_flags[:30]]
    if not high_risk_channels:
        high_risk_channels = ["未发现显著高风险通道标记（以当前阈值为准）。"]

    plot_dir = ana.parent / "plots"
    report = f"""# EDF 单文件 EDA/QC 报告

## 1. 文件信息与完整性
- EDF 路径：`{meta.get("edf")}`
- 文件大小（bytes）：`{meta.get("size_bytes")}`
- 读取状态：`{meta.get("readable")}`
- 通道数：`{meta.get("n_channels")}`
- 记录时长（秒）：`{meta.get("duration_sec")}`
- 是否多采样率：`{meta.get("multi_sampling_rate")}`
- 记录类型：`{meta.get("recording_type")}`
- 电源频率：`{meta.get("line_freq")}`
- 起始时间（pyedflib）：`{meta.get("start_time_pyedflib")}`
- 起始时间（mne）：`{meta.get("start_time_mne")}`
- 文件级 flags：`{flags}`

## 2. 元数据检查解读
- 通道元数据行数：`{ch_meta_rows}`
- 已检查项：通道名空值/重复、类型推断、单位标准化、采样率一致性、physical/digital 范围有效性。
- 若 `errors.jsonl` 中存在 `reader_discrepancy`，应优先复核 EDF 头信息与读取链路。

## 3. 通道信号质量分析解读
- 参与 QC 的通道统计行数：`{ch_qc_rows}`
- 使用阈值：`{thresholds}`
- 高风险通道提示：
{chr(10).join(["- " + x for x in high_risk_channels])}
- 若某些通道在 `CHANNEL_QC_STATS` 缺失，常见原因为该通道数据异常或读取失败，具体见 `errors.jsonl`。

## 4. PSD 与频谱噪声解读
- PSD 特征行数：`{psd_rows}`
- 已输出：总功率、delta/theta/alpha/beta/gamma 功率及占比、低频漂移比例、高频噪声比例、50/60Hz 工频比例。
- 若 PSD 行数为 0，通常为有效样本长度不足或通道读取失败。

## 5. 通道间相关性解读
- 相关性分组行数：`{corr_rows}`
- 已输出：同采样率组的平均绝对相关、最大/最小相关、低相关标记，以及高相关通道对清单。
- 若相关性结果为空，通常为同采样率有效通道不足 2 个或数据质量问题。

## 6. Annotation 与 Seizure 事件解读
- Annotation 事件行数：`{ann_rows}`
- Seizure list 事件行数：`{sez_rows}`
- 一致性摘要：`{consistency}`
- 若存在越界事件（见 `annotations.csv` 的 `out_of_bounds`），建议先清洗后再做监督建模。

## 7. 高风险通道/文件提示
- 文件级风险：`{flags if flags else "无明显文件级风险标记"}`
- 通道级风险数量：`{len(ch_flags)}`
- 建议优先复核：平线比例高、饱和比例高、robust z 异常比例高的通道。

## 8. 建模前建议
1. 先基于 `channel_flags` 过滤或降权高风险通道。
2. 对多采样率数据进行统一重采样策略，并保留映射日志。
3. 对工频噪声明显的通道考虑陷波处理（50Hz/60Hz 视采集地区而定）。
4. 对 annotation 与 seizure list 不一致样本单独标注，避免标签噪声进入训练集。

## 9. 图像引用说明
以下图像来自 `plots/` 目录，缺失图像已给出原因说明：
{build_plot_refs(plot_dir, plots)}
"""

    out.write_text(report, encoding="utf-8-sig")
    print(f"Report written to: {out}")


if __name__ == "__main__":
    main()
