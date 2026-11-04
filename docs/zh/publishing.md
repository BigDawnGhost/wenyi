# 发布 Python 包

[English](../publishing.md) · [贡献指南](CONTRIBUTING.md#发布与修复)

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
不要把它添加为 MIT CLI/Core 包的依赖。

## 每次发布版本

1. 按正常 PR 流程，将需要发布的改动和工作流合入 `main`，先完成适用验证。
2. 在该已验证提交处选择新标签，例如 `v1.4.2` 或 `v1.4.2rc1`；这里只是示例，
   不是指定必须发布这些版本。CLI 和 Core 从 Git 标签获取版本，不手动修改固定版本号，
   不设置 SCM 版本覆盖变量。
3. 可先打开 **Actions → Publish Python packages to PyPI → Run workflow**，
   填入已存在的标签，验证构建；这一步不会上传。
4. 打开 **Releases → Draft a new release**，选择该标签，发布 Release。
   只保存草稿不会触发上传；预发布标签也应在 GitHub 标记为预发布。
5. 打开对应 Actions 运行，确认 `build`、`publish-core` 和 `publish-cli` 都成功。
   现有 CLI/Desktop 可执行文件打包工作流仍独立运行。

构建作业会检查标签、检出提交和版本一致性、打包契约、严格分发元数据，以及语言和提示词
资源。随后分别将 wheel 与源码分发包安装到干净的 Python 3.12 环境，检查安装版本和
依赖隔离，并执行 CLI 帮助、版本及语言列表冒烟。这些检查不调用模型，也不翻译 PDF。

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

- **可信发布者被拒绝：**核对表格的五个字段，尤其是环境名和工作流文件名；
  两个 PyPI 项目都要配置。
- **提示待定发布者已注册给另一个项目名：**Core 保留 `pypi`，CLI 改用 `pypi-cli`，
  并在 GitHub 创建对应环境；不要删除有效的 Core 注册。
- **环境等待审批：**批准部署，或调整环境的审批和标签规则。
- **标签与版本不符：**创建版本正确的新 Release；不要移动已发布标签，不用版本覆盖掩盖问题。
- **部分上传或网络失败：**点击 **Re-run failed jobs**。显式 PyPI 检查地址让 uv 跳过
  已存在且哈希一致的文件。跨包发布不是原子操作，CLI 失败时 Core 可能已发布。
- **同名文件内容不同：**PyPI 不允许覆盖，必须发布新版本，不通过删除或改写已发布版本绕过。

官方参考：[PyPI 待定发布者](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
与 [uv 可信发布](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi)。
