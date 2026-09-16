# P10 · 多语言互译与提示词国际化

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p10-multilingual-internationalization.md)

2026-09-06：实验版已补齐默认英语 CLI、英语指令与注释，以及显式的目标语言元数据约束。真实模型质量评测和可切换的界面语言仍待推进。

## 第一版如何使用

在已有模型配置中设置源语言和目标语言。以中译英为例：

```yaml
language:
  source: zh
  target: en
```

```bash
uv run trans-novel languages
uv run trans-novel --config config.yaml translate book.epub --bilingual
```

第一条命令列出内置语言，不需要 API Key；第二条使用已有 provider 直接中译英，生成 `output/book.en.epub` 和 `output/book.en-bi.epub`。日译英改为 `source: ja`、`target: en`，英译日改为 `source: en`、`target: ja`。每次选择一个方向，以相应语言的文件为输入，不经过中文中转。字幕仍由同一 `translate` 命令分流到独立轻量流程。

内置语言：`zh`、`zh-Hant`、`en`、`ja`、`ko`、`fr`、`de`、`es`、`it`、`pt`、`ru`，以及 `en-US`、`en-GB`、`pt-BR`、`pt-PT` 地区变体。这里的“内置”表示规则、配置与流程可用，不表示每个语言对都已通过真实模型质量验收。

