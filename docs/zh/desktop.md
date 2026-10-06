# Wenyi Desktop

[English](../desktop.md) · [Web 部署与开发](web.md)

Desktop 是使用 Tauri、React 和现有 Python 翻译内核的**本地翻译应用**。它自行启动私有后端，使用独立 SQLite 工作区，不需要部署 Web、PostgreSQL、Redis 或 Docker。发行包包含 Python 运行时，用户无需另外安装 Python。

翻译仍需访问所配置的模型提供商。可选 MinerU、BabelDOC 服务保留各自的使用条件；“本地应用”不意味着外部模型或文档服务可以离线使用。

## 独立数据

Desktop 不接管或迁移已有 Web 项目，也不读取或修改 CLI 的 `config.yaml`、`state/`、`output/`。CLI 的行为保持不变。

默认桌面工作区：

| 平台 | 位置 |
| --- | --- |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/Wenyi Desktop` |
| macOS | `~/Library/Application Support/Wenyi Desktop` |
| Windows | `%LOCALAPPDATA%/Wenyi Desktop` |

工作区包含 SQLite 目录库、各项目的 SQLite 状态、上传原文、解析缓存和导出产物。备份时保留完整工作区，并先退出 Desktop；不要只复制仍在使用的 SQLite 主文件而遗漏 WAL。

可用 `--data-dir <路径>` 选择独立工作区，例如用于测试。同一工作区只能由一个后端进程持有。切换工作区不会导入或删除旧工作区。

## 从源码运行

准备 Python 3.10+、`uv`、Node 22、pnpm 9、Rust stable 和 [Tauri 平台前置依赖](https://v2.tauri.app/start/prerequisites/)。这些是开发及构建环境要求，不代表发行包用户需要额外安装 Python。

在仓库根目录执行：

```bash
uv sync --locked --package wenyi-desktop --group dev
pnpm install --frozen-lockfile
pnpm desktop
```

使用隔离的预览工作区：

```bash
pnpm desktop --data-dir /path/to/desktop-test-workspace
```

启动脚本先构建界面，再启动原生应用。Debug 构建使用仓库 `.venv`，也可用 `WENYI_DESKTOP_PYTHON` 显式指定开发解释器。不再提供远程 `--url` 模式，也不会悄悄回退连接 Web 服务。

在目标平台构建原生发行包：

```bash
pnpm desktop:build
```

构建先将 Python 引擎冻结为 onedir sidecar，再与界面和原生程序一起打包。Onedir 避免每次启动都解压完整运行时。每次发行仍需验证安装包、签名和平台运行依赖；Linux 构建成功不代表 Windows/macOS 已验证。

Windows 发行包只打开应用窗口，不弹出控制台。本地引擎也不打开控制台窗口，
但保留内部通信管道。Debug/源码启动仍保留开发终端，便于诊断。

原生包分别为 Linux、Windows 和 macOS 使用 PNG、ICO 与 ICNS 图标。
需要从共享徽标重新生成原生图标时，在仓库根目录运行
`pnpm exec tauri icon packages/ui/src/assets/wenyi-emblem.png --output /path/to/temporary-icons`，
然后仅将 `icon.ico` 与 `icon.icns` 复制到 `apps/desktop/icons`。

### 版本与发行下载

Python 元数据继续使用 `hatch-vcs`；`scripts/release_version.py` 使用相同的
setuptools-scm Git 来源派生原生应用和产物版本。例如，`v1.2.3` 对应 `1.2.3`，
`v1.2.3rc1` 对应 Python `1.2.3rc1` / 原生 `1.2.3-rc.1`。非标签提交和已跟踪
文件的本地修改保留明确的开发身份，例如 `1.2.4-dev.2+gabc123`。Cargo 私有 crate
的版本不代表应用版本。原生可执行文件的 `--version` 选项输出
`Wenyi Desktop <version>`，不启动界面。

当前发行面向正式稳定 tag。开发/预发行身份用于构建诊断，但尚未规范化 Linux
包管理器的升级排序：例如 Debian 会将 `1.2.4-rc.1` 排在 `1.2.4` 之后。
不要将这些构建视为已支持的预发行更新渠道。

在对应标签的干净 checkout 上使用 `WENYI_BUILD_TAG=v1.2.3 pnpm desktop:build`
（PowerShell 先设置 `$env:WENYI_BUILD_TAG = "v1.2.3"`）。明确指定的标签必须存在、
指向 HEAD、与 Python 版本一致，且不能有已跟踪文件修改；未跟踪笔记不改变
setuptools-scm 的 dirty 状态。禁止覆盖版本。发行标签支持数字版本及 `a`、`b`、
`rc` 预发行版；epoch、post release、带 local/dev 的发行标签、超过三个数字分量
或超出 `255.255.65535` 的版本会报错，不会静默丢失身份。macOS 数字 bundle version
使用三段发行版本，应用版本保留预发行/开发信息。Windows 使用 NSIS `.exe`，不使用
无法完整表达这些预发行/开发身份的 MSI。

**CLI packages** 和 **Desktop packages** workflow 均支持通过 `workflow_dispatch`
指定可选 `tag`；留空构建选定的 checkout。运行名称显示组件与标签（或开发 ref）。
GitHub release 发布事件构建该 release 的标签，仅上传各自组件的附件。
手动运行只生成 workflow artifact，不发布 release。

- 所有平台 CLI 下载均为 `wenyi-cli-<version>-<platform>-<arch>.zip`。ZIP 条目
  保留 Unix 可执行权限；需使用支持该权限的解压工具，或解压后执行 `chmod +x wenyi`。
- Desktop 下载为 `wenyi-desktop-<version>-<platform>-<arch>.<extension>`：
  Linux AppImage（单文件应用）、`.deb` 和 `.rpm`；Windows NSIS 安装程序 `.exe`；
  macOS `.dmg`。这些文件直接上传，不额外套 ZIP。AppImage 下载后需赋予可执行权限
  （`chmod +x <file>.AppImage`）；裸 Rust 可执行文件不是可独立发行的应用。
- GitHub artifact 服务可能为 CI 下载额外打包；真正的 GitHub Release 附件是上述文件。
  校验和分别为 `wenyi-cli-SHA256SUMS.txt` 与 `wenyi-desktop-SHA256SUMS.txt`。
  不覆盖已有 release 附件，重复上传会失败。

Sidecar 构建会重新构建本地 Python 包，而不是复用其缓存 wheel，并在冻结前核对
元数据与解析出的 Git 版本一致。安装包收集严格匹配版本、架构和该平台的完整格式
集合；旧产物原样保留，不会被改名冒充新版本。

这些 workflow 不执行操作系统代码签名或公证；平台签名与终端用户安装验证仍是发行前的责任。

已在 KDE Wayland 下使用临时工作区和无效的开发 Python 路径启动 AppImage：包内引擎成功启动，带鉴权的 loopback 请求正常，关闭原生窗口后引擎退出并被回收。独立冻结引擎检查还覆盖了离线合成 TXT 上传、解析、预览及新旧格式事件读取。真实文件管理器拖放、系统保存对话框、Windows/macOS 运行及可移动介质行为仍需平台验收。

## 应用更新

更新区域位于**全局设置底部**，显示由 Git 派生的当前应用版本。可手动检查新的公开发行版，
并打开 [GitHub Releases](https://github.com/BigDawnGhost/wenyi/releases)。
旧版用户需要先手动升级一次到包含更新器的版本。升级保留 Desktop 工作区；升级前请备份。

只有已配置更新签名的正式稳定原生构建会在启动时自动检查，且不会阻塞启动。
检查不会静默下载、强制安装或重启应用。安装需要明确确认；请先完成或取消运行中的任务，
并保存或放弃编辑器及设置中的未保存修改。应用在安装并重启前会再次检查这些限制。

已签名的 Windows NSIS、macOS 应用和 Linux **AppImage** 支持应用内安装更新。
Linux `.deb`/`.rpm` 始终通过手动下载及包安装升级，不会用 AppImage 替换。
开发、本地及未配置签名的构建仍可检查公开发行版并手动下载。
网络或检查失败会明确显示，不影响继续使用 Desktop。
应用内安装必须验证更新签名；它与 **Windows/macOS 操作系统代码签名及公证是独立机制**。
签名还必须绑定公告中的应用版本（`requireSignedVersion`），旧的已签名产物不能冒充新版。

### 维护者签名配置

无需部署更新服务。原生更新器使用固定 HTTPS 端点：
`https://github.com/BigDawnGhost/wenyi/releases/latest/download/latest.json`。
不要在仓库内生成密钥。在仓库根目录执行以下命令，将 Tauri signer 密钥写入
**checkout 之外**的安全位置：

