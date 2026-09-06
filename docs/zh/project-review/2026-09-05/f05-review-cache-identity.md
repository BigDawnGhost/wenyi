# F05 · Review 缓存身份遗漏模型和术语证据字段

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f05-review-cache-identity.md)

优先级：**P2** · 类型：已复现缺陷；尚未修复 · 2026-09-05

## 结论与触发条件

已复现。完成 Review 后更换初审实际使用的 cheap 档模型，或修改术语的性别、别名等证据字段，仍可直接返回上次结果，模型零调用。风险是用户以为新模型或新证据已重新审校，但返回的是旧结果；未完成 Review 的缓存身份也使用这组不完整指纹。取证/Fixer 使用的档位模型同样应纳入身份。

## 证据与原因

配置快照只保存部分 `pipeline.review_*`，不包含实际 provider、档位模型、options、提示词版本、分析/梗概身份等输入。术语指纹仅包含 source/target/type，而 `BookEvidenceIndex._term_evidence()` 明确读取 gender、aliases、note、reading 等字段。运行指标中已有较完整的配置身份，但没有用于 Review 缓存。

## 复现结果

脚本 F05 先完成一次单段 Review，并确认初审调用 cheap 档且 prompt 包含术语性别。之后分别执行仅换模型、仅改术语性别/别名的两次审校。两种配置快照与术语指纹仍相等，两种变更都复用同一运行目录且没有新调用。

## 建议修改

建立显式、带版本的 Review 输入身份：实际解析后的模型与非敏感 options、影响语义的分块/范围配置、prompt 版本、源译文身份、章节梗概/风格/全书概览及实际可见的术语证据。调度并发等不影响语义的参数可与内容身份分开。增加强制重新审校选项，并显示复用原因。

## 验收标准

分别改变模型、provider、options、prompt 版本、术语别名/性别/注释、analysis 与章节 digest，应拒绝旧结果和不兼容检查点；只改变不影响语义的运行参数可按明确规则复用。旧缓存缺新指纹时应视为不兼容。不要将真实密钥写入指纹原始数据或日志。

## 交付与取舍

约 2–4 日；新身份会让部分旧缓存失效并增加首次重审费用。同步中英文文档：当前 README/configuration 宣称“每次从头审校”，与已存在的 skip/resume 实现不一致；文案应说明实际策略和强制重跑方式。

## 源码与测试位置

- [trans_novel/pipeline/review_workflow.py:271](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L271)
- [trans_novel/pipeline/review_workflow.py:292](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L292)
- [trans_novel/pipeline/review_workflow.py:690](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L690)
- [trans_novel/review/evidence.py:265](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L265)
- [trans_novel/pipeline/metrics.py:118](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/metrics.py#L118)
- [tests/test_orchestrator.py:1367](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_orchestrator.py#L1367)

复现：按索引运行公共脚本，查看 `F05` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
