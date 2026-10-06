<div align="center">

<h1>
  <img src="../images/wenyi-emblem.png" alt="" width="280">
  <br>
  <img src="../images/wenyi-wordmark-zh.svg" alt="文译" width="180" height="54">
</h1>

**让故事跨越语言。**

面向书籍与长篇文字的桌面翻译应用，在全书语境中完成翻译。

全书理解 · 术语一致 · 取证式审校

[![Tests](https://img.shields.io/github/actions/workflow/status/BigDawnGhost/wenyi/tests.yml?style=flat-square&labelColor=00263D)](https://github.com/BigDawnGhost/wenyi/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-MIT-D4B56A?style=flat-square&labelColor=00263D)](../../LICENSE)
[![Stars](https://img.shields.io/github/stars/BigDawnGhost/wenyi?style=flat-square&labelColor=00263D&color=D4B56A)](https://github.com/BigDawnGhost/wenyi/stargazers)
[![Discord](https://img.shields.io/badge/Discord-join-D4B56A?style=flat-square&labelColor=00263D&logo=discord&logoColor=white)](https://discord.gg/sM3AQcF5D2)

[快速开始](#快速开始) · [语言支持](#语言支持) · [使用文档](#文档)

[English](../../README.md) | **简体中文**

<a href="https://hellogithub.com/repository/BigDawnGhost/wenyi" target="_blank"><img src="https://abroad.hellogithub.com/v1/widgets/recommend.svg?rid=648c0ab0997c42479027e360f604fa23&claim_uid=EkLpt1FHIqRrade&theme=small" alt="Featured｜HelloGitHub" /></a>

</div>

---

## 为什么选择文译

| 常见方案 | 文译 |
|---|---|
| 逐段翻译，彼此孤立，缺乏上下文 | 全书预扫 + 逐章梗概 + 滚动上下文 |
| 术语靠人工事后整理 | 翻译中实时抽取专有名词，自动检测译法冲突，立即影响后续批次 |
| 一次性翻译，中断即作废 | 批次检查点 + 章节状态记录，重新打开项目即可从已保存进度续跑 |
| 模型直出，无系统性质控 | 翻译 → 润色 → 取证式全书审校 |

文译为**长文本**设计 —— 长篇小说、社科专著、纪实文学……

---

## 核心特性

- **本地 Desktop 应用** — 在一个应用中完成书籍导入、翻译、校阅和导出。项目保存在独立本地工作区，发行包内置 Python 翻译引擎，无需另装 Python、PostgreSQL、Redis 或 Docker。详见 [Desktop 使用说明](desktop.md)。
- **可视化校阅** — 中英文界面、实时翻译进度、带版本记录的段落编辑，以及集中展示证据与写回结果的全书审校。截图见[界面预览](#界面预览)。
- **原生文件与凭据支持** — 拖入文件创建项目，通过系统保存对话框选择导出位置，并在系统凭据库可用时安全保存 API Key。
- **全书理解** — 翻译前预扫源文，生成逐章梗概和全书概览，注入每批翻译上下文
- **实时术语闭环** — 翻译中自动提取人名、地名、术语和固定表达；检测译法冲突并提示人工裁决
- **多阶段质量保证** — 可选润色（强档模型重译）和取证式全书 AI 审校
- **断点续跑** — 自动保存已完成批次和章节进度；重新打开应用后，可从已保存的检查点继续
- **多种 LLM 支持** — DeepSeek、OpenAI、OpenRouter、OrcaRouter、Google Gemini、Ollama、vLLM，以及通用 OpenAI 兼容端点；保留三档位入口，支持按操作独立选模型、混用连接与共享限额。配置见[模型路由](configuration.md#模型与操作路由)。
- **原生 EPUB 回填** — 基于原书 XHTML 模板替换译文片段，尽量保留原书样式、图片、目录和锚点
- **双语对照输出** — 可选原文译文对照版，原文视觉淡化，支持深色模式。

---

## 界面预览

Desktop 与 Web 共用这些工作台页面。下图展示 Web 中文界面的翻译总览，也可在设置中切换为英文。

<p align="center">
  <img src="../images/web-translation-overview.png" alt="翻译总览：查看步骤用量、缓存命中率和运行耗时。" width="960">
  <br>
  <sub>翻译总览：查看步骤用量、缓存命中率和运行耗时。</sub>
</p>

---

## 快速开始

安装 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/) 后，在仓库根目录运行：

```bash
uv sync --locked
export DEEPSEEK_API_KEY="YOUR_DEEPSEEK_API_KEY"
uv run wenyi translate book.epub
```

以上 Shell 示例使用默认 DeepSeek 提供商，请替换为自己的密钥，并在翻译前检查 `config.yaml`。Windows 命令、其他提供商及续跑／导出选项见 [CLI 使用指南](cli.md)。

想使用图形界面？下载与设置步骤见 [Desktop 快速开始](desktop.md#快速开始)。

---

## 支持格式

| 输入 | 输出 |
|---|---|
| EPUB、FB2、TXT、Markdown、HTML、PDF、DOCX | EPUB（单语 / 双语）、TXT、HTML、Markdown、DOCX |
| SRT（影视字幕） | SRT（单语 / 双语） |

- PDF 输入默认走 MinerU，首次转换需配置其 API Key，生成的 HTML 会缓存复用。可选 BabelDOC bridge 用于尽量保留版式。详见 [PDF 配置](configuration.md#流水线)。
- EPUB 输出尽量保留原书样式、图片、目录和锚点，竖排转为横排以适配中文阅读。
- SRT 项目使用轻量字幕流程，不建术语库、不做润色与全书审校。
- DOCX 走完整书籍管线：尽量保留标题导航、简易表格、列表与常见字符/段落样式；已译中文用宋体。

### 语言支持

创建项目时选择语言，支持中文 → 英文、英文 → 日文等方向。内置语言规则覆盖中、英、日、韩、法、德、西、意、葡、俄、越及部分变体。多语言互译仍属实验性功能，真实模型的长篇翻译质量还需进一步评估。

---

## 翻译流水线

文译将全书理解、分批翻译、可选润色、审校与导出串联起来。完整流程图与各阶段说明见[翻译流程](pipeline.md)。

---

## 文档

### 使用入口

- [Desktop 使用说明](desktop.md) — 安装与构建、API Key、原生导入与保存、本地数据和故障排查
- [CLI 使用指南](cli.md) — 本地安装、命令、翻译、续跑和导出
- [Web 部署](web.md) — 可选的自部署浏览器工作台，使用独立项目数据

### 通用说明

- [配置说明](configuration.md) — 模型提供商、源语言、流水线开关、切分与路径配置
- [翻译流程](pipeline.md) — 预扫、术语、上下文、润色、审校和断点续跑如何协作

### 开发与贡献

- [模块职责](architecture.md) — 模块归属、共享服务与平台边界
- [贡献指南](CONTRIBUTING.md) — 开发、测试和贡献要求

公版书翻译示例可在 [wenyi-bookcase](https://github.com/BigDawnGhost/wenyi-bookcase) 查看，也欢迎提交分享。请勿提交或分享无授权的版权文本、私人书籍或包含敏感信息的工作区数据。

---

## 当前限制

本项目为作者个人兴趣所开发，旨在为长文本书籍的译介做出一份微薄的努力。现阶段翻译质量仍受限于所选模型的能力：润色和审校阶段会显著增加 token 消耗，开启影子修订后还可能执行多次全书审校与额外 Fixer 调用；极长的书籍可能产生较大的工作区，PDF 输入默认依赖 MinerU 外部服务（首次转换需 API Key），可选 BabelDOC bridge 保留版式。SRT 字幕不建术语库、不做润色与全书审校。多语言互译仍属实验性功能；提示词指令使用英语，模型生成的说明性元数据使用翻译目标语言。

如果你发现了问题，欢迎提交 [Issue](https://github.com/BigDawnGhost/wenyi/issues)；如果你有想法，欢迎在[讨论区](https://github.com/BigDawnGhost/wenyi/discussions)提出；如果你有一定的编程能力，欢迎提交 PR，让这个项目变得更好。👏

---

## 社区

- [Discord 服务器](https://discord.gg/sM3AQcF5D2)
- QQ 群：1055065098
- [GitHub Issues](https://github.com/BigDawnGhost/wenyi/issues) — 问题反馈
- [GitHub Discussions](https://github.com/BigDawnGhost/wenyi/discussions) — 想法与讨论

---

## 支持项目

如果项目对你有帮助，欢迎打赏。

<p align="center">
  <img src="../images/tip-wechat.jpg" alt="微信收款码" width="220">
  &nbsp;&nbsp;
  <img src="../images/tip-alipay.jpg" alt="支付宝收款码" width="220">
  <br>
  <sub>微信 · 支付宝</sub>
</p>

---

## 星标历史

<a href="https://star-history.dera.page/#BigDawnGhost/wenyi&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://star-history.dera.page/svg?repos=BigDawnGhost/wenyi&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://star-history.dera.page/svg?repos=BigDawnGhost/wenyi&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://star-history.dera.page/svg?repos=BigDawnGhost/wenyi&type=date&legend=top-left" />
 </picture>
</a>

---

## 许可证

[MIT](../../LICENSE)

---

## 国内 AtomGit 托管

本项目在 AtomGit 亦有镜像：[https://atomgit.com/BigDawnGhost/wenyi](https://atomgit.com/BigDawnGhost/wenyi)
