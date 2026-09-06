# F07 · HTML resource containment checks do not resolve symlinks

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f07-html-resource-symlinks.md)

Priority：**P2** · Type: reproduced defect; not fixed · 2026-09-05

## Finding and trigger

Reproduced. A resource symlink inside an HTML source directory can point outside it; the loader follows it, and packaging can include the external bytes. This requires an existing link and reference. No remote exploitation chain or user-sensitive files were investigated.

## Evidence and reproduction

The loader promises containment but compares `abspath/commonpath`, which are lexical. `isfile/read_bytes` follow symlinks. Probe F07 links a temporary `html/linked.txt` to a benign sibling fixture and receives its bytes.

## Proposed change

Resolve the trusted root and candidate to real paths before checking containment. Preserve valid in-tree links and handle broken/cyclic links clearly. If protection against concurrently modified directories is required, design descriptor-based opening separately; one resolve operation does not eliminate all races.

## Acceptance and delivery

Cover external file/directory links, internal links, encoded traversal, absolute paths, and broken/cyclic links. External bytes must not enter packages/assets; normal images and data URIs remain supported. Include Windows-supported link cases. Estimate 1–2 days. Documents relying on out-of-tree links will need resources relocated into the allowed source tree.

## Source and test locations

- [trans_novel/assemble/html_resources.py:60](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/html_resources.py#L60)
- [trans_novel/assemble/html_resources.py:87](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/html_resources.py#L87)
- [trans_novel/assemble/html_resources.py:104](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/html_resources.py#L104)

Reproduce using the shared script in the index; inspect `F07` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