```bash
pnpm exec tauri signer generate --write-keys /secure/path/outside-checkout/wenyi-updater.key
```

通过交互提示输入密码，不要将真实密码放入命令历史。安全备份私钥和密码；
禁止提交、粘贴到 issue/日志，或在 PR workflow 中使用。不同发行版应保留同一密钥：
已安装应用信任内置公钥，更换密钥需要规划迁移或手动升级。

在 GitHub Actions 仓库中配置：

| 名称 | 类型 | 值 |
| --- | --- | --- |
| `WENYI_UPDATER_PUBLIC_KEY` | Variable | 生成的 `.pub` 文件完整内容，不是路径 |
| `TAURI_SIGNING_PRIVATE_KEY` | Secret | 私钥文件完整内容 |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Secret | 私钥密码（有意使用无密码密钥时为空） |

本地签名构建从安全环境或 secret manager 提供同名三个环境变量（公钥不是秘密），
在对应干净标签 checkout 执行 `WENYI_BUILD_TAG=v1.2.3 pnpm desktop:build`。
macOS 添加 `--bundles dmg,app` 以生成额外的更新归档。
启动脚本仅为已配置的稳定签名构建启用 Tauri v2 `bundle.createUpdaterArtifacts`。
部分配置、公钥路径/格式错误，或为开发/预发行版本签名都会明确失败。
本地密码变量必须定义，即使值为空。普通 `cargo test`、`cargo run`、PR 构建及未签名的
`pnpm desktop:build` 不需要签名秘密。

