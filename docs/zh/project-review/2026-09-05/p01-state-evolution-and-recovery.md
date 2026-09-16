# P01 · 为持久化状态建立版本与可验证恢复工具

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p01-state-evolution-and-recovery.md)

类型：未来规划，尚未实施；依赖与顺序见索引 · 2026-09-05

## 当前基础与问题边界

书籍状态已有源 SHA-256、manifest 最后提交、原子 JSON 和多种锁，Autofix 索引也已有局部 version 字段。这些基础应保留。常规书籍 manifest 与 SRT manifest 尚缺统一的 schema 演进契约；缺源身份的旧状态目前直接拒绝，并提示删除重建。长期运行和格式元数据演进需要更可审查的恢复路径。此项是规划，不是宣称现有状态普遍损坏。

## 目标

让用户能在不调用模型、不自动删除或覆盖状态的前提下，判断状态是否完整、哪些阶段可恢复，以及升级后哪些缓存必须重算。内容身份、状态格式版本和模型语义版本分别记录，避免一个版本号承担不同职责。

## 分阶段实施

第一步，定义各持久化产物的版本、兼容范围及最小校验结构，书籍与 SRT 保持各自领域模型。第二步，新增只读 `state check` 或 `doctor`，检查 manifest/章节对应关系、源身份、待发布 Autofix、缓存依赖与账本对账。第三步，迁移默认输出到新目录，生成变更摘要与验证报告，校验后才允许显式切换；无法证明兼容时拒绝推断。

## 恢复与账本设计

P06–P09 新增的向量索引、翻译记忆、风格证据和人物关系也应纳入派生状态目录与版本注册。区分可从原文重建的缓存和不能丢失的人工裁定/引用谱系；源哈希、模型/切分、风格与关系版本分别参与对应依赖判断。诊断工具应说明哪一层失效和为何失效，避免为了重建向量而删除正式译文或人工确认。

将用量增量与其提交标识关联，使审校运行、书籍累计和恢复后的对账可解释；对写入/计费之间的中断窗口先设计故障注入测试，再决定是否需要日志式提交机制。不要把某个进程内累计快照直接当成可重复合并的事务。

## 验收标准

使用临时的旧/新格式 fixture，检查可兼容升级、未知版本、损坏章节、缺失源身份、半完成初始化及待发布索引。只读诊断对正式译文字节零修改；迁移可重入、可回退到原目录，并保留 Segment/锚点/DOCX/babeldoc_id。账本测试覆盖同一增量重复提交及写入边界中断。

## 依赖、节奏与取舍

依赖 F01、F05、F06、F09 明确发布和缓存契约。建议第二阶段交付诊断最小版（3–5 日），迁移及账本协议后续 5–10 日。多一份迁移状态需要磁盘空间；没有源文件时只报告可验证范围，不根据同名文件补造身份。

## 代码与文档依据

- [trans_novel/pipeline/runstore.py:264](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/runstore.py#L264)
- [trans_novel/pipeline/runstore.py:322](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/runstore.py#L322)
- [trans_novel/review/run_store.py:389](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/run_store.py#L389)
- [trans_novel/pipeline/review_autofix.py:680](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_autofix.py#L680)
- [trans_novel/pipeline/metrics.py:118](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/metrics.py#L118)
