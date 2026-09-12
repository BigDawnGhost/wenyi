# 07 · 分离 DOCX 样式策略、Word 写入与块级组装

[总览](README.md) · [English](../../refactoring/07-docx.md)

状态：待实施；中优先级。基线：`7471256`。

## 证据

[`docx_writer.py`](../../../trans_novel/assemble/docx_writer.py) 共 737 行，556 行的 `_emit_chapter_blocks()` 为 131 行。文件混合了 OOXML 操作、字体颜色策略、列表重启、混合样式切片、双语段落、表格单元格与章节遍历。

有两条具体跨层依赖：writer 导入 reader 的私有 `_text_has_visible_list_prefix`，又从 `pipeline.docx_styles` 导入 `proportional_range_placements`。后者是纯回退计算，却位于带模型和持久化依赖的服务模块中。提取共享策略，比简单把 writer 对半拆更有价值。

## 建议结构

| 模块 | 职责 |
| --- | --- |
| `document_styles/docx.py` | 纯样式元数据类型、支持继承的原文属性、可见列表前缀识别、比例回退位置；不修改 python-docx 文档，不导入 agent、store、pipeline。 |
| `assemble/docx_styles.py` | 应用 run 和段落样式、目标字体与标题颜色策略，计算渲染切片。 |
| `assemble/docx_numbering.py` | OOXML 编号查找和列表重启。 |
| `assemble/docx_blocks.py` | 标题、普通及双语段落、表格写入，按原顺序遍历章节块并合并续段。 |
| `assemble/docx_writer.py` | 打开或新建 Word 文档，选择导出设置，调用块级写入并保存。 |
| 现有 reader、对齐服务 | 分别抽取原始元数据、进行模型对齐，共用中立样式策略。 |

新的共享模块保持 DOCX 专属，不把 HTML DOM 恢复和 Word run 样式强行统一成通用渲染抽象。先移动已有真实复用的纯逻辑，其他样式策略仍可留在 writer 层。

## 保留行为

样式范围中的粗体、斜体、下划线、颜色和字号继承自原文条目，不采纳模型任意生成的样式属性。统一样式跳过对齐；混合样式只有 target digest 匹配时才使用已有 placement，否则保留按原文范围比例映射的回退。

中文译文继续使用现有宋体策略，双语原文和未翻译原文回退不强制使用目标字体；其他目标语言保持不指定目标字体。保留原文明确设置的标题颜色、标题导航、段落对齐和底纹、当前双语顺序。

列表保留原始编号身份和重启行为，已有可见编号不能重复生成。表格维持当前基本结构支持，保留行列位置与连续表格分组；本次不新增合并单元格、嵌套表格，也不改变 DOCX 输入支持范围。

渲染辅助函数不调用模型、不修改正式章节状态，继续使用一致导出快照和一次性导出视图；不能因为移动函数位置而多做一次模型对齐。

## 切片与验收

1. 先将纯前缀判断和比例映射移到 `document_styles/docx.py`，同步更新 reader、writer、`pipeline/docx_styles.py`，不留私有导入兼容层。
2. 提取 Word 样式与编号操作，再拆块级写入；reader API、模型对齐 operation 和元数据保持现状。
3. 新增导入边界检查：`document_styles` 不依赖 pipeline，格式辅助模块不得导入 `pipeline.docx_styles`；顶层导出适配器对快照存储的依赖不属于这个禁止范围。

使用 `tests/test_docx.py`、`tests/test_metadata_language.py`、`tests/test_bilingual.py`、`tests/test_review_autofix.py`、`tests/test_assemble.py`。比较规范化 `word/document.xml`、编号、样式、关系及文本顺序，不比较 ZIP 字节。覆盖原文回退、过期 placement 哈希、混合样式、长段续段、重复列表 ID、表格单元格和两种双语顺序，包含默认路径、显式 `--out` 和并发快照。

以后新增字体选择或 DOCX 结构，可以只改一个策略或写入模块并做针对性验证。本方案不增加字体、配置项或输出语义。