`source: auto` 保留模型识别；目标不支持 `auto`。未知代码明确报错，不再截断为两个字母。`zh-Hans` / `zh-CN` 按语言别名映射为 `zh`，`zh-TW` 映射为 `zh-Hant`，`ja-JP` / `ko-KR` 分别映射为 `ja` / `ko`。这些是产品别名，完整支持的标签以注册表为准；首版不是任意 BCP 47 标签解析器。文字体系和地区可以作为语言标签的一部分；标签标准本身不等于产品具备相应翻译能力。[RFC 5646](https://www.rfc-editor.org/info/rfc5646/)

## 当前实现与原有问题

原流程虽然已有 `source_lang` / `target_lang`，正文、润色、标题、术语抽取、章节梗概和全书概览仍含中文目标指令。第一版将这些任务改为按目标语言渲染，敬称也按语言对选择。英文、日文、繁体等目标不再收到简体中文全角标点转换要求。

所有模型生成的说明性元数据（包括每条术语 `note`、风格指南、人物描述及说明中的人物称呼）均明确要求使用目标语言。人物和术语 `target` 保存翻译或音译后的姓名、译名；`source` 和 `aliases` 保留原文拼写用于匹配，证据引用也可包含原文。JSON 键、Review 动作和段落身份保持不变；类型和性别使用英语标识符，不再转换旧中文枚举。模型未遵守字符串协议、返回风格指南列表时，仍会保留其中的文字条目。

CLI 的帮助、进度、表格和错误提示统一使用英语，代码注释、docstring、配置注释及提示词指令也使用英语。指令书写语言不会限定译文语言：模型生成的说明仍遵循 `language.target`，生成的默认配置仍选择 `zh`。

角色规则移除了“仅凭姓名常识或第一人称确定性别”的默认建议，要求结合原文证据；英语 `brother/sister` 不自动确定长幼。这只是提示词约束，尚未实现 P09 的人物关系证据库，也不能保证模型从不猜测。语义更保守的代价是可能保留更多未决指代，需要后续取证和审校。

## 统一资源目录

以下目录已建立，全部资源随 Python 包发布：

```text
trans_novel/i18n/
  __init__.py
  resources.py                 # 包资源读取及提示词内容指纹
  languages.py                 # 注册、别名、规则组合、续跑语言校验
  prompts.py                   # string.Template 单次严格渲染
  metadata.py                  # 当前英语元数据格式归一化
  data/
    tasks/*.txt                # 正文、标题、分析、Review、字幕等任务模板
    languages/registry.json    # 支持代码与显式别名
    languages/zh.json          # 各语言：源文理解、目标表达、术语和标点
    languages/en.json
    languages/ja.json
    languages/zh-Hant.json     # 显式继承与覆盖
    languages/…
    pairs/registry.json
    pairs/ja__zh.json          # 日译中专属敬称例子
    shared/honorific.json      # 通用敬称策略
    shared/guidance.json       # 通用证据约束
    shared/metadata_guidance.txt # 输出语言与原文姓名约束
    shared/review_evidence_tools.txt
    export/about.zh.xhtml
    export/about.en.xhtml
```

`agents/prompts.py` 仅负责格式化术语、注释和段落数据；调用方直接使用 `i18n.prompts` 和 `i18n.languages`，旧 `agents/langprofile.py` 与 `pipeline/language.py` 入口已移除。Agent、SRT 和 CLI 读取同一套顶层纯语言服务；`i18n` 不导入 Pipeline、RunStore、Agent 或 provider。Orchestrator 的服务装配与职责没有改变。

渲染按“任务协议 + 源语言理解 + 目标语言表达 + 少量语言对差异”组合。新增语言通常添加一个 profile 和注册项，无需为全部语言对复制完整提示词。地区变体仅显式继承一个基础 profile；不自动把繁体目标降级为简体目标。

目标 profile 包含 `label`、`english_name`、`source_guidance`、`target_guidance`、`term_guidance`、`punctuation_rule`、`title_rule`、`digest_length` 和 `synopsis_length`。新增资源应同步补测试及中英文支持列表。资源仅提供数据，不从配置导入任意 Python 函数；机械简体标点转换仍由原 `postprocess` 纯函数负责。

模板变量缺失立即报错；正文中的 `$` 和 JSON 花括号只作为一次替换的值，不会递归执行模板。任务 JSON 协议及原有注释引用约束仍由代码与测试校验。资源经 `importlib.resources` 加载，不依赖当前工作目录；Python 的包资源接口也适用于资源不以普通文件目录存在的情况。[Python 3.10 importlib.resources](https://docs.python.org/3.10/library/importlib.html#module-importlib.resources)

## 状态隔离

所有目标（包括默认 `zh`）统一使用 `state/<slug>/targets/<目标语言>/`。字幕对应 `state/srt/<slug>/targets/<目标语言>/`。每个目标独立保存章节、术语、分析、上下文、Review、账本及其锁作用域；字幕仍不建立术语或 Review。

不再查找或迁移旧版 `state/<slug>/` 根目录状态；使用当前配置重新开始翻译，原文件保持不动。保存的 manifest 必须包含源语言和目标语言。完整 `source_sha256` 校验仍执行，同名但不同内容不能复用目标状态。

所有状态类命令按配置中的目标定位。显式源语言与保存值不同则拒绝续跑，`auto` 可恢复保存的实际源语言。切换目标不再被 manifest 静默改回旧目标；反向翻译是独立运行，不在一个目录中混用多个正式 `target`。

初始化保持派生状态先落盘、manifest 最后提交。已有完成批次继续跳过；更新资源不会自动重译旧的完成段落、已保存分析或术语备注。新初始化的 manifest 记录资源指纹，激活事件记录本次资源版本，Review 缓存身份加入源/目标语言、敬称策略与资源指纹。若中断前后使用不同资源，旧完成译文和新请求可能来自不同规则版本；质量比较应使用独立 `paths.state_dir`，不能把续跑当作全书重译。

## 导出范围

默认名称为 `<stem>.<目标语言>.<扩展名>`，双语为 `<stem>.<目标语言>-bi.<扩展名>`；默认简体目标的 `.zh.*` 名称不变。显式 `--out` 保持原约定，双语路径仍从显式名称派生 `-bi`。同一目标的不同风格版本仍可能共享默认输出名称，需要指定独立 `--out`。

EPUB/HTML 元数据使用目标语言。DOCX 保持现有样式策略：中文目标使用宋体，其它目标不强制改成中文字体。简体标点后处理同时在 Runtime 和导出视图校验目标语言，避免直接调用导出 API 时误改英文或繁体标点。导出仅改内存副本，正式译文与注释/样式身份保持原有边界。

EPUB 说明页的中文版本已移入资源目录；非简体目标暂用明确标记为英语的英文说明页，可用 `output.about_page: false` 关闭。首版未实现每个语言的界面或说明页翻译，也未增加阿拉伯语、希伯来语等 RTL 目标。PDF 字体、排版和外部 BabelDOC bridge 的语言能力仍需按格式验证，MIT 包没有新增 AGPL 依赖。

## 验证与质量限制

新增离线测试覆盖：原四个失败反例；中/英/日六个直接互译方向的分析、梗概、翻译、润色、术语与 Review；TXT/Markdown/HTML/EPUB/DOCX 单双语输出；显式路径；不同目标的状态与字幕隔离；繁体字幕；已完成跳过和中断后恢复；语言资源变化使 Review 重做；未知语言和损坏 YAML 的 CLI 错误；资源渲染与语言列表。

所有模型响应为 FakeClient，自建文本位于临时目录。新提示词尚未使用至少 50,000 字公版长篇完成真实模型前后比较；按 CONTRIBUTING，这仍是正式质量验收前的必要工作。不同语言的表达、字体、超长续段间距及原有基于字符比例的长度告警，需要后续语言专项评测。不能用离线协议通过或词法上的目标语言名称正确，推导真实互译质量合格。

PyInstaller 构建已添加语言资源收集，并在发行 smoke 中运行 `languages`。本地全量离线测试为 **611 passed, 44 subtests passed**（多语言与元数据语言测试共 49 项），Ruff check/format 与差异空白检查通过。只复制产品源码到临时目录构建 wheel/sdist，54 份资源与源码逐字节一致；从 wheel 在另一工作目录执行 `languages` 通过。包验证使用临时版本 `0.0.0`，不是正式发行包。跨平台二进制和真实 PDF bridge 未在这里宣称验收。

## 后续分阶段方案

| 阶段 | 范围 | 验收门槛 |
|---|---|---|
| I1：当前实验版 | 纯语言服务、任务外置、直接互译、目标状态隔离、命名、英语默认文案、元数据约束 | 本文离线回归、架构检查、包资源检查 |
| I2：核心语言质量 | 中/英/日六方向公版长篇；繁体/地区用法；长句、标题、指代、分段间距 | P02 盲评；每个方向独立报告，未通过的标记实验性 |
| I3：提示词和界面 locale | 分离提示词书写语言、分析说明语言、CLI 界面语言、导出说明页语言 | 同一目标切换界面不影响语义缓存；提示词语义变化可追溯；界面有明确回退 |
| I4：更多语言与排版 | 各语言母语审校、RTL、字体/字形和格式能力矩阵 | 按语言和格式逐项认证；不以一个 CSS `direction` 宣称 RTL 完成 |

I3 可在 `data/instructions/<locale>/` 管理任务指令，在语言 profile 下分 locale 管理语言规则，在 `data/ui/` 和 `data/export/` 管理展示文本。现在不暴露尚未实现的 `ui_locale`、`prompt_locale` 或多目标并发配置。界面资源可以采用语言协商回退；目标语言规则缺失应拒绝，而不能静默改变用户要翻译成的语言。语言标签匹配标准提供协商机制，具体翻译支持策略仍由项目定义。[RFC 4647](https://datatracker.ietf.org/doc/html/rfc4647)

与 P06–P09 联动：向量模型须评测相应语言召回；翻译记忆必须按语言方向和风格范围隔离；风格观察与目标语言表达规则分开；人物关系证据保留与目标无关的实体/方向信息，再按目标语言决定是否必须表达长幼、性别或敬语。它们仍是独立规划，没有随本版一并实现。

## 主要实现入口

- [语言规则与验证](../../../../trans_novel/i18n/languages.py)、[模板渲染](../../../../trans_novel/i18n/prompts.py)、[资源目录](../../../../trans_novel/i18n/data/)。
- [配置](../../../../trans_novel/config.py)、[状态定位](../../../../trans_novel/pipeline/runstore.py)、[准备服务](../../../../trans_novel/pipeline/preparation.py)。
- [多语言回归测试](../../../../tests/test_i18n.py)、[元数据语言约束](../../../../tests/test_metadata_language.py)、[使用指南](../../usage.md)、[配置说明](../../configuration.md)。
