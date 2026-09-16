# P08 · 以分层采样和原文证据增强风格分析

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p08-evidence-backed-style-analysis.md)

类型：用户补充的未来规划，尚未实施；本文为设计建议，接口与阈值待评测确定 · 2026-09-05

## 当前基础与目标

风格分析已有 genre/tone/narration/pacing/register/dialogue_style/rhetoric 等维度，也已做开头/中部/结尾样章采样。现有样本主要来自这些章的局部片段，分析结果经类型清洗后形成全局简报，尚未给每个判断保存源文依据、覆盖范围与反例。目标是减少以少数样章代表整本书、用空泛形容词压平角色差异的情况。

## 分层样本方案

先从源文确定性地抽取按章段位置分布的候选，覆盖叙述、对白、书信/日记、不同视角及有足够材料的角色，保留原文 ref、区间和采样原因。无法可靠识别说话人时标为未知；嵌入可帮助发现未覆盖的风格群，但 P06 不应成为首版依赖。语言检测继续独立使用纯源文，不能混入分析标签。通过局部分析再归并，避免只把更多正文塞进一次请求。

## 分析结果契约

将“原文观察”与“中文翻译策略”分开。每个维度保存范围（全书/章节/叙述者/角色）、支持和反例 ref、覆盖信息、判断状态（supported/uncertain/conflicted）及理由。策略写成可执行的译法指导和短例，而不是笼统的“优美、自然”；事实关系交给 P09，不能把风格模型对角色的猜测直接当作确定人物属性。

## 如何形成稳健结论

先汇总各类源文的共同点，再保留局部差异及相互矛盾的样本；样本不足的维度保留空或不确定，不强填完整画像。重复分析用于测稳定性，但多个模型相同结论不能替代独立源文证据。对本次拟用模型在 P02 留出样本上验证建议是否成立；不要用该模型已润色的译文反向证明原作风格。

## 接入与版本

初始派生分析仍遵守 manifest 最后提交的初始化顺序；后续预扫补样更新为独立版本，在开始正文翻译前检查需要的风格准备状态并冻结快照。Translator、Polisher 和 Fixer 接收同一版本的书级指南及局部覆盖；运行中发现新角色或风格反例时记录待修订项，在阶段边界显式更新，不在每批随意漂移。新版本影响 F05/P07 的缓存，已完成 target 默认不变。

## 验收与质量取舍

建立单章、混合视角、多个角色、讽刺对白、古今语域切换、书信插入、稀有风格和证据冲突 fixtures。固定输入和采样版本应得到相同样本；伪造/越界 evidence ref 必须拒绝，失败样本可续跑。比较旧版与新版的证据覆盖、无依据建议比例、跨样本稳定性和人物口吻辨识度；盲评须单独检查是否过度统一或增添原文没有的文采。

## 依赖、成本与参考

依赖 P02、F05/F06，与 P01/P03 的快照和预算配合。分层采样/证据字段约 5–8 日，多层指南及质量评测管线再 5–10 日。长上下文位置敏感的研究提示要测试样本位置效应，但不证明当前 Wenyi 模型一定存在相同程度的问题；本方案是基于仓库的设计建议。[Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/)

## 源码与文档接入点

- [trans_novel/pipeline/preparation.py:274](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L274)
- [trans_novel/agents/analyzer.py:26](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/analyzer.py#L26)
- [trans_novel/agents/analyzer.py:89](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/analyzer.py#L89)
- [trans_novel/agents/prompts.py:39](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/prompts.py#L39)
- [tests/test_preparation.py:13](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_preparation.py#L13)
- [trans_novel/pipeline/preparation.py:301](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L301)

工程估时为单人建议，未含真实模型/embedding 服务测评和长篇人工盲评。配置落地时需同步模型、内置模板、根示例、双语文档与 CLI/配置测试；所有默认自动化测试保持离线。
