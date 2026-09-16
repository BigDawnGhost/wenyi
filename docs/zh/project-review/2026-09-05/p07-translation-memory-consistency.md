# P07 · 建立翻译记忆，统一同义同境的重复表达

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p07-translation-memory-consistency.md)

类型：用户补充的未来规划，尚未实施；本文为设计建议，接口与阈值待评测确定 · 2026-09-05

## 目标与适用范围

让同一句反复出现的誓言、口头禅、固定叙述在语义和语境一致时采用一致译法，减少无理由漂移；保留角色、反讽、场景和修辞所需变化。现有历史索引服务术语首次译法确认，不等同于句级翻译记忆；不把所有句子塞入术语表。

## 三档处理规则

精确原文＋已确认的上下文条件一致：可选复用已确认记忆。原文相同但说话人、指代、语气或语义角色不同：给候选并重新翻译。只有向量近似：默认仅提供参考，不能自动套用整句。精确检索无需等待 P06，可先实现；规范化键与原始键分开，规范化不得删除否定、数字、名字或有意义的大小写/引号差异。

## 反例与复用边界

例如同一角色在同一誓约意义下反复说“I will return.”可以统一；“You are right.”在诚恳赞同与反讽场景下可以不同。“My brother is here.”由不同人物说出时，关系参照者不同，即使英文完全相同也必须重新检查。用记忆时依次核对实体/指代、否定/数量/时态、语用与风格范围；任何未知关键条件都降为参考。

## 记忆记录与对齐

每个条目记录原文、目标译文、所在 source/target 哈希、稳定段号和句级区间、说话人/指代候选、风格与关系版本、确认方式、适用范围及派生谱系。自动生成译文可先成为候选，不能因“最早出现”或“出现次数最多”自动升为可信标准；记录人工确认或具体审校依据。一个源句可能对应多个目标分句，源译句级对齐未确认时只给整段建议，不按字符位置硬拼译文，也不改变 Segment/EPUB/DOCX 的正式身份。

## 写入、失效与审校

新批次中受信记忆可以作为受约束输入，并在译后验证实际采用情况。对已完成段落，扫描只生成“一致性候选＋允许不同的原因”，由只读 Review 产生完整段替换，正式发布仍由启用的 Autofix 完成。润色若修改已锁定表达，应重新验证该表达或产生冲突。默认隔离到单本书；跨书复用以后单独配置风格/版权与来源范围。记忆条目撤销或改版后，通过引用关系标记受影响位置待审，不批量静默改写。

## 验收指标与实现顺序

先做精确键、人工锁定/解除和差异报告，再接上下文条件验证与 P06 模糊候选。报告“应该统一的表达中实际统一比例”和“允许差异的表达中被错误统一比例”，同时检查原意保留与文学性。固定测试包括反讽、不同说话人、多义词、否定/数字差异、局部术语变更、润色改写、句段错位和续跑；缓存/引用提交失败不会覆盖正式 target，复用命中不虚记 LLM 调用。

## 依赖、成本与取舍

先修 F01/F05；依赖 P01 版本身份、P02 质量集，上下文精化逐步接 P08/P09，模糊召回接 P06。精确记忆与审阅报告预计 5–8 日，自动条件验证与派生失效再 5–10 日。统一的目标是保留作者的重复与角色特征；若只追求字符串相同率，可能把原本合理的文学变化误判为错误。

## 源码与文档接入点

- [trans_novel/pipeline/translation.py:133](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/translation.py#L133)
- [trans_novel/pipeline/translation.py:198](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/translation.py#L198)
- [trans_novel/pipeline/translation.py:738](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/translation.py#L738)
- [trans_novel/glossary/extractor.py:35](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/glossary/extractor.py#L35)
- [trans_novel/agents/translator.py:139](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/translator.py#L139)
- [trans_novel/pipeline/review_autofix.py:721](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_autofix.py#L721)

工程估时为单人建议，未含真实模型/embedding 服务测评和长篇人工盲评。配置落地时需同步模型、内置模板、根示例、双语文档与 CLI/配置测试；所有默认自动化测试保持离线。
