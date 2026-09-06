# P06 · 引入向量语义检索，建立可追溯的全书证据召回

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p06-semantic-evidence-retrieval.md)

类型：用户补充的未来规划，尚未实施；本文为设计建议，接口与阈值待评测确定 · 2026-09-05

## 目标与现有接入点

支持“查找表达同一件事的段落”“寻找某人的年龄/亲属线索”“找同类叙述或角色对白”，即使关键词不同也能召回。现有 `BookEvidenceIndex` 已提供稳定 ref、术语/别名命中、邻近上下文和影子来源标记，可以扩展查询能力。新增能力先服务书籍流程；SRT 保持独立轻量路径。

## 检索与判断的职责

采用精确/词法命中与向量候选召回相结合，再按实体、书籍、段落范围等过滤，必要时对少量候选重排。向量分数仅描述检索相关程度，不能直接证明两句可互换、亲属关系成立或某个历史译法正确；确认交给对应领域规则和取证判断。没有匹配也不证明书中不存在证据，应记录搜索范围及未覆盖区域。

## 索引单位与身份

保留原 `Segment.index`、章节和锚点；长段只在派生索引里切成句子或窗口，记录 source_sha256、segment_ref、源字符区间、切分版本与内容哈希，不重新编号正式章节。首期分别维护源文证据与已确认译文记忆；源文索引在正式 target 改变时可复用，译文索引按 target 版本失效。影子译文只存在于本次 Review 覆盖层，不进入共享确认索引。

## 查询返回契约

建议返回 ref、原文摘录及范围、章节/说话人候选、匹配类型、排序分数、截断/覆盖信息和证据来源。完整原段按 ref 二次读取。先限制到本书/本次会话，在有可靠实体约束时缩小候选；实体尚未确定时保留多个候选并扩大检索，避免错误过滤导致漏证。分数相同时按稳定书内顺序排序。所有引用文本作为数据注入，不执行其中的指令。

## 工程方案与恢复

先定义独立的 embedding/retrieval 接口，提供 FakeEmbedding 和小规模精确向量扫描基线；用实测规模决定是否需要近似索引或服务，不预先绑定向量数据库。领域服务持有并发和预算，Orchestrator 继续只装配。索引是可重建派生缓存：模型/版本/维度、归一化/距离算法、切分和源哈希组成身份；模型标识无法可靠锁定时显式更换代际。分批检查点、完整代际验证后原子切换指针，拒绝混用不同向量空间。密钥仅走环境变量；本地与远程 embedding 需显式配置，远程模式要明确哪些文本会发送，并纳入 P03 用量。

## 分阶段验收

第一步只在 Review 增加按预算取原文证据的工具；第二步供 P09 关系查询和 P08 风格样本检索；第三步供 P07 模糊记忆候选。用固定候选向量做离线确定性测试，并在 P02 标注集比较词法基线、向量及混合策略的 Recall@k、Precision@k、延迟、索引体积与 token 成本。包括否定、数字差异、同名人物、跨章改述、无答案和故意混淆的困难负例；阈值在开发集校准，在留出书籍上报告，不采用通用的“相似度大于 0.9 就等价”。

## 依赖、成本与研究边界

依赖 F05/F06、P01 的版本和 P02/P03 的评测/预算。只读检索原型预计 5–8 个工作日；可靠增量索引与校准约再 5–10 日，模型评测另计。句向量用于相似检索有原始研究基础，具体文献没有验证 Wenyi 的召回质量；上述混合检索、数据契约和阈值策略是本项目设计建议。[Sentence-BERT](https://aclanthology.org/D19-1410/)

## 源码与文档接入点

- [trans_novel/review/evidence.py:34](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L34)
- [trans_novel/review/evidence.py:167](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L167)
- [trans_novel/review/evidence.py:356](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L356)
- [trans_novel/agents/prompts.py:125](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/prompts.py#L125)
- [trans_novel/ingest/models.py:22](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/ingest/models.py#L22)

工程估时为单人建议，未含真实模型/embedding 服务测评和长篇人工盲评。配置落地时需同步模型、内置模板、根示例、双语文档与 CLI/配置测试；所有默认自动化测试保持离线。
