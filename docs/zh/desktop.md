# Wenyi Desktop

[English](../desktop.md) · [Web 部署与开发](web.md)

Desktop 是使用 Tauri、React 和现有 Python 翻译内核的**本地翻译应用**。它自行启动私有后端，使用独立 SQLite 工作区，不需要部署 Web、PostgreSQL、Redis 或 Docker。发行包包含 Python 运行时，用户无需另外安装 Python。

翻译仍需访问所配置的模型提供商。可选 MinerU、BabelDOC 服务保留各自的使用条件；“本地应用”不意味着外部模型或文档服务可以离线使用。

## 快速开始

[![下载 Desktop](https://img.shields.io/badge/Desktop-download-D4B56A?style=flat-square&labelColor=00263D)](https://github.com/BigDawnGhost/wenyi/releases)

### 1. 下载 Desktop

前往 [GitHub Releases](https://github.com/BigDawnGhost/wenyi/releases)，选择适合系统的 `wenyi-desktop-<版本>-<平台>-<架构>` 文件：

| 平台 | 安装包 |
|---|---|
| Windows x64 | `.exe` 安装程序 |
| Linux x64 | `.AppImage`、`.deb` 或 `.rpm` |
| macOS Apple Silicon | `.dmg` |

Desktop 安装包直接提供，不再套一层 ZIP。使用 AppImage 时，先在文件属性中允许作为程序执行，再打开。发行包已包含翻译引擎，无需安装 Python 或部署服务器。

平台要求和签名状态请以发行说明为准。如需从源码运行，参见[从源码运行](#从源码运行)。

### 2. 连接模型

打开**设置 → API 供应商与模型**，选择提供商，配置模型及所需的自定义接口地址，然后保存连接配置。在密码输入框中填写并保存 API Key。

Desktop 优先使用系统凭据库；不可用时，界面会提示密钥仅在当前会话内保存，重启后需要重新输入。本地工作区不代表模型处理完全离线：除非使用本地模型服务，文本仍会发送给所配置的提供商。

### 3. 创建翻译项目

点击项目列表中「设置」上方的悬浮 **+**，打开居中的「创建项目」对话框。列表仍在背后显示但不能操作；直接访问 `/projects/new` 也打开同一对话框。焦点首先进入「项目名称」，Tab 不会离开对话框；下拉框打开时 Escape 先关闭下拉框。点击「取消」、关闭按钮、遮罩，按 Escape 或浏览器后退均返回列表，不创建项目，焦点回到 **+**。关闭时释放已选原文的原生资源，再次打开是全新表单。窄屏下表单可滚动，顶部关闭按钮始终可见。

拖入支持的书籍文件或点击「浏览文件」选择文件，再选择源语言和目标语言。源语言可自动检测。可选择**标准翻译**；书籍还支持先生成三稿、再综合成稿的**三译合润**，但会增加模型用量。上传中不能更换原文或关闭对话框，关闭不是取消任务的入口。上传失败保留表单和已选原文；原生上传失败需重新拖入文件，以更新单次使用授权后重试。创建成功后进入项目总览，解析或可选的译前准备继续在后台执行。旧链接 `/projects/new?project=<id>` 保留只读原文预览／恢复操作。

上传中仍可通过浏览器后退离开，但这不会取消请求。原文资源保留至请求结束后再释放。项目可能在后台创建，完成后不会强制跳离项目列表。

在项目页面启动翻译。文译会解析原文、准备全书上下文并分批翻译，在界面中展示进度、用量和已完成章节；可按需配置润色和全书审校。

### 4. 校阅并保存

对照原文检查译文，编辑段落、查看版本记录并处理审校问题。审校可以将修订写回译文；只想查看建议时，请关闭自动修复。Desktop 与 Web 共用这些工作台页面，截图见[界面预览](README.md#界面预览)。

选择导出格式，并在支持时选择双语版。Desktop 会先打开系统保存对话框，确认位置后才开始导出；取消不会创建导出任务。HTML 以包含页面和资源的 ZIP 保存，阅读前请先解压。

### 稍后继续

已完成批次会保存在本地工作区。重新打开 Desktop，进入原项目即可从检查点继续。关闭应用前，请先保存正在编辑的校阅内容。工作区位置和备份方法见[独立数据](#独立数据)。

## 界面语言

界面默认使用英语。打开全局 **设置 → 界面语言**，可选择 **English** 或 **简体中文**。
选择立即改变界面标签，不修改书籍的源语言、目标语言、内容或模型生成的分析。

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

Debian/Ubuntu 还需安装 `libdbus-1-dev`，用于原生托盘可用性检查
（`sudo apt-get install libdbus-1-dev`）；其他 Linux 发行版安装对应的 D-Bus 开发包。

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

这些 workflow 不签名或公证安装包；平台签名与终端用户安装验证仍是发行前的责任。

已在 KDE Wayland 下使用临时工作区和无效的开发 Python 路径启动 AppImage：包内引擎成功启动，带鉴权的 loopback 请求正常，旧版关闭即退出流程的引擎退出与回收已验证。独立冻结引擎检查还覆盖了离线合成 TXT 上传、解析、预览及新旧格式事件读取。当前托盘流程另在 KDE Wayland 下使用 debug 原生构建、生产 UI、源码 Python 引擎和临时空工作区验证：最小化/恢复及关闭到托盘/恢复期间引擎保持运行，显式托盘退出后引擎被回收。打包后的托盘行为、真实文件管理器拖放、系统保存对话框、Windows/macOS 运行及可移动介质行为仍需平台验收。

## API 密钥

从项目列表右下角悬浮**设置**图标进入独立设置页。分类为界面语言（`/settings`）、API 供应商与模型（`/settings/providers`）、新项目默认配置（`/settings/defaults`）和高级 YAML 配置（`/settings/advanced`）；**项目列表**返回首页。分类导航不随项目侧栏收起，切换分类保留未保存配置草稿。所有配置分类均提供保存、校验与恢复默认。

项目与设置侧栏的底部统一只保留**项目列表**。项目列表中的悬浮按钮按上方**创建项目**、下方**设置**排列。

打开**设置 → API 供应商与模型**，配置提供商、模型及可选 base URL，先保存连接配置，再在对应密码框输入并保存 API key。

原生凭据控件独立加载，进入设置页时不会因其加载而将整页切成空白占位。

- Desktop 自动优先使用受支持的系统凭据库：通过 `keyring` 接入 Keychain、Windows 凭据库、Secret Service 或 KWallet。
- 凭据库不可用或写入失败时，密钥**仅在本次会话的内存中保留**，界面明确提示下次启动需要重新输入。无需选择存储方式，也不会回退为明文文件。
- 已保存密钥不会回显；输入框留空不会清除或替换已有密钥。
- 高级选项支持指定环境变量名。没有选中手动凭据时，留空使用提供商默认变量名；明确指定变量后不会回退到其他变量或连接的密钥。在应用外修改继承环境后需要重启 Desktop。
- 使用**清除手动密钥**移除密钥，或使用明确的“清除并改用环境变量”操作。缺失的手动/会话密钥不会自动切换为环境变量密钥。

凭据变更对新建客户端生效；运行中的任务保留配置和凭据快照。重命名连接会迁移凭据引用，删除连接后新连接不能继承它的密钥。

SQLite 仅保存来源模式和不透明凭据引用。密钥不会保存到 YAML/JSON、项目状态、浏览器存储或 API 响应。Web 和 CLI 保留原有环境变量方式。

**检查本地可用性**只检查本地配置，不联系提供商，也不发送模型请求验证密钥。

## 导入与保存

- 从文件管理器将支持的文件拖入「创建项目」对话框的原文区域，或使用「浏览文件」。拖入只选择文件，点击**创建项目**才开始上传。原生选择是短时、单次使用授权，不是通用文件系统权限；原生上传失败或授权过期后需要重新拖入。表单保留已填内容，重新选择文件后才能再次创建。
- 原生拖拽事件和高 DPI 坐标修复需重启更新后的原生程序；仅刷新前端不会更新 Rust 事件处理器。
- Desktop 导出先显示原生目标位置选择框，选好后才创建导出任务。取消选择不会创建任务，也不会写入输出文件。
- 已完成的历史条目提供**另存为…**。保存以流式方式写入目标目录中的临时文件，仅在完成后正式发布。覆盖现有文件必须确认；传输失败保留原目标文件。
- 导出历史保留最近五个已完成条目。较旧条目立即从历史中移除，已开始的保存仍可读取。文件下载会延迟删除到最后一个流关闭，包括取消或断连；HTML ZIP 使用独立的临时归档。清理失败时，会在后续导出发布或重启时重试。
- HTML 明确保存为 **HTML + 资源（ZIP）**，文件名以 `.html.zip` 结尾。先解压再打开 HTML，保留相对图片及媒体链接；归档仅包含该次已发布 HTML 及其配套资源。
- Desktop 拒绝内部工作区目标及本次应用会话记录的原文文件身份/路径。源保护记录有上限，仅存于内存，不长期占用原文文件句柄。保存期间不要用其他应用替换同一目标：覆盖是原子文件替换，不是跨进程的条件交换。Web 继续使用浏览器下载。

## 启停与渲染

启动和关闭使用无加载文案的安静过渡。启动错误仍明确显示，并提供重试/重新加载操作。界面请求等待本地引擎就绪握手，不回退到远程端点。后端仅监听随机分配的回环端口，每次启动生成新的内存 token。

Windows 的 debug Python 覆盖值必须是现有可执行文件路径，不能是 PATH 命令或包装器。对于 CPython venv，Desktop 读取 `pyvenv.cfg`，直接启动基础 `python.exe` 并设置 CPython 的 `__PYVENV_LAUNCHER__` 提示。这与 CPython venv 重定向器使用相同机制：导入环境和 `sys.executable` 仍属于 venv，但引擎本身成为原生应用持有的子进程。就绪 PID 仍必须与该子进程完全一致；就绪数据不能授权打开或终止其他 PID。venv 配置无效时启动失败，不回退到重定向器。打包后的 onedir 引擎仍直接启动，不发现或依赖外部 Python/venv。这个单进程契约避免了仅为管理重定向器后代而引入 Windows Job Object 及挂起进程分配流程。

### 后台运行

- 最小化窗口不会停止本地任务。点击关闭按钮会隐藏到系统托盘，而不是退出应用。
  使用托盘菜单中的 **Show Wenyi** 恢复窗口；macOS 的 Dock 重新打开操作也会恢复窗口。
- 托盘创建失败时，关闭按钮改为最小化，不会隐藏成无法找回的窗口。
  仍可通过任务栏/Dock 操作。
- 窗口隐藏，或平台报告窗口已最小化后，暂停 UI 周期轮询、进度事件触发的刷新和实时用时计时器。
  进度订阅与 Python 引擎继续运行，不取消已发出的请求或正在进行的原生保存。
  恢复窗口时立即同步已保存状态，包括后台完成的任务。普通失焦不视为最小化。
- 隐藏会保留 WebView、未保存的编辑器草稿和仅会话内存中的凭据。
  这减少的是无用 UI 工作，不会释放 Python 引擎或 WebView 的常驻内存，
  也不会阻止计算机休眠。

Linux/GTK Wayland 无法可靠地向应用报告合成器侧的最小化状态。
此时任务仍继续运行，但 UI 轮询和计时器可能继续工作。
需要可靠触发后台降耗时，请使用关闭到托盘；不会把普通失焦误判为最小化。
托盘恢复会重新映射同一个原生窗口，兼容 GTK/Wayland 的恢复限制，不替换 WebView 或其中的草稿。

Linux 和 Windows 隐藏原生窗口顶部菜单栏；macOS 保留系统应用菜单。
此策略在应用重启后生效。托盘中的 **Show Wenyi**、**Quit Wenyi** 和退出快捷键仍保留。

使用托盘、macOS 应用菜单中的 **Quit Wenyi** 或退出快捷键才会真正退出。
显式退出时停止接收任务，让本地任务写入检查点/取消，然后关闭自身后端。
重启后可从已保存进度续跑。退出前请保存编辑器草稿；尚未保存的内存草稿不属于持久化检查点。
后台翻译仍会向配置的模型服务发送请求，直到任务完成或被暂停，期间可能继续产生用量费用。

Linux 有 Wayland 时优先使用原生 Wayland；X11 仅是连接阶段的回退，不是全局强制设置。针对 NVIDIA 专有驱动，原生 Wayland 使用进程级显式同步兼容设置并保持 DMA-BUF；NVIDIA/X11、NVIDIA/Hyprland 使用独立 DMA-BUF 回退。用户显式设置的图形环境变量优先。

窗口仍异常时，可仅对一次启动尝试以下诊断，不要全局设置：

```bash
WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm desktop
```

已用原生 GTK/WebKit 探针复现 NVIDIA/KDE Wayland 的 `Gdk Error 71`，并确认针对性显式同步设置可以避免该错误。这仅验证该兼容场景，不代表所有 GPU 的性能结论。界面性能测量与正确性测试分开记录。

Rust 后端重写仍暂缓。Desktop 通过共享后端接口复用现有 Python 引擎；Rust 仅负责原生应用能力和进程生命周期。
