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

本地 Linux x86-64 预览构建产物为 **50.34 MiB `.deb`**、**50.35 MiB `.rpm`** 和 **143.02 MiB AppImage**。AppImage 额外包含 Linux 运行库；这些数字是压缩安装包大小，不是内存占用。原生外壳目前使用开发版本号 `0.0.0`；这些未签名预览包不是已发布的正式版本。

已在 KDE Wayland 下使用临时工作区和无效的开发 Python 路径启动 AppImage：包内引擎成功启动，带鉴权的 loopback 请求正常，关闭原生窗口后引擎退出并被回收。独立冻结引擎检查还覆盖了离线合成 TXT 上传、解析、预览及新旧格式事件读取。真实文件管理器拖放、系统保存对话框、Windows/macOS 运行及可移动介质行为仍需平台验收。

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
- HTML 明确保存为 **HTML + 资源（ZIP）**，文件名以 `.html.zip` 结尾。先解压再打开 HTML，保留相对图片及媒体链接；归档仅包含该次已发布 HTML 及其配套资源。
- Desktop 拒绝内部工作区目标及本次应用会话记录的原文文件身份/路径。源保护记录有上限，仅存于内存，不长期占用原文文件句柄。保存期间不要用其他应用替换同一目标：覆盖是原子文件替换，不是跨进程的条件交换。Web 继续使用浏览器下载。

## 启停与渲染

启动和关闭使用无加载文案的安静过渡。启动错误仍明确显示，并提供重试/重新加载操作。界面请求等待本地引擎就绪握手，不回退到远程端点。后端仅监听随机分配的回环端口，每次启动生成新的内存 token。

关闭 Desktop 时停止接收任务，让本地任务写入检查点/取消，然后关闭自身后端。重启后可从已保存进度续跑。关闭前请保存编辑器草稿；尚未保存的内存草稿不属于持久化检查点。

Linux 有 Wayland 时优先使用原生 Wayland；X11 仅是连接阶段的回退，不是全局强制设置。针对 NVIDIA 专有驱动，原生 Wayland 使用进程级显式同步兼容设置并保持 DMA-BUF；NVIDIA/X11、NVIDIA/Hyprland 使用独立 DMA-BUF 回退。用户显式设置的图形环境变量优先。

窗口仍异常时，可仅对一次启动尝试以下诊断，不要全局设置：

```bash
WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm desktop
```

已用原生 GTK/WebKit 探针复现 NVIDIA/KDE Wayland 的 `Gdk Error 71`，并确认针对性显式同步设置可以避免该错误。这仅验证该兼容场景，不代表所有 GPU 的性能结论。界面性能测量与正确性测试分开记录。

Rust 后端重写仍暂缓。Desktop 通过共享后端接口复用现有 Python 引擎；Rust 仅负责原生应用能力和进程生命周期。
