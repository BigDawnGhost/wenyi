# F01 · 关闭 Autofix 时，中断恢复仍会发布正式译文

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f01-autofix-disable-on-resume.md)

优先级：**P1** · 类型：已复现缺陷；尚未修复 · 2026-09-05

## 结论与触发条件

已复现。先开启 Autofix，在 `autofix/index.json` 写为 `applying` 后中断；随后用 `review --no-autofix` 或 `pipeline.review_autofix: false` 继续，仍可能写回正式章节。此问题需要存在待发布索引；普通首次只读 Review 不受这条路径影响。

## 证据与原因

`Orchestrator._run_review_locked()` 在检查 `review_autofix` 前无条件调用 `resume_pending()`；后者发现 `applying` 就进入 `_apply_index()`，也没有开关检查。已有 `test_pending_index_resumes_without_model_calls` 覆盖开启时恢复，没有覆盖恢复时关闭开关。

## 复现结果

公共复现脚本 F01 先使用真实规划逻辑生成索引，在发布入口注入中断，再创建关闭 Autofix 的 Orchestrator。正式 `target` 从“旧译”变为“新译”。数据全部来自临时目录，未调用模型服务。

## 建议修改

在编排层选择恢复发布路径前检查开关，并在发布服务入口保留同样的约束，防止直接调用绕过。关闭时保留待发布索引，正常返回只读审校结果或清晰标明仍有未完成发布；不得自动删除索引、回滚已发布内容或继续发布剩余内容。

## 验收标准

补充中断前零章节发布、部分章节已发布、恢复时开关为 false/true 的组合测试。false 时正式章节字节不变；true 时幂等完成剩余发布，已完成位置不重复调用模型、不重复累计用量。运行 Autofix、Orchestrator 契约和架构边界测试。

## 交付与取舍

建议独立修复 PR，预计 1–2 个工作日（含回归）。优先恢复开关语义，不调整 Autofix 默认值或影子审校算法。已发布章节的历史修改仍然存在，应让用户看到发布进度。

## 源码与测试位置

- [trans_novel/pipeline/orchestrator.py:186](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/orchestrator.py#L186)
- [trans_novel/pipeline/review_autofix.py:76](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_autofix.py#L76)
- [tests/test_review_autofix.py:318](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_review_autofix.py#L318)

复现：按索引运行公共脚本，查看 `F01` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
