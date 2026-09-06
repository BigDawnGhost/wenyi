# F02 · 显式导出路径可覆盖输入文件，事后哈希检查无法保护源文

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f02-export-source-protection.md)

优先级：**P1** · 类型：已复现缺陷；尚未修复 · 2026-09-05

## 结论与触发条件

已在 TXT 导出路径复现。输入与 `--out` 指向同一个文件时，writer 使用写模式覆盖输入，随后收尾服务的源身份复核才抛出错误。用户看到失败提示，但原始文件已经改变，既有状态也无法再通过该源文件的身份检查。SRT 的输出路径同样没有同源保护；本次未逐个执行所有格式的覆盖场景。

## 证据与原因

`assemble()` 先解析输出格式并调用 writer，未拒绝输入输出路径相同的情况。`_assemble_plain_text()` 直接 `open(out_path, 'w')`。`assemble_snapshot()` 的末尾哈希校验能检测变化，却发生在不可逆的源文件覆盖之后。

## 复现结果

脚本 F02 在临时目录中构造完成状态，调用真实快照导出服务，将 TXT 输出设为源路径。观察到 `ValueError`，同时确认源文件字节已经变化。

## 建议修改

在任何产物写入前统一解析单语、双语及附属资源路径，拒绝与输入文件或关键状态文件重合。路径比较覆盖规范化相对路径、符号链接以及现有文件的 `samefile` 硬链接判断。导出采用同目录临时产物，验证完成后再发布；HTML 资源目录等多文件输出需要明确的发布边界。

## 验收标准

同一路径、相对路径别名、符号链接、硬链接均应在写入前拒绝，源文与状态字节保持不变；正常显式 `--out`、默认目录、单语/双语仍成功。注入 writer 失败，已有成品不应被截断。覆盖 TXT、EPUB、DOCX、SRT 与快照并发导出。

## 交付与取舍

优先交付路径保护（约 1–2 日），原子产物发布可另分后续 PR（约 2–4 日）。先修源文件保护，不必等待全部格式事务化。临时文件增加峰值磁盘占用；多文件产物需要独立设计。

## 源码与测试位置

- [trans_novel/assemble/writer.py:56](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/writer.py#L56)
- [trans_novel/assemble/text_writer.py:44](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/text_writer.py#L44)
- [trans_novel/pipeline/finalization.py:158](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/finalization.py#L158)
- [trans_novel/assemble/srt_writer.py:10](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/srt_writer.py#L10)

复现：按索引运行公共脚本，查看 `F02` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