Desktop workflow 仅向稳定 release 构建或明确开启的稳定 tag 手动签名测试提供签名秘密；
PR、开发/预发行构建及普通手动 workflow 运行保持未签名。
签名配置全部缺失时，稳定发行仍生成手动安装包，并明确记录**不发布 `latest.json`**。
部分签名配置则失败，不发布误导性的更新元数据。

签名发行在 Windows `.exe` 和 Linux `.AppImage` 旁增加 `.sig`；
macOS 在现有 `.dmg` 之外增加 `.app.tar.gz` 及 `.sig`，不额外套安装包 ZIP。
`scripts/desktop_updates.py` 校验完整三平台集合，生成正式 SemVer manifest，
包含 `windows-x86_64`、`linux-x86_64`、`darwin-aarch64`、实际重命名后的发行 URL
和签名内容。所有更新包及签名构建、上传成功后才**最后上传**含发行说明的 `latest.json`。
发行上传不覆盖已有附件；同名附件已存在时重试会失败。缺失或错误签名不能发布部分 manifest。
没有 manifest 的发行不能通过应用内更新器安装，请手动下载。

### 不发布 Release 的签名测试

1. 将更新器及签名测试代码合并到默认分支，为计划发行的版本推送稳定 tag，例如 `v1.2.3`。
   tag 必须包含这些脚本；旧 tag 无法测试后来新增的代码。**不要创建 GitHub Release。**
2. 打开 **Actions → Desktop packages → Run workflow**，工作流分支保持为仓库默认分支，
   填写 `tag`，勾选 **Test updater signing**（`test_updater_signing`，默认关闭）。
3. 检查三个平台构建，确认 **Verify updater payload and signed version with public key**
   步骤通过：它使用 `WENYI_UPDATER_PUBLIC_KEY` 对更新包内容、受签名保护的元数据及版本
   绑定进行实际密码学验证。公私钥不匹配、内容被修改或签名配置缺失时会失败，不会冒充
   已签名测试包。
4. 在本次运行的 **Artifacts** 下载 `wenyi-desktop-signing-test-<version>-<platform>`。
   每个平台包含安装包、更新产物和 `.sig`，保留 14 天。

此运行仅有仓库只读权限，并跳过发布任务；不创建或上传 Release，不发布或修改 `latest.json`。
它验证签名和打包，**不验证应用内下载、安装及重启流程**；线上更新端点不会公告这些测试产物。
此手动测试不会向普通发行流程新增验签步骤。

## API 密钥

打开**设置 → API 提供商与模型**，配置提供商、模型及可选 base URL，先保存连接配置，再在对应密码框输入并保存 API key。

- Desktop 自动优先使用受支持的系统凭据库：通过 `keyring` 接入 Keychain、Windows 凭据库、Secret Service 或 KWallet。
- 凭据库不可用或写入失败时，密钥**仅在本次会话的内存中保留**，界面明确提示下次启动需要重新输入。无需选择存储方式，也不会回退为明文文件。
- 已保存密钥不会回显；输入框留空不会清除或替换已有密钥。
- 高级选项支持指定环境变量名。没有选中手动凭据时，留空使用提供商默认变量名；明确指定变量后不会回退到其他变量或连接的密钥。在应用外修改继承环境后需要重启 Desktop。
- 使用**清除手动密钥**移除密钥，或使用明确的“清除并改用环境变量”操作。缺失的手动/会话密钥不会自动切换为环境变量密钥。

