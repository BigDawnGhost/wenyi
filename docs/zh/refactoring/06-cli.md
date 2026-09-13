# 06 · 拆分 CLI 展示、命令组与本次调用上下文

[总览](README.md) · [English](../../refactoring/06-cli.md)

状态：已实施；基线：`7471256`。

已实施：应用构建、提前初始化、调用上下文、输入校验及工作流/检查/术语命令的显式注册分别独立。CLI 入口为 43 行，并沿用现有模型命令注册器。测试覆盖配置与参数隔离、提前显示帮助、早期参数错误和实际安装入口；已移除模块全局配置选择。

## 证据

[`cli.py`](../../../trans_novel/cli.py) 共 953 行，集中了配置提前创建、可变 `_CONFIG` 选择、Rich 进度条、用量和计时展示、输入输出校验、字幕分流、工作流命令、状态和术语命令。最长函数只有 110 行，主要问题是多个独立变化方向挤在一起，不是某个特别复杂的算法。

`model_commands.py` 已经采用显式注册。沿用这个模式即可，不新增 entry-point 插件、自动扫描模块或第二套命令注册机制。

## 建议结构

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 创建、注册应用，暴露当前 `main` 入口。 |
| `commands/bootstrap.py` | Windows 流设置、提前识别配置路径、在 help 和参数错误前创建默认配置、根命令组及版本处理。 |
| `commands/context.py` | 本次调用的 `CommandContext`：配置路径、预检策略、console；明确命令覆盖参数后加载和校验配置。 |
| `commands/progress.py` | Rich 列、标题截断、一次显示一个阶段的进度桥。 |
| `commands/presentation.py` | 用量、计时、完成信息及 Review 摘要。 |
| `commands/validation.py` | 文件、格式、后端等共用检查，以及明确的可预期错误转换。 |
| `commands/workflows.py` | 注册 translate、prepare、review，SRT 仍分流到轻量路径。 |
| `commands/inspection.py`、`commands/glossary.py` | 分别注册 status/report/assemble 和术语操作。 |

第一阶段保留现有独立的 `model_commands.py` 注册器。各注册器接收 app 和有类型的上下文访问函数，不能为获取全局变量反向导入 `cli.py`。保留 `trans-novel = trans_novel.cli:main` 作为实际应用入口，不把它变成兼容转发文件。

## 上下文与错误

用 Typer/Click 的本次调用上下文承担 `_CONFIG` 现有职责。根回调之前仍要提前确定配置路径；如果把初始化全部搬到回调中，会破坏 help 和早期参数错误时的行为。可以用应用工厂让测试建立独立实例，同时复用相同根初始化逻辑。

每次调用独立加载配置，应用命令覆盖参数，再校验实际可达的模型 operation。本地命令和 help 不要求 API Key。保留明确的 `typer.Exit`、取消处理和不同命令的退出码，不用吞掉编程错误的统一大异常装饰器替代现有处理。

进度条渲染属于 CLI，持久化计时属于 `timing.py` 和 Runtime/store 范围。移动进度桥时不能在阶段切换处重建总体时钟，也不能增加额外的持久化计时器；保留最终时长摘要和 `status` 查询。

## 切片与验收

1. 先提取进度条和展示，第一份 PR 暂时保持命令及调用上下文位置不变。
2. 提取公共校验，再显式注册术语和检查命令组。
3. 最后移动工作流命令，将全局配置选择替换为本次调用上下文；内部导入与测试 patch 位置同步更新，不保留私有别名。

运行 `tests/test_cli.py`、`tests/test_model_commands.py`、`tests/test_config.py`、`tests/test_timing.py`、`tests/test_srt.py`、`tests/test_bilingual.py`、`tests/test_docx.py` 和 `tests/test_pdf_export_defaults.py`。保持 help 的命令与参数、启动创建配置、本地命令免密钥、退出码、单阶段进度条、总体计时、BabelDOC 默认 PDF 和显式导出覆盖行为。

新增用两个不同配置文件连续调用的测试，第二次不能继承第一次的路径、开关或跳过预检状态；同时检查 `app` 调用和配置中的 CLI 入口。约束依赖方向为 `commands → 领域入口`，禁止 `commands → cli`。

入口文件应能很快读出应用装配结构，具体压缩到多少行不作为验收条件。各语言模型提示词仍放在现有 i18n 资源下，不搬进命令模块。
