# P05 · 将代码、格式产物与发行包验证纳入持续集成

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p05-ci-and-release-validation.md)

状态更新（2026-09-17）：部分实现。CI 已有 Ruff、Python 3.10/3.12 测试与包资源检查。保留跨格式发行验收和文档一致性门禁等后续建议；下文描述 2026-09-05 的历史基线，不能将已落地项目重复作为待办。当前验证命令见[贡献指南](../../CONTRIBUTING.md)，实际结果以相应 CI 运行为准。

## 当前基础与证据

Tests workflow 在 Ubuntu 上覆盖 Python 3.10/3.12，并使用锁文件安装；本地 pre-commit 配有 Ruff。CI 未运行 Ruff check/format，PR 测试触发只面向 dev。构建矩阵覆盖多平台，但发布安装用 `pip install . pyinstaller` 未复用锁定依赖，二进制 smoke 只执行 `--help/--version`。这些是当前配置事实，不等于构建已经失败，也未核查远端分支保护。

## 目标

让 CI 验证实际可交付能力：源码检查、离线流程、格式保真和打包后的基本工作流均有明确门槛。跨平台只运行针对性 smoke 和锁语义测试，不盲目复制全部昂贵测试。

## 实施步骤

在测试流程增加 Ruff check/format；按实际分支策略决定是否覆盖 main PR。为关键依赖与 PyInstaller 建立可复现构建约束，跨平台从相同依赖基线产生发行物。给打包后二进制提供临时配置和 fake provider，执行最小 prepare/translate/export；不要只验证 CLI 能启动。

## 产物与文档检查

扩展合成 EPUB、DOCX、SRT fixtures 的往返矩阵：锚点/注释/TOC、原始序号/时间轴、表格/运行样式、单语/双语及显式路径。可选 PDF 引擎另设带依赖的 job；真实 bridge/MinerU 集成应与默认离线 CI 分开。加入本地文档链接和中英文配置/行为一致性检查，防止出现 F05 所述缓存说明漂移。

## 验收标准

在错误依赖、遗漏打包资源、格式信息丢失或双语文档链接损坏时相应门禁能失败；正常矩阵通过。发行 smoke 不需要真实 API key，全部样例为自行生成或可再分发 fixture。保留现有发行校验和、CPU 架构检查及 macOS 签名验证。

## 依赖、节奏与取舍

后续工作按当前 CI 的覆盖缺口重新拆分与估时。Review 重构、多语言首版及 Web 已落地，不再作为未来里程碑。多语言现行行为见[配置说明](../../configuration.md)，Web 说明见[部署指南](../../web.md)。

## 代码与文档依据

- [.github/workflows/tests.yml:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/.github/workflows/tests.yml#L1)
- [.github/workflows/build.yml:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/.github/workflows/build.yml#L1)
- [.pre-commit-config.yaml:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/.pre-commit-config.yaml#L1)
- [pyproject.toml:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/pyproject.toml#L1)
- [tests/test_bilingual.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_bilingual.py#L1)
- [tests/test_docx.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_docx.py#L1)
- [tests/test_pdf_support.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_pdf_support.py#L1)
