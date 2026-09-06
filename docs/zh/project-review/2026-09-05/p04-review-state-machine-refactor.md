# P04 · 拆分 Review 状态机并收紧跨服务数据契约

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p04-review-state-machine-refactor.md)

类型：未来规划，尚未实施；依赖与顺序见索引 · 2026-09-05

## 当前基础与证据

Orchestrator 已是受测试约束的 façade，应继续保持。复杂度集中在领域内部：本次基线 `review_workflow.py` 1,833 行、`review_autofix.py` 898 行、`agents/review_loop.py` 968 行；`run_session()` 同时管理缓存、检查点、循环、影子版本、停止条件、结果和用量。行数本身不是缺陷，但这些状态交叉使恢复边界难以独立审查。

## 目标

让每种停止、失败和恢复转换可以独立验证，减少类似 F01/F05 的规则被分散实现。先保持 prompt、模型选择、结果排序、发布行为和费用语义不变。

## 实施步骤

定义带类型的会话状态、轮次结果、检查点和发布计划，替换跨服务边界的宽泛 dict/Any。提取纯状态转换与停止条件判定，输入旧状态和轮次结果、返回新状态和动作。I/O 适配层继续负责文件、用量和事件；Reviewer/Fixer 继续在 agents；发布服务继续与影子审校分离。按缓存身份、轮次执行、检查点恢复、结果整理逐块拆分，避免一次整体重写。

## 验收标准

对同一 FakeClient 轨迹比较重构前后最终 issues/changes、正式 target、语义事件序列和累计 usage；忽略时间戳等运行噪声。覆盖 clean 确认、Fix 上限、无进展、A→B→A、仲裁未解、单段失败和中断恢复。运行全套测试及两项架构契约，确认无下层反向导入或 Orchestrator 线程池。

## 依赖、节奏与取舍

先落地 F01/F05 等可复现行为修复，再把修复后的行为作为重构基线。建议 5–10 日拆为多个 PR，放在稳定性与质量基准之后。此项暂不改变审校质量策略；如需改 prompt 或停止语义，另走 P02 评测。

## 代码与文档依据

- [trans_novel/pipeline/review_workflow.py:690](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L690)
- [trans_novel/pipeline/review_autofix.py:111](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_autofix.py#L111)
- [trans_novel/agents/review_loop.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/review_loop.py#L1)
- [trans_novel/pipeline/orchestrator.py:186](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/orchestrator.py#L186)
- [trans_novel/review/run_store.py:29](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/run_store.py#L29)
- [tests/test_architecture_boundaries.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_architecture_boundaries.py#L1)
