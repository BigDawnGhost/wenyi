# P02 · 建立长篇质量基准，并以完整覆盖评估预扫改进

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p02-long-form-quality-evaluation.md)

类型：未来规划，尚未实施；依赖与顺序见索引 · 2026-09-05

## 当前基础与证据

已有大量 FakeClient 测试能验证流程、对齐和续跑，但通过这些测试不等于译文质量达标。`Synopsizer.digest_chapter()` 只发送 `source_text[:8000]`：长章后部没有进入该次梗概请求。README 的全书理解承诺需要覆盖指标支撑。这里将完整预扫改进纳入质量规划，不将其未经比较就替换上线。

## 目标

建立可复现的质量与成本基线，能够回答某次 prompt、术语、上下文、润色或 Review 改动究竟改善了什么，并暴露漏译、误改和上下文缺失。

## 实施步骤

先用自行编写的小型离线语料固定章节末尾关键信息、跨章代词、别名、注释、超长段和 Review 误报案例。再建立至少 50,000 words 的公版文本长篇基准，以满足英文 CONTRIBUTING 的较明确要求，并记录来源、语言、切分和内容哈希。对于长章，比较当前前缀采样与分块摘要后按顺序归并的候选方案，记录实际覆盖范围，给子摘要建立恢复检查点。

## 评测设计

记录源段完整性、术语一致性、代词/人物错误、Review 确认精度、Autofix 引入的新错误、人工盲评与每万源字符 token/耗时。固定语料、配置、模型身份、prompt 版本和抽样位置；人工审校与模型评分并列，不能把同一审校模型的 clean 作为唯一质量证明。非确定性模型需重复抽样并报告波动。

## 验收标准

新增方向分别建立留出书籍评测：P06 的检索命中与困难负例、P07 的一致性收益与错误统一、P08 的样本覆盖与角色口吻、P09 的指代/关系/长幼证据与合理弃答。按书或系列划分开发和评测数据，避免同书重复句泄漏到两个集合；自动确认率和错误率必须同时报告，避免“全部复用”或“全部 unknown”获得虚假高分。对四项能力做单独开关及组合对照，区分检索、证据和生成策略带来的收益。具体方案见总索引的 P06–P09。

公开基准说明、可重跑命令和前后对照报告；长章末尾标记在分块方案中必须进入某个摘要请求，所有块恰好覆盖且可续跑。重大语义变更提供长篇前后比较并解释质量/费用取舍，达到约定质量门槛才切换默认。

## 依赖、节奏与取舍

先修 F05/F06 保证实验身份与上下文可靠；与 P03 的预算控制配合。离线框架和语料规范约 5–8 日，真实模型评测时间及费用另计。本次仅做审查和规划，未调用付费模型或自动处理任何私有样例。完整预扫会增加调用量，必要时保留明确标识的采样模式。

## 代码与文档依据

- [trans_novel/agents/synopsis.py:20](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/synopsis.py#L20)
- [trans_novel/agents/prompts.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/prompts.py#L1)
- [trans_novel/pipeline/preparation.py:301](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L301)
- [tests/fake_llm.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/fake_llm.py#L1)
- [CONTRIBUTING.md:15](../../../../CONTRIBUTING.md#L15)
