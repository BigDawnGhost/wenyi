# 04 · 先明确共享标记处理归属，再拆 EPUB 读写

[总览](README.md) · [English](../../refactoring/04-epub-markup.md)

状态：待实施；高优先级，输出保真风险高。基线：`7471256`。

## 证据

[`epub_reader.py`](../../../trans_novel/ingest/epub_reader.py) 为 1,561 行，[`epub_writer.py`](../../../trans_novel/assemble/epub_writer.py) 为 858 行，[`html_renderer.py`](../../../trans_novel/assemble/html_renderer.py) 为 747 行。这里已经存在真实共享边界：HTML 输入调用 `annotate_epub_resource`；EPUB 导出调用该函数和私有 `_fragment_anchor_map` 重建模板；HTML 渲染也导入片段辅助函数；Translation 从 EPUB reader 导入 `strip_ruby_markers`。

Reader 同时包含 ZIP/OPF 读取、DOM 标注、行内及 ruby 处理、注释上下文收集、逻辑章节构造。延迟 import 可以避免过早加载依赖，但并没有说明共享逻辑应该归谁管理。

## 建议布局

| 模块 | 内容与使用方 |
| --- | --- |
| `markup/contracts.py` | 输入与渲染共享的注释、行内元数据键名和纯类型；序列化键不变。 |
| `markup/anchors.py` | 片段索引与确定性锚点工具，由 ingest、assemble 共用。 |
| `markup/annotations.py` | DOM 注释标记、范围识别及局部语义辅助逻辑。 |
| `markup/segments.py` | 翻译目标选择、正文与行内内容提取、资源标注；接收 markup 和资源身份，不打开整本书。 |
| `markup/ruby.py` | ruby 读音标记及去除函数，调用方无需导入 EPUB reader。 |
| `ingest/epub_package.py` | ZIP、OPF、spine、manifest 资源发现，不涉及翻译和渲染。 |
| `ingest/epub_layout.py` | 物理资源与 TOC 到逻辑章节的映射、跨资源注释上下文；沿用 `epub_chapters.py` 策略注册。 |
| `ingest/epub_reader.py` | 协调 `peek_epub_title`、`read_epub`。 |
| `assemble/epub_navigation.py` | NAV/NCX 标题回填、OPF 元数据重写。 |
| `assemble/epub_resources.py` | 从原始资源重建结构，与保存段落核对，每个物理资源只渲染一次。 |
| `assemble/html_inline.py`、`assemble/html_bilingual.py` | 分别管理行内及注释恢复、双语原文侧锚点和链接。 |
| 现有 writer、renderer | 保留格式协调，调用已提取模块。 |

`markup/` 是确定性文档处理层，可以使用 BeautifulSoup 和 ingest 的纯模型，不依赖压缩包 reader、store、pipeline、LLM 或输出 writer。调用方都单向依赖此层。本次提取不为 DOM 规则创建通用插件注册机制。

## 身份与渲染契约

- `tn{resource_index}_{segment_index}` 锚点由物理资源身份决定，逻辑章节拆分不能重新编号；保留 `Segment.index`、`anchor`、`cont`、`resource_href`、TOC entry ID 和原始 href 语义。
- 一个逻辑章节可以跨 XHTML，多个逻辑章节也可以共享同一 XHTML；按物理文件重建和写入一次，不能互相覆盖译文。
- 原始 EPUB 继续作为模板和行内布局的权威来源，不改成每个逻辑章节存一份完整 XHTML，也不放宽原文及锚点不匹配检查。
- 非 spine 的附属注释可以提供上下文，但不能变成正式章节。保留点注释、范围注释、回链排除、ruby 提示、处理指令、图片、换行和长段回并。
- 双语原文侧链接指向原文侧锚点，译文侧链接指向译文侧锚点，跨资源同样如此；先建立全书原文链接映射，再逐资源渲染。
- 原模板 EPUB、从章节新建 EPUB、从 HTML 模板新建 EPUB 保持独立导出路径，继续从一致快照读取。

## 增量实施

先用小型合成 fixture 固定物理资源标注结果，再依次移动共享常量和 ruby、锚点工具、DOM 标注与分段，保持遍历顺序完全一致。HTML 输入和两个渲染调用方改用共享层的明确接口，移除旧私有辅助函数别名。

随后移动容器读取和逻辑布局，保留已有 `epub_toc.py`、`epub_chapters.py` 的拆分；最后处理导航、资源重建及 HTML 恢复。章节划分策略和视觉行为变更不要混进同一 PR。

## 验收

使用 `tests/test_ingest.py`、`tests/test_assemble.py`、`tests/test_bilingual.py`、`tests/test_annotation_aligner.py`、`tests/test_i18n.py`。现有 fixture 包含跨 spine 注释、非 spine 注释、损坏的次级 TOC、嵌套片段、ruby、分页处理指令和跨文件双语链接。

比较段落身份、原文、元数据，章节与 TOC 映射，规范化后的 XHTML DOM、OPF/NAV/NCX，以及未改动的二进制资源。新增导入边界检查，禁止 `markup` 导入工作流或存储，禁止 writer 导入 reader 私有函数。覆盖模板及新建 EPUB、单语双语、显式 `--out`、默认路径和并发快照导出；共享层接入后跑全量测试。

收益是输入和导出共用唯一的标注实现，不是重新设计 EPUB 语义，也不承诺顺便解决所有原书排版问题。