凭据变更对新建客户端生效；运行中的任务保留配置和凭据快照。重命名连接会迁移凭据引用，删除连接后新连接不能继承它的密钥。

SQLite 仅保存来源模式和不透明凭据引用。密钥不会保存到 YAML/JSON、项目状态、浏览器存储或 API 响应。Web 和 CLI 保留原有环境变量方式。

**检查本地可用性**只检查本地配置，不联系提供商，也不发送模型请求验证密钥。

## 导入与保存

- 从文件管理器拖入支持的文件，或使用浏览按钮。拖入只选择文件，点击**创建**才开始上传。原生选择是短时、单次使用授权，不是通用文件系统权限；原生上传失败或授权过期后需要重新拖入。
- Desktop 导出先显示原生目标位置选择框，选好后才创建导出任务。取消选择不会创建任务，也不会写入输出文件。
- 已完成的历史条目提供**另存为…**。保存以流式方式写入目标目录中的临时文件，仅在完成后正式发布。覆盖现有文件必须确认；传输失败保留原目标文件。
- 导出历史保留最近五个已完成条目。较旧条目立即从历史中移除，已开始的保存仍可读取。文件下载会延迟删除到最后一个流关闭，包括取消或断连；HTML ZIP 使用独立的临时归档。清理失败时，会在后续导出发布或重启时重试。
- HTML 明确保存为 **HTML + 资源（ZIP）**，文件名以 `.html.zip` 结尾。先解压再打开 HTML，保留相对图片及媒体链接；归档仅包含该次已发布 HTML 及其配套资源。
- Desktop 拒绝内部工作区目标及本次应用会话记录的原文文件身份/路径。源保护记录有上限，仅存于内存，不长期占用原文文件句柄。保存期间不要用其他应用替换同一目标：覆盖是原子文件替换，不是跨进程的条件交换。Web 继续使用浏览器下载。

## 启停与渲染

启动和关闭使用无加载文案的安静过渡。启动错误仍明确显示，并提供重试/重新加载操作。界面请求等待本地引擎就绪握手，不回退到远程端点。后端仅监听随机分配的回环端口，每次启动生成新的内存 token。

Windows 的 debug Python 覆盖值必须是现有可执行文件路径，不能是 PATH 命令或包装器。对于 CPython venv，Desktop 读取 `pyvenv.cfg`，直接启动基础 `python.exe` 并设置 CPython 的 `__PYVENV_LAUNCHER__` 提示。这与 CPython venv 重定向器使用相同机制：导入环境和 `sys.executable` 仍属于 venv，但引擎本身成为原生应用持有的子进程。就绪 PID 仍必须与该子进程完全一致；就绪数据不能授权打开或终止其他 PID。venv 配置无效时启动失败，不回退到重定向器。打包后的 onedir 引擎仍直接启动，不发现或依赖外部 Python/venv。这个单进程契约避免了仅为管理重定向器后代而引入 Windows Job Object 及挂起进程分配流程。

关闭 Desktop 时停止接收任务，让本地任务写入检查点/取消，然后关闭自身后端。重启后可从已保存进度续跑。关闭前请保存编辑器草稿；尚未保存的内存草稿不属于持久化检查点。

Linux 有 Wayland 时优先使用原生 Wayland；X11 仅是连接阶段的回退，不是全局强制设置。针对 NVIDIA 专有驱动，原生 Wayland 使用进程级显式同步兼容设置并保持 DMA-BUF；NVIDIA/X11、NVIDIA/Hyprland 使用独立 DMA-BUF 回退。用户显式设置的图形环境变量优先。

窗口仍异常时，可仅对一次启动尝试以下诊断，不要全局设置：

```bash
WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm desktop
```

已用原生 GTK/WebKit 探针复现 NVIDIA/KDE Wayland 的 `Gdk Error 71`，并确认针对性显式同步设置可以避免该错误。这仅验证该兼容场景，不代表所有 GPU 的性能结论。界面性能测量与正确性测试分开记录。

Rust 后端重写仍暂缓。Desktop 通过共享后端接口复用现有 Python 引擎；Rust 仅负责原生应用能力和进程生命周期。
