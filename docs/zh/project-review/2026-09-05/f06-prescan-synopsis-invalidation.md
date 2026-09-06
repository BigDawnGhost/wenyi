# F06 · 补齐失败的章节梗概后，全书概览仍复用不完整结果

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f06-prescan-synopsis-invalidation.md)

优先级：**P2** · 类型：已复现缺陷；尚未修复 · 2026-09-05

## 结论与触发条件

已复现。一章预扫失败回退空串，其他章成功时，仍会生成并保存全书概览。后续恢复补齐失败章的梗概后，因为概览字段已经非空，不会重新归并。于是章节梗概已完整，全书概览仍缺那一章的信息，并继续被正文翻译引用。

## 证据与原因

`ensure_understanding()` 以缺少 `source_digest` 选择重试章节，但仅以 `not synopsis` 决定是否生成全书概览。这两个缓存层没有依赖身份关系。`_ask_text()` 的失败空串语义使该组合可真实发生。

## 复现结果

脚本 F06 模拟首次章节结果为 `[chapter 0, 空]`，恢复结果补出 chapter 1。章节状态已补齐，但概览归并函数总共只调用一次，概览仍为 `chapter 0|`。

## 建议修改

为概览保存按 manifest 顺序排列的章节梗概指纹、覆盖数量及完成状态。依赖变化则重新生成；明确选择“预扫未完整时阻止正文开始”或“允许降级并记录覆盖范围”的产品策略。若正文已开始，不应悄悄更新跨章恒定上下文：在运行边界冻结版本并说明后续采用哪个版本。

## 验收标准

覆盖部分失败后恢复、全失败后恢复、旧状态缺指纹、空正文章节、梗概改变和完全不变的情况；只重算失效的概览及必要章节。检查首次运行与恢复时的 prompt 实际引用内容。

## 交付与取舍

约 1–3 日。依赖失效修复会增加一次必要归并调用；质量影响来自恢复后获得更完整的全书信息。已完成译文默认保持不动，是否重译应是单独的质量评估决策。

## 源码与测试位置

- [trans_novel/pipeline/preparation.py:301](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L301)
- [trans_novel/pipeline/preparation.py:360](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L360)
- [trans_novel/agents/base.py:63](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/base.py#L63)
- [tests/test_orchestrator.py:1007](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_orchestrator.py#L1007)

复现：按索引运行公共脚本，查看 `F06` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
