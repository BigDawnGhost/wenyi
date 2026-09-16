# F08 · 可预期的语言识别与配置错误未统一转换为简洁 CLI 提示

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f08-cli-error-contract.md)

优先级：**P2** · 类型：基线已复现；两处症状于 2026-09-06 修复 · 2026-09-05

2026-09-06 更新：P10 实现将自动语言识别失败改为 `ValueError`，并在统一 CLI 配置加载入口捕获 YAML/配置错误；本文复现的两个用户可见症状已修复。原分析保留为审查基线；更广泛的 YAML 字典结构校验仍可进一步加强。

## 结论与触发条件

已复现。自动识别语言失败会抛 `RuntimeError`，而 prepare/translate 的局部捕获未覆盖它；非法 YAML 的 `ParserError` 也未转换为配置领域异常。正常 CLI 入口因此走未处理异常路径，违背“预期错误不打印 traceback”的约定。

## 证据与复现

脚本 F08 用 fake provider 默认空响应执行 prepare，CliRunner 返回原始 `RuntimeError`；随后用 `language: [` 的配置执行 status，返回原始 `ParserError`。这是通过测试入口确认异常未被处理，并非声称实际终端的 traceback 展示样式已逐平台验证。

## 建议修改

定义清晰的用户可处理异常层次，在配置读取、语言识别和 provider 边界转换预期错误；各命令共享呈现逻辑。YAML 错误保留文件位置与行列信息。不要用顶层 `except Exception` 把编程错误也吞掉；调试详情可另设显式选项。

## 相关配置建议

`from_dict()` 在多处直接 `.get()`，应先校验根节点和子节为映射；对未知关键配置字段、非正批次大小/超时及无效枚举给出明确错误。已有 TierConfig 的 `extra='forbid'` 可作为局部参考。配置规范化宜另分提交并检查内置模板、示例、双语配置文档及测试。

## 验收标准

覆盖 prepare/translate/review 与免凭据的 status/assemble/report：非法 YAML、列表根节点、null 子节、识别失败及模拟 provider 永久错误均返回非零码和简洁提示；没有 traceback 或完整敏感响应。未预期的编程错误仍可诊断。

## 交付与取舍

异常契约约 1–2 日，严格配置校验约 1–2 日。更严格的未知字段规则可能拒绝以前被忽略的拼写错误，需要迁移提示；不修改模型协议或重试次数。

## 源码与测试位置

- [trans_novel/pipeline/preparation.py:186](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L186)
- [trans_novel/cli.py:460](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/cli.py#L460)
- [trans_novel/cli.py:488](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/cli.py#L488)
- [trans_novel/config.py:198](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/config.py#L198)
- [trans_novel/config.py:205](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/config.py#L205)

复现：按索引运行公共脚本，查看 `F08` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
