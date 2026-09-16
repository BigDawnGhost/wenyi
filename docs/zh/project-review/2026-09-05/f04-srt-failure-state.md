# F04 · 字幕失败与空译文被标为完成，续跑无法补译

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f04-srt-failure-state.md)

优先级：**P1** · 类型：已复现缺陷；尚未修复 · 2026-09-05

## 结论与触发条件

已复现两种情况：批次与单条请求都失败时，原文作为译文被写为 `done`，下次恢复不再请求；批次返回合法 JSON 但某个值是空字符串时，也会被写为 `done`，并可能导出空字幕。CLI 仍以完成数报告这些条目。

## 证据与原因

`_translate_single()` 捕获所有异常并返回原文，上层没有成功标记；`_parse_batch_json()` 只检查值是字符串；`missing` 仅检查键是否存在。最终 `apply_translations(..., status=STATUS_DONE)` 统一赋予完成状态。状态读取又接受任何非空 target，削弱了已有 `STATUS_FAILED` 的意义。

## 复现结果

脚本 F04 使用始终抛异常的 FakeClient：首次保留 Hello. 且状态 done，第二次换成正常客户端仍为零调用。另外返回 `{"1":""}` 的批次也产生 done 状态。

## 建议修改

将调用结果建模为成功译文/失败原因，禁止用原文回退伪装完成；若需要可读的部分导出，可在导出副本回退原文，同时保留 failed/pending 状态。验证有效字幕键、非空译文与缺失项；恢复只复用已确认成功的结果，并忽略失败或无效缓存。

## 验收标准

模拟永久错误、重试耗尽、非法 JSON、缺失键、空字符串及空白字符串，不能计为成功；随后恢复成功应只补缺失条目。合法无需翻译的标点或原语言文本不能仅凭“与原文相同”判失败，必须使用显式调用结果。CLI 返回可辨识的部分失败状态并显示成功/失败计数。

## 交付与取舍

约 2–3 日，结合 F03 的缓存规则但保持独立提交。不会可靠地自动识别历史上哪些“原文译文”源自失败，应提供显式重试范围或说明旧状态限制，避免强制重译全部已确认字幕。

## 源码与测试位置

- [trans_novel/srt/translate.py:37](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L37)
- [trans_novel/srt/translate.py:89](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L89)
- [trans_novel/srt/translate.py:273](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L273)
- [trans_novel/srt/translate.py:301](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L301)
- [trans_novel/srt/store.py:210](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/store.py#L210)

复现：按索引运行公共脚本，查看 `F04` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
