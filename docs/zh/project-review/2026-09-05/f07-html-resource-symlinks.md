# F07 · HTML 本地资源路径限制可被符号链接绕过

[返回索引](README.md) · [English](../../../project-review/2026-09-05/f07-html-resource-symlinks.md)

优先级：**P2** · 类型：已复现缺陷；尚未修复 · 2026-09-05

## 结论与触发条件

已复现。HTML 源目录内的一个资源符号链接若指向目录外文件，加载器仍会读取目标字节，后续资源打包可将其放入成品。触发需要实际存在这样的链接与引用；本次未发现或验证远程攻击链，也没有读取任何用户敏感文件。

## 证据与原因

`_load_html_resource()` 声明资源不能离开 HTML 源目录，但使用 `abspath` 与 `commonpath` 比较的是词法路径，不会解析符号链接。后续 `isfile/read_bytes` 会跟随链接。现有对 `../` 的限制不能覆盖此情况。

## 复现结果

脚本 F07 在临时目录中创建 `html/linked.txt`，链接到同一临时根目录下的无害 `outside.txt`。加载器返回了源目录之外的文件字节。

## 建议修改

在打开资源前解析可信根目录与候选路径的真实路径，确认真实候选仍位于根内；保留正常目录内链接兼容性，明确处理失效链接与循环链接。若产品需要抵抗同时修改目录的本地对手，再增加基于文件描述符的打开约束；单次 resolve 不能承诺消除所有竞态。

## 验收标准

测试目录外文件链接、目录链接、目录内链接、编码的 `../`、绝对路径、失效/循环链接。越界字节不能进入导出包或 assets；普通图片与 data URI 仍可正常打包。Windows 平台补充其链接能力范围内的测试。

## 交付与取舍

约 1–2 日。优先修复与函数现有约束不一致的行为。过去依赖目录外链接的文档将需要把资源放入源目录；文档应清晰说明允许的资源范围。

## 源码与测试位置

- [trans_novel/assemble/html_resources.py:60](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/html_resources.py#L60)
- [trans_novel/assemble/html_resources.py:87](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/html_resources.py#L87)
- [trans_novel/assemble/html_resources.py:104](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/html_resources.py#L104)

复现：按索引运行公共脚本，查看 `F07` 输出。估时为单人工作量建议，未包含外部服务或真实翻译质量评测。
