"""简体中文命令行界面消息。"""

MESSAGES: dict[str, str] = {
    "app.help": "面向长篇小说的多语言翻译工作流。",
    "glossary.help": "查看术语、检查冲突并裁定固定译名。",
    "panel.workflow": "主要流程",
    "panel.quality": "质量检查",
    "panel.output": "状态与输出",
    "panel.glossary": "术语库",
    "panel.options": "选项",
    "panel.arguments": "参数",
    "panel.commands": "命令",
    "option.config": "配置文件路径；文件不存在时自动创建",
    "option.translate.input": "待翻译书籍（EPUB / FB2 / TXT / Markdown / HTML / PDF）",
    "option.translate.chapter": (
        "仅翻译并保存指定章节（从 0 起）；不执行审校、QA、报告和导出"
    ),
    "option.translate.format": "最终导出格式：epub / txt / html / markdown",
    "option.output": "单语版输出路径；默认写入源文件旁的 output 目录",
    "option.polish": "覆盖 pipeline.polish，控制翻译后是否润色",
    "option.review": "覆盖 pipeline.review，控制全书翻译后是否执行最终审校",
    "option.qa": "覆盖 pipeline.consistency_qa，控制是否执行跨章一致性扫描",
    "option.mono": "覆盖 output.mono，控制是否生成单语版",
    "option.bilingual": "覆盖 output.bilingual，控制是否生成原文译文对照版",
    "option.prepare.input": "待准备书籍（EPUB / FB2 / TXT / Markdown / HTML / PDF）",
    "option.review.input": "全书正文已经翻译完成的源文件",
    "option.review.force": "忽略审校摘要，强制重新审校全部章节",
    "option.review.fix": "覆盖 pipeline.autofix_severe；开启后串行修复漏译和误译",
    "option.state.input": "已建立翻译状态的源文件",
    "option.glossary.source": "需要裁定的原文术语",
    "option.glossary.target": "今后统一采用的目标语言译名",
    "option.assemble.input": "已完成或部分完成翻译的源文件",
    "option.assemble.format": "导出格式：epub / txt / html / markdown",
    "option.qa.input": "已完成翻译的源文件",
    "command.translate": "一键完成准备、翻译、可选审校/QA、报告和导出；中断后原命令续跑。",
    "command.prepare": "只解析书籍、识别语言、分析风格和术语并预扫全书，不翻译正文。",
    "command.review": "使用最终术语库审校完整译文；结果按章保存，可断点续审。",
    "command.status": "查看各章进度与术语库统计。",
    "command.glossary.list": "列出当前书籍术语库中的固定译名和状态。",
    "command.glossary.conflicts": "列出模型抽取过程中发现的未裁定译名冲突。",
    "command.glossary.resolve": "把一个已有术语裁定为指定译名，并关闭对应冲突。",
    "command.assemble": "从已有状态重新生成译文文件，不调用模型或重新翻译。",
    "command.qa": "调用模型执行全书跨章一致性扫描，只报告问题而不修改正文。",
    "command.report": "根据当前章节、审校和术语状态重新生成 report.json，不调用模型。",
    "error.input_missing": "输入文件不存在：{path}",
    "error.unsupported_format": (
        "不支持的输出格式：{format}（可选 epub / txt / html / markdown）"
    ),
    "error.manifest_source_missing": "运行状态 manifest 缺少 source_lang。",
    "error.manifest_target_missing": "运行状态 manifest 缺少 target_lang。",
    "error.source_mismatch": (
        "源语言与已有翻译状态不一致：配置为 {configured}，状态为 {stored}。"
    ),
    "error.target_mismatch": (
        "目标语言与已有翻译状态不一致：配置为 {configured}，状态为 {stored}。"
    ),
    "error.state_source_language_missing": "运行状态缺少已解析的源语言。",
    "error.state_target_language_missing": "运行状态缺少明确的目标语言。",
    "error.identical_source_target": (
        "源语言与目标语言相同（{language}），无需翻译；"
        "请修改 config.yaml 中的 language.source 或 language.target。"
    ),
    "error.translation_state_missing": "尚无翻译进度。请先运行 translate。",
    "error.language_detection_failed": (
        "自动识别源语言失败：请检查模型配置，或在 config.yaml 的 language.source "
        "指定 ISO 639-1 语言代码（如 ja/en/ko/ru/fr/de/es）。"
    ),
    "error.chapter_not_found": "章节编号 {chapter} 不存在；可用范围：{range}",
    "error.review_incomplete": (
        "全书审校要求所有章节先完成翻译；仍待翻译章节：{chapters}"
    ),
    "error.translation_array_missing": "模型未返回译文数组",
    "error.translation_count_mismatch": (
        "译文数量不匹配：期望 {expected} 段，实际 {actual} 段"
    ),
    "error.translation_item_invalid": "模型返回了空译文或非字符串译文",
    "error.translation_fallback_failed": "逐段兜底翻译在第 {index} 段失败",
    "error.punctuation_segment_count_mismatch": ("texts 与 continuations 数量必须一致"),
    "error.prefix": "错误：{error}",
    "error.chapter_finish_options": (
        "--chapter 只翻译并保存指定章节，不能同时使用收尾选项：{options}"
    ),
    "progress.preparing": "准备中…",
    "progress.review_preparing": "准备全书审校…",
    "progress.locating_state": "查找翻译进度…",
    "progress.parsing_document": "解析文档…",
    "progress.detecting_language": "识别语言…",
    "progress.analyzing_style": "分析全书风格…",
    "progress.translation_complete": "翻译完成",
    "progress.prescan_chapters": "预扫章节梗概",
    "progress.generating_overview": "生成全书概览…",
    "progress.translating_titles": "翻译章节标题…",
    "progress.chapter_fallback": "章节 {chapter}",
    "progress.review_chapter": "全书审校：{chapter}",
    "progress.consistency_qa": "一致性 QA…",
    "progress.generating_report": "生成报告…",
    "progress.assembling_translation": "回填译文…",
    "result.chapter_done": "[green]已翻第 {chapter} 章[/]，状态目录：{path}",
    "result.translation_complete": (
        "[bold green]完成[/]：{done}/{total} 章，审校 {reviewed}/{total} 章，"
        "术语 {terms}，一致性问题 {issues} 项。"
    ),
    "result.translation_output": "译文：[bold]{path}[/]",
    "result.prepare_complete": (
        "[bold green]准备完成[/]：解析 {chapters} 章，预扫 {digests}/{chapters} 章，"
        "全书概览{overview}。"
    ),
    "result.overview_generated": "已生成",
    "result.overview_missing": "未生成",
    "result.state_directory": "状态目录：[bold]{path}[/]",
    "result.prepare_resume": "运行 translate 并传入同一源文件即可继续完成全书翻译。",
    "usage.total": (
        "用量（本书累计）：{total:,} tok（提示 {prompt:,} / 生成 {completion:,}），"
        "缓存命中率 {rate:.1%}（命中 {hit:,} / 未命中 {miss:,} tok）"
    ),
    "usage.tier": ("  · {tier}：{total:,} tok，{calls} 次调用，缓存命中率 {rate:.1%}"),
    "usage.stage": (
        "  · 阶段 {stage}：{total:,} tok（提示 {prompt:,} / 生成 {completion:,}），"
        "{calls} 次调用，缓存命中率 {rate:.1%}"
    ),
    "result.review_complete": "[bold green]全书审校完成[/]：发现 {issues} 项问题{fixed}。",
    "result.review_fixed": "，已按配置尝试修复严重项",
    "result.no_progress": "[yellow]尚无进度。先运行 prepare 或 translate。[/]",
    "status.book": "《{title}》（{format}）  {source}→{target}",
    "status.chapter": "章节",
    "status.translation": "翻译",
    "status.review": "审校",
    "status.glossary": "术语库：",
    "glossary.source": "原文",
    "glossary.target": "译文",
    "glossary.type": "类型",
    "glossary.status": "状态",
    "glossary.no_conflicts": "没有待裁定的术语冲突。",
    "glossary.conflict": (
        "  {source}: 现有「{existing}」 vs 提议「{proposed}」（第 {chapter} 章）"
    ),
    "glossary.not_found": "术语不存在：{source}",
    "glossary.resolved": "已裁定 {source} → {target}",
    "result.assembled": "已生成译文：[bold]{path}[/]",
    "qa.count": "一致性问题 {count} 项：",
    "report.written": "QA 报告已写入 {path}",
    "report.summary": (
        "  章节 {done}/{total}  术语 {terms}  待裁决冲突 {conflicts}  "
        "审校问题 {review}  回译疑点 {backtranslation}"
    ),
    "error.pdf_cache_dir_required": "读取 PDF 需要指定运行状态缓存目录。",
    "error.input_format_unsupported": (
        "不支持的输入格式：{format}（支持：{supported}）"
    ),
    "error.epub_rootfile_missing": (
        "EPUB 损坏：container.xml 中没有有效的 rootfile full-path。"
    ),
    "error.pdf_dependencies_missing": (
        "PDF 转换需要额外依赖，请运行：\n"
        "  uv pip install {packages}\n"
        "也可以先手动将 PDF 转为 HTML，保存到本书状态目录的 "
        "{cache_path}，再重新运行。"
    ),
    "error.pdf_conversion_failed": "PDF 转换失败：{error}",
    "error.provider_unknown": ("未知模型提供商：{provider}（支持：{supported}）"),
    "error.json_parse_failed": "无法解析为 JSON：{preview}",
    "error.llm_tier_model_missing": "llm.tiers.{tier}.model 不能为空。",
    "error.llm_strong_model_missing": "配置缺少 llm.tiers.strong.model。",
    "error.provider_base_url_required": ("{provider} 提供商需要配置 llm.base_url。"),
    "error.openai_sdk_missing": (
        "需要 OpenAI SDK：请运行 `pip install openai`，或将 llm.provider "
        "设为 fake 进行离线测试。"
    ),
    "error.api_key_missing": "未设置环境变量 {env}（{provider} API Key）。",
    "error.mineru_extraction_failed": "MinerU 提取 {file} 失败：{error}",
    "error.mineru_timeout": "MinerU 批次 {batch} 处理超时。",
    "error.mineru_html_missing": "MinerU 结果压缩包中没有 HTML 文件。",
    "error.mineru_api": "MinerU API 错误：代码={code}，消息={error}",
    "error.mineru_token_missing": (
        "未提供 MinerU API Token，且没有设置环境变量 {env}。"
    ),
    "value.unknown": "未知",
    "value.no_translatable_chapters": "无可翻译章节",
    "warning.review_index_invalid": (
        "忽略无效审校索引 {index}；当前审校块长度为 {count}"
    ),
    "progress.pdf_pages": "PDF：{pages} 页",
    "progress.pdf_splitting": "正在拆分为每份最多 {max_pages} 页的文件…",
    "progress.pdf_chunk": "  分块 {current}/{total}：{pages} 页",
    "progress.pdf_uploading": "正在上传并提取 {chunks} 个分块…",
    "progress.pdf_chunk_done": ("  分块 {current}/{total} 完成（{chars:,} 字符）"),
    "progress.pdf_done": "完成 → {path}（{chars:,} 字符）",
    "pdf.cli_usage": "用法：uv run python pdf_to_html.py <PDF 路径> [输出 HTML 路径]",
    "result.cancelled": "已取消。",
}
