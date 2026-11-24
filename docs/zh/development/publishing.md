# 发布 Python 包

`Publish Python packages to PyPI` 工作流
（`.github/workflows/publish-pypi.yml`）发布 **`wenyi-core` 和 `wenyi-cli`**。
仓库根目录是虚拟工作区，不是第三个发布包；该工作流不上传 Web 或 Desktop 包。

发布 GitHub Release 时触发自动发布。仅推送标签、创建 PR 或点击 **Run workflow**
都不会上传 PyPI；PR 与手动运行只验证构建，并将分发包保存为 Actions 产物。

## 一次性配置

### GitHub 环境

在 [Wenyi 仓库环境设置](https://github.com/BigDawnGhost/wenyi/settings/environments)
分别创建 Core 使用的 **`pypi`** 和 CLI 使用的 **`pypi-cli`** 环境。

- 不需要 PyPI Token，也不需要添加仓库 Secret。
- 如果希望发布 Release 后全自动上传，不要启用环境人工审批；若配置了审批人，
  上传会等待审批。
- 若启用部署限制，需要允许发布用的**标签**，例如 `v*`。只允许 `main` 分支会阻止
  以标签为身份运行的发布任务。

### PyPI 可信发布者

注册 PyPI 账号，开启双重验证，进入
[账号发布设置](https://pypi.org/manage/account/publishing/)。
对于尚不存在的包，分别添加**两个待定发布者（pending publisher）**：

| 字段 | Core | CLI |
| --- | --- | --- |
| PyPI project name | `wenyi-core` | `wenyi-cli` |
| Owner | `BigDawnGhost` | `BigDawnGhost` |
| Repository name | `wenyi` | `wenyi` |
| Workflow name | `publish-pypi.yml` | `publish-pypi.yml` |
| Environment name | `pypi` | `pypi-cli` |

Workflow name 填工作流的**文件名**，不是显示名称，也不带 `.github/workflows/` 路径。
即使由同一个工作流上传，也需要为两个包分别配置，且环境名必须不同：
PyPI 不允许两个待定项目共用完全相同的 Owner、仓库、工作流、环境身份。
已经注册的 Core 保留 `pypi`，CLI 的发布者和 GitHub 环境使用 `pypi-cli`。
首次成功上传时，PyPI 自动创建项目，
并把待定发布者转换成正式发布者；待定发布者不会提前占用包名。
如果项目已经存在且由你管理，则到项目的 **Manage → Publishing** 添加相同配置。

独立许可证的桥接服务需要在 `BigDawnGhost/wenyi-babeldoc-bridge` 配置自己的发布者，
详见[桥接仓库发布指南](https://github.com/BigDawnGhost/wenyi-babeldoc-bridge/blob/main/docs/publishing.md)。
不要把它添加为采用 [GPLv3](../../../LICENSE) 许可的 CLI/Core 包的依赖。

## 每次发布版本

1. 按正常 PR 流程，将需要发布的改动和工作流合入 `main`，先完成适用验证。
2. 在该已验证提交处选择新标签，例如 `v1.4.2` 或 `v1.4.2rc1`；这里只是示例，
   不是指定必须发布这些版本。CLI 和 Core 从 Git 标签获取版本，不手动修改固定版本号，
   不设置 SCM 版本覆盖变量。
3. 可先打开 **Actions → Publish Python packages to PyPI → Run workflow**，
   填入已存在的标签，验证构建；这一步不会上传。
4. 打开 **Releases → Draft a new release**，选择该标签，发布 Release。
   只保存草稿不会触发上传；预发布标签也应在 GitHub 标记为预发布。
5. 打开对应 Actions 运行，确认 `build`、`wheels`、`publish-core` 和 `publish-cli` 都成功。
   现有 CLI/Desktop 可执行文件打包工作流仍独立运行。

构建作业会检查标签、检出提交和版本一致性、打包契约、严格分发元数据，以及语言和提示词
资源。CLI 根据 Hatch 的 SCM 元数据声明 `wenyi-core==<CLI 版本>`；脱离 Git 从源码
包重建时也会保留这个依赖。uv workspace 从本地成员解析 Core，因此 `uv.lock` 记录的
是工作区来源，不固定开发版本。CLI 和 Core 的可编辑构建使用相同的缓存输入，Git 身份
或两者的打包文件变化时，uv 会一起刷新它们。

构建作业分别在干净的 Python 3.12 环境中仅安装 CLI 的 wheel 或源码包。解析器必须从
同时包含不兼容旧版 Core 测试包的本地 wheelhouse 选中匹配的 Core，不向 PyPI 查询
Wenyi 候选包。随后检查两个包的安装版本、依赖隔离、CLI 启动和随包资源。另用合成文档
验证已安装的 Markdown → DOCX → HTML
→ EPUB → TXT 转换链路和原生双语 PDF 拼版，不调用模型或 PDF 服务。

Core 将原生文档引擎打包为私有 PyO3 扩展。`wheels` 作业构建并验证 CPython 3.10+ 稳定 ABI
wheel，覆盖 glibc Linux（x86-64 和 ARM64）、macOS 13+（Intel 和 Apple Silicon）及
Windows x64。安装匹配的 wheel 不需要 Rust；从源码分发包构建需要 Cargo 和链接器。
CLI 仍发布纯 Python wheel。

Wenyi 自有代码使用 SPDX 标识 `GPL-3.0-only`。各包目录的 `LICENSE` 应与根目录原文一致，
以便源码包脱离仓库也能构建，包括在 Windows 上构建。Core 另附 `THIRD_PARTY_NOTICES.md`，
保留锁定的原生依赖各自的许可证和版权声明。修改 `packages/core/rust/Cargo.lock` 后运行：

```sh
cargo fetch --locked --manifest-path packages/core/rust/Cargo.toml
python scripts/release/core_native_notices.py  # Python 3.11+
python scripts/release/core_native_notices.py --check
```

CI 检查 Core 和 CLI 的分发包（含修复后的平台 wheel）的许可元数据，并将正文与仓库逐字节比对。
自有代码与 Cargo 依赖的许可材料位于 `.dist-info/licenses/`；CLI 发行 ZIP 也将它们放在可执行文件旁。
二进制 wheel 另将构建工具链的标准库许可材料放在 `wenyi_core/_licenses/rust-std/`。官方 rustup
工具链自带这些文件；若发行版单独打包，请将 `WENYI_RUST_LICENSE_DIR` 指向与编译器版本匹配的
`share/doc/rust` 对应目录。平台 wheel 修复工具若加入未经许可检查的动态库，CI 会阻止上传。

只有独立的 `publish-core` 和 `publish-cli` 作业获得 `id-token: write`。
每个作业下载已经验证的产物，不检出或执行仓库构建代码，通过各自的环境身份只上传对应包。
CLI 必须等 Core 上传成功后再执行。Fork 的 Release 不会执行本仓库的上传作业。

## 发布后验证与恢复

等待 PyPI 索引更新后，在新环境验证：

```bash
uv venv .venv-pypi-verify --python 3.12
uv pip install --python .venv-pypi-verify/bin/python "wenyi-cli==1.4.2"
.venv-pypi-verify/bin/wenyi --version
.venv-pypi-verify/bin/wenyi translate --help
```

将 `1.4.2` 替换为实际发布版本。Windows 对应使用 `Scripts/python.exe` 和
`Scripts/wenyi.exe`，而不是 `bin/python`、`bin/wenyi`。

- **可信发布者被拒绝**：核对表格的五个字段，尤其是环境名和工作流文件名；
  两个 PyPI 项目都要配置。
- **提示待定发布者已注册给另一个项目名**：Core 保留 `pypi`，CLI 改用 `pypi-cli`，
  并在 GitHub 创建对应环境；不要删除有效的 Core 注册。
- **环境等待审批**：批准部署，或调整环境的审批和标签规则。
- **标签与版本不符**：创建版本正确的新 Release；不要移动已发布标签，不用版本覆盖掩盖问题。
- **部分上传或网络失败**：点击 **Re-run failed jobs**。显式 PyPI 检查地址让 uv 跳过
  已存在且哈希一致的文件。跨包发布不是原子操作，CLI 失败时 Core 可能已发布。
- **同名文件内容不同**：PyPI 不允许覆盖，必须发布新版本，不通过删除或改写已发布版本绕过。

官方参考：[PyPI 待定发布者](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
与 [uv 可信发布](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi)。
