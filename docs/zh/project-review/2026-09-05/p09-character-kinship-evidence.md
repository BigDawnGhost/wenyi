# P09 · 建立人物关系与指代证据，确定兄弟长幼等翻译歧义

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p09-character-kinship-evidence.md)

类型：用户补充的未来规划，尚未实施；本文为设计建议，接口与阈值待评测确定 · 2026-09-05

## 目标与关键边界

为 brother/sister、he/she、称呼与省略主语建立人物身份和原文证据链，减少中文被迫细化时的臆断。这里至少有三个独立问题：该提及指向谁、与谁是什么关系、是否存在长幼等属性证据。`brother` 单独不说明哥哥/弟弟；即使认定为亲兄弟，也不能由出场顺序、职业、名字或既有译文推断年龄。

## 有条件的结论示例

这些例句为本规划自拟，结论均以说话人和提及身份已确定为前提。

| 原文证据 | 可记录的结论 | 翻译条件 |
|---|---|---|
| “Tom is my brother,” said John. | Tom 是 John 的兄弟；长幼未知 | 不能无依据选哥哥/弟弟 |
| “Tom, my older brother,” said John. | Tom 相对 John 年长 | 当前兄弟义及人物指代明确时可用“哥哥” |
| “Tom was born two years before John.”＋独立的兄弟关系证据 | 关系成立且 Tom 年长 | 需两项证据都绑定正确人物 |
| “Tom is like a brother to me.” | 比喻性的亲近 | 不写成血缘/法律亲属事实 |
| “Brother Thomas”出现在修道院语境 | 宗教称谓候选 | 不自动登记为某人的哥哥 |

当 P09 得不到长幼证据时，保存 `unknown`；译文根据完整句意选可自然表达的名字、关系泛称或句式调整，并检查信息没有被删去。不要把“兄弟”“手足”当成任何语境下都正确的机械替换。

## 最小数据模型

独立的版本化派生关系存储即可，不要求首版引入图数据库。实体有稳定 entity_id、带位置的名字/别名/代词提及和候选指代；关系记录主体、客体、predicate、关系视角、时间/世界范围、支持/反驳 source refs、原文摘录与区间、推导步骤、判断状态和人工裁定。将 `sibling_of` 与 `older_than` 分别存储，`older_than(A,B)` 的方向不能反转。兄弟可能是亲生、同父异母、收养、比喻或称号，未知细分保留未知。

## 证据流程

预扫按稳定源位置抽取提及和候选主张；先用明确姓名/词法/邻接上下文建立小范围实体，再按需调用 P06 找跨章支持与反例。核对引用确实存在且指向同一人物、同一时间/叙事世界，再由有界推导或取证 Agent 判断 supported/uncertain/conflicted。角色对白中的主张记录说话人及可信范围，不能自动当全知叙述；同一原句被重复引用不算独立多数证据，摘要和已生成译文也不能充当新的源事实。

## 时间、悬念与冲突

明确区分读者在当前位置可知的信息与全书原文事实。默认使用全书证据帮助内部理解，但译文新增的确定性不得超过当前原段的叙事许可：后文身份揭晓不能提前写破，未知时仍保留歧义。回忆、梦境、戏中戏、冒名和不可靠叙述记录作用范围；不同人物/世界的事实不得混并。发现较强新证据时保留旧结论及引用链，将受影响段落列为待审；冲突或线索不足时不强行选边。

## 接入与发布

在翻译前向本批实际出现的人物提供精简关系证据，显示确定性和允许表达范围；源事实与 P07 记忆的可信度分开。先在 Review 提供解释性检测，再评估是否约束新批次翻译。对已完成章节只提出带证据的完整段替换，显式启用 Autofix 才发布；不修改术语库去强制把全书 `brother` 映射为单一译词。

## 验收与扩展

建立有答案/无答案/矛盾的自拟英语小故事，覆盖多兄弟姐妹、视角互换、同名、昵称、代词歧义、年龄证据缺失、收养关系、宗教称谓、比喻、撒谎、回忆和延后揭示。按任务分开衡量指代准确率、关系方向、长幼判定、引用支持率、合理弃答及无依据细化率；既要避免乱猜，也要在证据明确时正确确定，不能用全报 unknown 换安全分。以后扩展 uncle/aunt/cousin 的父系/母系和性别细分仍沿用相同证据规则。

## 依赖、成本与研究边界

首版可以只用现有词法/上下文工具：实体候选＋显式 older/younger 规则＋只读报告约 8–12 日；跨章推导、叙事范围和发布集成约再 10–15 日。依赖 P01/P02、F01/F05，P06 提升召回、P07/P08 消费证据，但不形成循环前置条件。文学指代已有面向长距离提及的标注研究；其语料可启发评测设计，并不直接解决中文长幼表达或小说悬念保护。[An Annotated Dataset of Coreference in English Literature](https://aclanthology.org/2020.lrec-1.6/)

## 源码与文档接入点

- [trans_novel/glossary/store.py:34](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/glossary/store.py#L34)
- [trans_novel/agents/analyzer.py:54](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/analyzer.py#L54)
- [trans_novel/review/evidence.py:77](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L77)
- [trans_novel/agents/prompts.py:125](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/prompts.py#L125)
- [trans_novel/pipeline/review_workflow.py:690](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L690)

工程估时为单人建议，未含真实模型/embedding 服务测评和长篇人工盲评。配置落地时需同步模型、内置模板、根示例、双语文档与 CLI/配置测试；所有默认自动化测试保持离线。
