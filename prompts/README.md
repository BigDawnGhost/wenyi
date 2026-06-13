# 提示词覆盖

代码内置了一套日译中提示词（见 `trans_novel/agents/prompts.py`）。
若想在**不改代码**的前提下迭代某个 agent 的提示词，在本目录放一个同名覆盖文件即可：

```
prompts/{name}.{src}-{tgt}.md      # 例如 translator_user.ja-zh.md
```

`render()` 会优先读取该文件（找不到才用内置默认）。可用的 `name`：

| name | 说明 |
|---|---|
| `translator_system` / `translator_user` | 翻译（强档） |
| `reviewer_system` / `reviewer_user` | 审校（廉价档） |
| `polisher_system` / `polisher_user` | 润色（强档） |
| `analyzer_system` / `analyzer_user` | 全局分析（强档） |
| `glossary_extractor_system` / `glossary_extractor_user` | 术语抽取（廉价档） |
| `backtranslate_system` / `backtranslate_user` | 回译（廉价档） |
| `consistency_system` | 跨章一致性（廉价档） |

模板用 `string.Template`（`$占位符`），渲染时可用的占位符随 agent 不同，常见有：
`$n`、`$n_minus_1`、`$glossary`、`$style`、`$context`、`$numbered_source`、
`$numbered_target`、`$pairs`、`$honorific_rule`、`$sample`、`$source`、`$target`。

> 覆盖目录可用环境变量 `TRANS_NOVEL_PROMPTS_DIR` 改到别处（默认 `prompts`）。
