# 发行

本文说明 Desktop 打包、更新发布及双语发行说明契约。本地环境和运行时不变量见
[Desktop 开发](desktop.md)，PyPI 见 [Python 包发布](publishing.md)，用户安装和更新
操作见 [Desktop 使用指南](../guide/desktop.md)。

## 发行身份

Python 包元数据使用 `hatch-vcs`。
[发行版本解析器](../../../scripts/release/release_version.py#L24)使用相同的
setuptools-scm Git 来源生成原生应用和产物版本：

| Git 身份 | Python 版本 | 原生版本 |
| --- | --- | --- |
| `v1.2.3` | `1.2.3` | `1.2.3` |
| `v1.2.3rc1` | `1.2.3rc1` | `1.2.3-rc.1` |
| 非标签或有已跟踪修改的 checkout | SCM 开发身份 | 明确的开发身份，不冒充稳定版 |

Cargo 私有 crate 版本不是应用版本。原生可执行文件通过 `--version` 输出
`Wenyi Desktop <version>`，不启动 UI。

显式 `WENYI_BUILD_TAG` 必须存在、指向 HEAD、匹配 Python 元数据，且不能有已跟踪
文件修改。未跟踪笔记不改变 SCM dirty 状态。禁止版本覆盖。发行 tag 支持数字版本及
`a`/`b`/`rc` 预发行版；epoch、post release、local/dev tag、超过三个发行分量及超出
`255.255.65535` 的版本会失败，不静默丢失身份。macOS 数字 bundle version 使用
三段发行版本，应用版本保留限定信息。

分发及应用内更新面向稳定版。开发/预发行身份可用于诊断，但 Linux 包管理器排序未被
规范化为受支持的预发行更新渠道。

## 在目标平台构建

使用 [Desktop 开发](desktop.md)中的前置依赖及环境。在冻结引擎前先构建原生锁库：

```bash
cargo build --release --manifest-path native/wenyi-locks/Cargo.toml
pnpm desktop:build
```

在发行 tag 的干净 checkout 上：

```bash
WENYI_BUILD_TAG=v1.2.3 pnpm desktop:build
```

PowerShell 先设置 `$env:WENYI_BUILD_TAG = "v1.2.3"`，再执行构建。
使用与 checkout 匹配的真实 tag，不通过虚构版本覆盖身份。

[构建启动器](../../../scripts/dev/desktop.mjs#L55)先构建前端、冻结引擎，再将解析出的
版本传入 Tauri。[Sidecar 构建器](../../../scripts/dev/desktop_sidecar.py#L227)
创建临时、仅含 Desktop 依赖的 Python 环境，生成 PyInstaller **onedir** 引擎：

- 重建本地 Core/backend/Desktop 包，不复用缓存中的本地 wheel；核对安装元数据与
  Git 派生的 Python 版本。
- 包含 prompt/i18n 资源、provider 模块、tokenizer 词表、凭据后端和对应平台的
  `wenyi_locks`。普通 Python wheel 安装不携带该原生库，可使用行为兼容的 Python
  锁后端。
- 排除 CLI、Web API、PostgreSQL、Redis 和 Arq 运行包。BabelDOC 保持独立下载、
  进程隔离的 AGPL 插件，不进入 sidecar。
- 在仓库之外，以临时工作区**及用户目录**、无开发 Python path、无外部服务访问的环境
  做 smoke check，覆盖带鉴权的离线 TXT 解析/预览、ready/诊断协议、正常退出和真实的
  隔离工作区锁启动失败。

Onedir 避免每次启动都解压整个 Python 运行时。发行完整的原生包及其引擎/资源，
不能只发行裸 Rust 可执行文件。Windows 发行宿主/引擎启动不创建控制台窗口；
源码 debug 启动保留开发控制台和私有协议管道。

macOS Apple Silicon 与 Intel 分别使用匹配的原生 Python/Rust 工具链构建。
只交叉编译 Rust 宿主不会把 sidecar 重建为另一架构。一个 OS 构建成功不代表另一个已验证。

## 产物与签名

Desktop 发行附件命名为 `wenyi-desktop-<version>-<platform>-<arch>.<extension>`。
平台/架构后缀与完整格式集合如下：

| 目标 | 安装/下载产物 | 已签名更新载荷 |
| --- | --- | --- |
| `linux-x64` | `.AppImage`、`.deb`、`.rpm` | `.AppImage` 及其 `.sig` |
| `windows-x64` | NSIS `.exe` | `.exe` 及其 `.sig` |
| `macos-arm64` | `.dmg` | `.app.tar.gz` 及其 `.sig` |
| `macos-x64` | `.dmg` | `.app.tar.gz` 及其 `.sig` |

Windows 使用 NSIS 而非 MSI，以保留预发行/开发身份。安装包直接上传，不套外层 ZIP。
GitHub workflow artifact 下载可能自带包装，但那不是 Release 附件格式。

[安装包收集器](../../../scripts/release/package_release.py#L1)要求准确的版本、架构及
完整平台格式集合，不将旧产物改名为新版。Desktop 校验和为
`wenyi-desktop-SHA256SUMS.txt`；CLI 单独提供
`wenyi-cli-<version>-<platform>-<arch>.zip` 和 `wenyi-cli-SHA256SUMS.txt`。
CLI ZIP 条目保留 Unix 可执行权限。

**更新签名与 OS 代码签名/公证是两套机制。** 当前打包 workflow 不执行 Windows/macOS
OS 代码签名或 macOS 公证；这些要求及真实终端用户安装检查仍由发行流程负责。

### 更新签名配置

在 **checkout 之外**的安全位置生成 Tauri 签名密钥：

```bash
pnpm exec tauri signer generate --write-keys /secure/path/outside-checkout/wenyi-updater.key
```

使用交互密码提示，不把真实密码放入 shell 历史。安全备份私钥/密码，不放进源码、
issue、日志或 PR workflow。跨发行保留同一密钥：已安装构建信任内置公钥，轮换需
明确规划信任迁移或手动升级。

配置 GitHub Actions 仓库条目：

| 名称 | 类型 | 值 |
| --- | --- | --- |
| `WENYI_UPDATER_PUBLIC_KEY` | Variable | 生成的 `.pub` 完整内容，不是路径 |
| `TAURI_SIGNING_PRIVATE_KEY` | Secret | 私钥完整内容 |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Secret | 密码；有意使用无密码密钥时为空 |

本地签名构建通过安全环境/secret manager 提供三个环境变量，再构建匹配的干净稳定
tag。本地密码变量必须定义，即使为空。macOS 使用
`pnpm desktop:build --bundles dmg,app`，以包含更新归档。

启动器只为已配置的稳定签名构建启用 `bundle.createUpdaterArtifacts`。
部分配置、公钥格式错误或将公钥路径当成内容、以及签名开发/预发行身份均明确失败。
普通 Cargo 测试、源码启动和未签名构建不需要签名秘密。

## 发布工作流

[Desktop packages workflow](../../../.github/workflows/desktop.yml#L1)与
[CLI packages workflow](../../../.github/workflows/build.yml#L1)构建选中的 checkout
或可选 `workflow_dispatch` tag。手动运行默认仅生成 workflow artifact，不发布。
GitHub Release 发布后构建其 tag，各 workflow 仅上传自己的组件附件。PyPI 使用独立
workflow 和[发布契约](publishing.md)。

要为已有稳定版重新构建 Desktop 附件，选择包含所需构建环境修复的 workflow 分支，
将 `tag` 设为该发行版，并显式开启 `publish`。准入检查会在打包前拒绝缺失的 tag、
草稿及预发行版。应用源码仍来自原始干净 tag，而非 workflow 分支；不会移动 tag，
也不会纳入新的运行时代码修复。四个平台全部通过后才上传附件，不覆盖已有同名文件，
也不重新触发 CLI/PyPI 发布。

对旧 tag，后端测试步骤会将 `TCP_NODELAY` 断言修正为接受任意非零启用值
（macOS 返回 `4`，不限定为 `1`），运行完整后端测试，并在成功或失败后按原始字节
恢复测试文件，因此版本校验与打包仍使用原始干净 tag。

Desktop PR 检查使用缩减的平台/构建层级；PR 通过不等于完整发行打包矩阵通过。
已发布 Release 和手动 Desktop 构建覆盖完整的 Linux x64、Windows x64 及两种原生
macOS 目标。Desktop 的 main push 工作用于预热构建缓存，不发布安装包。

签名秘密仅提供给稳定 Release 构建及显式发布的稳定版重建。
PR、普通手动运行及开发/预发行构建保持未签名。
没有签名配置时，稳定版仍生成手动安装包，并明确**不发布 `latest.json`**。
部分签名配置会失败，不公告客户端无法安装的更新。

更新仓库是构建期常量：
[`WENYI_UPDATE_REPO`](../../../apps/desktop/build.rs#L1)，默认
`BigDawnGhost/wenyi`，必须恰好是 `owner/repository`。发布 workflow 将其设置为
`github.repository`，因此使用同一 workflow 的 fork 从自身仓库更新。清单查询和可信
产物 URL 使用同一仓库。无需独立更新服务，构建读取
`https://github.com/<repository>/releases/latest/download/latest.json`。

已签名稳定版的发布顺序：

1. 构建并收集每个平台的安装包、更新载荷和签名。
2. [校验更新产物](../../../scripts/release/desktop_updates.py#L126)：要求稳定 SemVer、
   绑定版本的签名、匹配配置公钥的 key ID，以及完整目标集合
   （`windows-x86_64`、`linux-x86_64`、`darwin-aarch64`、`darwin-x86_64`）。
3. 成功上传载荷、签名及组件校验和。
4. 用实际重命名后的 Release URL、签名内容和发行说明生成清单，**最后上传
   `latest.json`**。

上传不覆盖已有附件，同名重试会失败。缺失、损坏或不匹配的签名不得生成部分清单。
发布阶段检查签名结构/key 身份不等于密码学验签。原生更新器在安装前校验下载产物及其
签名绑定版本；保持 `requireSignedVersion`，防止旧的已签名产物被重新标为新版。

应用内安装支持已配置的稳定 Windows NSIS、macOS Apple Silicon/Intel 及 Linux
AppImage。macOS 保持已安装架构。Linux `.deb`/`.rpm` 使用包安装，不以 AppImage
替换。检查不静默下载、不强制安装或重启；只有已配置且支持的稳定构建在启动时自动检查。

## 原位升级安全

[原生安装流程](../../../apps/desktop/src/updates.rs#L635)、
[后端准入门](../../../apps/desktop/backend/wenyi_desktop/updates.py#L26)及
[数据根目录租约](../../../apps/desktop/src/profile.rs#L153)构成同一个安全边界，
不能简化成“没有 running job 记录”。

1. 前端要求明确确认，并处理编辑器/设置的未保存变更。原生保存准入同时计算待选择
   对话框和活跃复制。
2. 通过所选数据根目录的 `locks/` 取得独占访问，排除同根目录下其他原生/独立引擎及
   仍存活的 writer 租约，不能只看窗口。替换程序文件前，同一可执行文件的其他副本
   也必须退出，即使它们使用不同的数据根目录。
3. 关闭写操作准入，包括流式请求。拒绝活跃插件操作、queued/running job 和运行时
   持有的 pending/draining 工作。
4. 写入冻结期间，从当前目录库解析准确备份计划：`<data-root>/catalog.sqlite3`
   及每个已登记项目目录，包含显式选择的外部项目库；不额外发现旧版位置。
5. 停止并回收引擎，确认 writer 已静止。在调用安装器前完成备份复制和校验，独占租约
   保持到安装/恢复结束。

[升级快照](../../../apps/desktop/src/migration/backup.rs#L114)写入
`<data-root>/backups/<版本>-<时间戳>/`。只复制目录库及选定项目目录，不复制整个根目录、
用户选择的父目录或旧备份。停止引擎后纳入仍存在的目录库 WAL/SHM，并保留项目数据库
附属文件；强制进程退出不证明已完成 checkpoint。清单记录源路径、文件大小和 SHA-256，
复制成功后才写入。凭据保留为引用，不导出系统凭据库/会话秘密。插件运行时与 PDF
会话需另行[手动备份](../guide/desktop.md#数据位置与备份)。

缺失来源、不安全链接、空间不足、复制/校验失败都会在安装器执行前中止。
恢复会尝试重新打开现有引擎，不承诺所有安装失败都自动从备份恢复。
引擎取消更新准备未获确认时，保持明确的不可用/错误状态，不能推测成功后恢复写入。

### Windows 安装器安全

保持 [Windows bundle 配置](../../../apps/desktop/tauri.windows.conf.json#L1)与
[NSIS 模板](../../../apps/desktop/installer.nsi#L106)及
[目录准入 hooks](../../../apps/desktop/installer.nsh#L140)相连：

- `installMode` 保持 `currentUser`，拒绝提权启动。程序与数据默认位置分离，具体路径见
  [Desktop 指南](../guide/desktop.md)。安装器同时提供英文和简体中文界面。
- 安装与卸载共用互斥锁。替换/删除前同时检查原生程序和
  `sidecar/wenyi-engine/wenyi-engine.exe` 是否被占用，不强杀翻译。交互、静默和 passive
  模式都必须拒绝占用文件及降级；项目 SQLite 版本拒绝是独立保护，不允许旧安装器绕过。
- 写入程序前拒绝盘符/共享/用户根目录、Windows/Program Files、与数据或旧位置重叠、
  存在链接祖先或不可写的目标。新位置或不同位置必须为空；只有已登记的当前布局原位
  升级可以保留程序目录里的无关文件。
- 运行时布局检查使用实际可执行文件目录，源码、解包及显式 `--data-dir` 启动同样检查。
  对尚不存在的目标先解析已有祖先，防止链接或 Windows 短路径掩盖重叠。
  精确备份来源规划使用相同规则，防止把来源复制到自身内部。
- 手动安装器不创建快照，也不要求历史 `MANIFEST.json`。应用内更新器仍在启动
  `/UPDATE` 前生成并校验快照，两者的保证不同。
- 以有效的“添加或删除程序”登记为准，不只看记住的位置。替换旧登记程序时调用其
  已记录的卸载器，只处理已知程序文件；不递归删除旧的程序/数据混合目录，也不迁移数据。
- 普通卸载提供两个独立、默认不勾选的选项：删除目录库/设置/日志/锁/运行时，或删除
  默认项目与升级备份。它们只影响 Windows 默认数据根目录；自定义根目录、外部项目库
  和旧版目录保持不动，递归清理不得跟随链接。即使项目文件保留，删除目录库也会丢失
  项目列表。静默/passive 卸载与升级替换不得选择数据清理。

在真实 Windows 上以普通用户验收：首次安装、同版重装、升级、降级/提权拒绝、
原生程序/引擎文件占用、并发安装/卸载、各项数据删除选择，以及包括未创建目标的
目录/链接别名安全。交叉编译和浏览器 IPC mock 不能验证这些安装器行为。

## 发行说明

Desktop 的[语言选择器](../../../apps/desktop/frontend/src/localizedReleaseNotes.ts#L5)和
[只读渲染器](../../../apps/desktop/frontend/src/ReleaseNotes.tsx#L36)读取 GitHub Release
正文，格式保持稳定：

1. 直接从 `## English` 开始，Release 标题单独填写。
2. 写简短概述和适用的 `###` 分类，更深标题使用 `####`。语言分区内部不放其他一级/
   二级标题或水平分隔线。
3. 两种语言间放独立 `---`，随后使用准确的 `## 中文` 及等价中文内容。
4. 若有共享比较链接页脚，放在最后一个独立 `---` 之后。

推荐分类顺序，省略空分类：

| English | 简体中文 |
| --- | --- |
| New features | 新增功能 |
| Improvements | 改进 |
| Fixes | 问题修复 |
| Development and documentation | 开发与文档 |
| Upgrade notes | 升级说明 |

解析器不区分大小写地识别 `English`/`en` 和 `中文`/`简体中文`/`zh-CN`。
同级或更高级标题、水平分隔线结束当前分区；代码块中类似语言标题的文字不是边界。
缺失/未标记的当前语言显示本地化空提示，不回退另一语言。

支持 Markdown 标题、列表、强调、代码、引用和 GFM 表格/任务列表。
忽略原始 HTML，链接/图片只渲染文字，不进行外部导航或远程图片请求。
优先使用可移植的纯文字 Markdown，不让升级说明依赖应用内可点击链接或图片。

按目标历史中前一个稳定版以来已核对的净变更起草，不复制 commit 清单，也不使用不相关
的“最新”tag。说明平台范围、真实升级操作及限制；无证据不声称签名、测试执行、
性能或质量提升。两种语言等价，过程说明放在可复制正文之外。仓库
[release-notes skill](../../../.agents/skills/release-notes/SKILL.md)提供具体起草流程，
起草不等于获准发布。

## 发行验证

以 workflow 命令为可执行事实源，核对 Python 元数据、原生 `--version`、安装包名称
和更新签名中的版本一致。运行 sidecar 资源/离线 smoke check、原生锁契约、
受影响 Python/Rust 测试、两端前端构建/边界检查及相关 E2E。

各目标的发行包验收使用临时数据：无系统 Python 的首次启动、原生保存/拖放、托盘和
再次启动激活、正常/强制引擎清理、引擎不可用时的诊断导出、更新准入、已校验备份及
安装器恢复/拒绝。Windows 另需 GUI subsystem/无控制台验证；macOS 验证两种架构；
Linux AppImage 与包管理器安装使用不同更新路径。

记录实际执行了什么、哪些签名/公证步骤完成、哪些原生平台未测。
浏览器 IPC mock、源码 debug 启动或单独 Linux 构建都不能证明所有平台已经可发行。
