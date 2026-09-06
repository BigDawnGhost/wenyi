# F09 · 同一字幕状态缺少运行锁，多写者会竞争临时文件

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f09-srt-run-lock.md)

优先级：**P1** · 类型：已复现缺陷；尚未修复 · 2026-09-05

## 结论与触发条件

底层竞态已确定性复现。同一 SRT 同时启动两个进程或调用方时，两者都可通过源身份检查并读写同一 cues、batches、manifest、usage。SRT 只有事件追加锁，未给整个翻译会话加运行锁；固定的 `.tmp` 路径进一步造成替换竞争。

## 证据与复现边界

`SrtRunStore._file_lock()` 目前只由事件锁使用，`translate_srt()` 没有运行锁作用域。脚本 F09 用两个独立 store 实例和 barrier 强制它们在同一临时文件写完后替换，稳定得到一次 `FileNotFoundError`。这是底层双写者反例，未进行完整两进程 CLI 压力测试。

## 建议修改

在字幕领域内为源身份确认、初始化、缓存恢复、翻译和最终状态发布持有跨进程运行锁；同一书籍串行，不同字幕仍并行。若需要独立字幕导出，再设计短状态锁与快照。唯一临时文件名只能避免重命名冲突，不能替代锁保护下的读改写和用量合并。

## 验收标准

补两个进程同时处理同源字幕的离线测试：一个等待或明确报告占用，不能重复请求、丢译文、漏累计用量或破坏 JSON。验证等待后重新读取状态、异常退出释放锁，以及不同源文件不互相阻塞。覆盖 Linux/Windows 锁实现。

## 交付与取舍

约 2–3 日，建议与 F03/F04 一同进入首轮稳定性修复，但独立文档和提交。保持 SRT 轻量流程，不将它接入书籍 Orchestrator，也不引入术语或 Review。长锁将同源任务串行，这是正确性所需。

## 源码与测试位置

- [trans_novel/srt/store.py:59](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/store.py#L59)
- [trans_novel/srt/store.py:287](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/store.py#L287)
- [trans_novel/srt/translate.py:151](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L151)
- [trans_novel/pipeline/runstore.py:65](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/runstore.py#L65)

复现：按索引运行公共脚本，查看 `F09` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
