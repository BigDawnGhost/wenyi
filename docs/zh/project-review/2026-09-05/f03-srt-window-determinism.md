# F03 · 字幕末尾滑窗重复拥有同一字幕，结果受完成顺序影响

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f03-srt-window-determinism.md)

优先级：**P1** · 类型：已复现缺陷；尚未修复 · 2026-09-05

## 结论与触发条件

已复现。20 条字幕时生成起点为 0、10 的两个任务，两者均被标为 `is_last=True`。第一个窗口写第 1–20 条，第二个写第 16–20 条，因此末尾 5 条存在两个写入者。两个窗口对同一条给出不同译文时，最终文本取决于任务完成顺序。

## 证据与原因

`range(0, len(segment_items), step)` 在已经覆盖尾部后仍继续生成窗口；`start + BATCH_SIZE >= len(...)` 允许多个尾窗口。`as_completed()` 返回结果后立即合并，而续跑缓存按起点顺序合并。因而即使续跑没有新模型请求，缓存重放也可能改变首次产物。

## 复现结果

脚本 F03 为两个窗口分别返回 A/B，并对同一合并函数模拟两种完成顺序。第 16 条分别得到 B 和 A。这里验证的是确定性的合并反例，没有依赖线程调度概率。

## 建议修改

在生成任务时为每条字幕确定唯一归属窗口，区分只读上下文和需要提交的有效范围；覆盖尾部后停止产生多余任务。持久化稳定的窗口方案标识，确保已有批次缓存升级时不会误套用新的归属规则。稳定排序可作为补充，但不应保留重复提交语义。

## 验收标准

覆盖 1、10、15、16、19、20、21、25、26、30、31 条字幕；有效范围应恰好覆盖每条一次。使用不同窗口译文和人为反转完成顺序，首次运行、缓存续跑及中断恢复产物须逐字一致；同时保留原始序号和时间轴。

## 交付与取舍

约 1–2 日的独立修复。重叠上下文仍有助于衔接，调整的是写入归属。旧批次缓存应显式识别，不静默覆盖已经完成且经用户确认的译文。

## 源码与测试位置

- [trans_novel/srt/translate.py:105](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L105)
- [trans_novel/srt/translate.py:199](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L199)
- [trans_novel/srt/translate.py:247](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L247)
- [tests/test_srt.py:97](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_srt.py#L97)

复现：按索引运行公共脚本，查看 `F03` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
