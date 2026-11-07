# Desktop 开发

本文说明本地引擎、原生宿主及其安全契约。安装与日常操作见
[Desktop 使用指南](../guide/desktop.md)，打包和升级见[发行](releasing.md)，
共享 UI 规则见[前端开发](frontend.md)。

## 从源码运行

准备 Python 3.10+、`uv`、Node 22、pnpm 9、Rust stable 和
[Tauri 平台前置依赖](https://v2.tauri.app/start/prerequisites/)。
Debian/Ubuntu 还需安装 `libdbus-1-dev`，用于原生托盘可用性检查。
这些是开发环境要求；发行包自带 Python 引擎。

在仓库根目录执行：

```bash
uv sync --locked --package wenyi-desktop --group dev
pnpm install --frozen-lockfile
pnpm desktop
```

选择独立的应用数据根目录：

```bash
pnpm desktop --data-dir /path/to/desktop-test-data
```

**`--data-dir` 选择整个数据根目录**，包括目录库、设置、日志、锁和插件运行时。
它不会搬迁或发现已有项目，也没有隐藏的共享目录库。自动化测试应显式注入临时根目录、
临时外部项目库和 fake 凭据存储：系统凭据库不在数据根目录内。
不要把真实项目库当作可丢弃的开发数据。

[启动脚本](../../../scripts/dev/desktop.mjs#L55)解析 Git 版本、构建 UI，再启动原生
debug 应用。默认使用仓库 `.venv`，也可通过 `WENYI_DESKTOP_PYTHON` 指定开发解释器。
不提供远程 `--url` 模式，也不会静默回退到 Web 服务。仅启动 UI 的 Vite 服务不会提供
原生引擎或 IPC 实现。

直接调用 Cargo 前先构建前端，因为 Tauri 在编译时嵌入其产物。例如：

```bash
pnpm -C apps/desktop/frontend build
cargo test --locked --manifest-path apps/desktop/Cargo.toml
```

## 平台端口与连接身份

| 层 | 职责 |
| --- | --- |
| 原生宿主 | 窗口/托盘生命周期、工作区激活、子进程所有权、原生文件能力、诊断持久化及应用更新 |
| Desktop Python 适配 | SQLite 目录库、项目存储、凭据、本地任务监管及 Desktop 专属 HTTP 接口 |
| 共享 backend | HTTP 契约和应用服务，通过显式的 repository、storage、telemetry、credential、execution 端口接入平台 |
| 共享 Core | 经 `Storage`/`ArtifactStorage` 执行翻译及文档服务，不导入 Desktop 或 HTTP 代码 |
| 共享 UI | 经平台服务注入复用页面及组件，不导入两端 app |

[Desktop 装配](../../../apps/desktop/backend/wenyi_desktop/main.py#L113)为每个应用创建
`BackendContext`，注入本地实现。HTTP 请求、worker 入口和线程回调必须保持正确的
上下文身份。共享 backend 不得通过进程全局标志选择平台，也不得导入
PostgreSQL/Redis/Arq/keyring 适配。仓库整体依赖方向见[架构](architecture.md)。

引擎只监听随机 loopback 端口，每次启动使用新的、仅内存保存的 bearer token。
[原生进程协议](../../../apps/desktop/src/process.rs#L65)在公布连接前校验
`WENYI_READY` 的协议版本、端口及准确的自有子进程 PID。UI 必须等待就绪；
启动失败不意味着可以尝试其他端点。端口和 token 不写入项目。

Windows 上的 debug Python 覆盖值必须是已存在的可执行文件路径，不能是 PATH
命令或包装器。对于 CPython venv，宿主通过 `__PYVENV_LAUNCHER__` 启动基础解释器，
使导入保留 venv 身份，同时引擎仍是直接持有的子进程。发行 sidecar 直接启动。
不得根据就绪消息中的任意 PID 授权终止其他进程。

凭据由 Desktop 凭据适配管理：可用时使用系统凭据库，否则仅存入会话内存并明确提示
降级。禁止将密钥写入 SQLite、YAML/JSON、浏览器存储或诊断。可选 BabelDOC 插件保持
独立进程并通过 HTTP 通信，不把其 AGPL 运行时加入主应用或 Core 依赖图。

## 数据根目录所有权

选定的单一根目录拥有应用本地数据。各平台默认路径与手动备份步骤见
[Desktop 指南](../guide/desktop.md#数据位置与备份)。

| 位置 | 所有权与内容 |
| --- | --- |
| `<data-root>/catalog.sqlite3` | 项目/任务目录、设置与不透明凭据引用 |
| `<data-root>/projects/` | 默认项目库，保存项目 SQLite 状态、原文、解析缓存及导出 |
| `<data-root>/logs/`、`locks/` | 诊断、进程准入及激活记录 |
| `<data-root>/runtime/` | 插件运行时/会话及 Windows/Linux WebView 数据 |
| `<data-root>/backups/` | 应用内更新生成的已校验快照 |
| 外部项目库 | 显式选择的本地位置，按项目记录在目录库中 |

路径名称集中在
[Python 路径模块](../../../apps/desktop/backend/wenyi_desktop/paths.py#L38)与
[原生路径模块](../../../apps/desktop/src/paths.rs#L21)，跨语言契约必须同步。
现有 API 中的 `workspace` 就是选定的数据根目录，不是第二个存储位置。
系统凭据库与 macOS 管理的 WKWebView 数据仍在根目录之外。

项目必须经
[目录库的项目路径解析器](../../../apps/desktop/backend/wenyi_desktop/local_backend.py#L157)
定位，不能直接拼接当前根目录和项目 ID。修改默认项目库不会搬迁已登记项目；
目录名中的可读后缀不能覆盖项目标记提供的身份。

数据根目录和项目库均应位于本地磁盘。网络共享和云同步目录不受支持：
SQLite 锁、占位文件及分时复制的 WAL 可能破坏一致性。应用目前不会检测所有这类位置。
[原生布局准入](../../../apps/desktop/src/paths.rs#L174)检查实际可执行文件目录与数据根目录，
源码/解包启动及 `--data-dir` 同样受约束。即使目标还不存在，也先解析已有祖先路径；
目录链接和 Windows 短路径别名不能掩盖重叠。

原生单实例准入发生在创建 WebView 和 Python 引擎之前。所有权按规范化后的数据根目录
判定，路径别名也属于同一根目录，不按可执行文件或版本判定。再次启动只激活现有窗口，
不替换 WebView。原生 `locks/.desktop-instance.lock` 与引擎 `locks/.desktop.lock`
是两个独立的 OS 所有权检查。发现文件存在不代表进程仍在运行；禁止删锁绕过活跃所有者。

[目录库事务辅助方法](../../../apps/desktop/backend/wenyi_desktop/local_backend.py#L129)
使用短 SQLite 事务、WAL 和有界 busy timeout。只读状态查询读取已提交快照，不预占写锁。
模型请求、凭据库访问、压缩和文件清理期间不能持有目录库写事务。领域状态仍由各项目
注入的 `SqliteStorage` 管理；目录库描述调度和已登记项目，不是翻译检查点的权威来源。

## 目录库准入与项目导入

[目录库初始化](../../../apps/desktop/backend/wenyi_desktop/catalog.py#L12)在应用存储建表前
检查 SQLite `application_id` 与 `user_version`。只有空数据库可以初始化；无法识别或
不支持的目录库直接拒绝，不接管其中的记录。当前目录库格式版本为 2。

启动时不扫描旧位置、不迁移目录库、不合并旧设置，也不从项目文件夹重建空目录库。
选择新的根目录会显示空列表，旧目录保持不动。恢复依赖一致备份，不依赖自动重新发现。

[显式项目导入](../../../apps/desktop/backend/wenyi_desktop/project_import.py#L138)
仅接受用户主动选择的当前格式项目：

- 要求有效的 `project.json` 且 `schema_version: 2`、可读的已存状态，以及唯一匹配
  已存 SHA-256 的原文。缺少标记或旧格式标记不会迁移。
- 拒绝链接文件、删除标记、重复项目身份和已占用目标，不猜测所有权。
- 先复制到 `<data-root>/projects/` 下的暂存目录并校验文件哈希，再发布目录及目录库记录；
  两者都成功后才删除选定的原目录。导入不等于旧格式迁移或旧目录库恢复。

项目数据库另外通过 `PRAGMA user_version` 记录版本。
[模式校验](../../../packages/core/wenyi_core/storage/sqlite.py#L193)对未知新版同时拒绝读写；
目录记录保留并标记不可用，其他项目不受影响。为支持的 version-zero 数据库标记版本，
不能绕过目录库或项目标记的准入检查。

删除与恢复必须共享目录所有权规则。保留
[删除标记](../../../apps/desktop/backend/wenyi_desktop/local_backend.py#L795)顺序：
先记录删除意图，再清理拥有的文件，最后删除目录库记录。中断删除应可重试；
启动和显式导入均不能复活已有删除标记的目录。

## 并行工作流与退出排空

[LocalRuntime](../../../apps/desktop/backend/wenyi_desktop/local_runtime.py#L191)
监管跨项目独立执行的解析、准备、翻译、章节翻译和 Review。派发前先持久化 job/run
身份和配置快照，准入失败需补偿。同一项目的冲突写入仍然互斥。

每次调用独占一个异步监管任务和
[单线程执行器](../../../apps/desktop/backend/wenyi_desktop/local_runtime.py#L177)，
跨线程复制上下文。不能改成共享的固定工作流线程池；默认 executor 必须留给控制请求和
快照读取。Desktop 没有工作流数量门禁，但不承诺无限内存、线程、吞吐或供应商容量。
单客户端限流和重试策略继续生效，共用凭据不等于具备账号级全局限流。
导出保留独立有界队列及两个消费者，遥测也保持有界。

运行时所有权覆盖**已接纳/待执行、运行中及取消/排空中**的工作，包括尚未进入任务
适配器的监管任务。目录库已是终态，不等于运行时已经空闲。

1. 调度前登记所有权；重复派发已持有或已终态的 run 不得再次执行。
2. 每个 run 独立创建客户端、停止信号和存储。某个 run 暂停/失败，包括线程分配失败，
   不得取消其他项目。
3. 超时或退出时，异步 worker **只取消一次**。
   [共享执行 runner](../../../packages/backend/wenyi_backend/execution/runner.py#L207)
   发出协作停止信号，并 shield 同步操作直至检查点写入结束。重复取消会打断这段等待。
4. 先停止准入，再排空。尚未进入执行的已接纳工作持久化为中断/可继续状态，不开始模型
   工作；worker 执行第一条指令前被取消的情况也要处理。
5. 等待活跃写入者、监管任务、导出和进度持久化全部结束后才释放工作区锁。即使目录库
   已是终态，更新准入仍须检查 pending/draining 所有权。
6. 崩溃后将孤儿任务恢复为适当的可继续/错误状态，不自动重启全部中断工作。

不强杀 Python 线程；尚未退出的线程继续保留所有权。原生宿主可在宽限期结束后终止无响应
的子进程，但这是强制恢复，不证明检查点已刷新；保留 WAL，并在下次启动按中断运行恢复。

隐藏/最小化窗口不是退出。后台活动状态减少周期性 UI 工作，引擎和进度订阅仍保持运行。
恢复窗口时重新核对已保存状态，不丢弃编辑草稿；普通失焦不是最小化。

## 诊断契约

诊断是 Desktop 自有、尽力保存的观察，**不是项目历史、用量账本或项目备份**。
清理日志不得影响检查点、Review 产物或记账，不自动上传任何报告。

### 安全入口与关联身份

[Python 投影器](../../../apps/desktop/backend/wenyi_desktop/diagnostics.py#L135)与
[原生校验器](../../../apps/desktop/src/diagnostics.rs#L216)只接收已注册事件、安全枚举、
有界数值和不透明身份。在入队之前处理，导出时再次校验；字段名在白名单中并不意味着
任意文本都安全。

不得收集原文/译文、提示词、模型响应、术语/Review 内容、凭据、请求正文、原始配置、
原始异常消息、traceback 或文件系统路径。安全故障观察可包含分类后的异常/cause 类型、
已知 OS/SQLite 错误码及白名单应用模块/行号，不包含异常字符串或调用栈转储。
不要把任意 stdout/stderr 或第三方日志器接入持久化 sink。

原生进程是**唯一文件写入者**。Python 通过私有管道发送有界 `WENYI_DIAGNOSTIC`
消息；`WENYI_READY` 保持独立协议帧，任意 stderr 仍被丢弃。前端 IPC 只接受可信窗口
提交的已注册事件名，不接受任意载荷或文件读取请求。复用任务/run/模型调用身份，
显式向线程传播上下文，并在操作结束后复位。Desktop 自行生成 request/error ID，
不持久化调用方提交的 request ID。导出对 session/project/run/call 引用使用包内别名。

`task_finished` 表示适配器返回，也可能是暂停；持久任务才是权威。非正常退出标记不证明
发生崩溃，强制清理和观察到的引擎退出也不能误标为原始故障。

### 有界保存与导出

普通采集为 INFO 及以上。临时详细采集最多 30 分钟，可提前结束，重启后复位；它不放宽
隐私约束，也不改变翻译配置/缓存身份。

原生持久化使用 `<data-root>/logs/`；磁盘保存失败时仅保留有界内存缓冲，不回退到另一个
平台目录。文件在 10 MiB 或 24 小时轮转，总上限
200 MiB，清理最后写入超过七天的自有文件，同时保护活跃/刚写入文件。外来文件计入空间
占用，但不读入报告，也不由保留策略或清理操作删除。日志失败不能阻塞翻译或掩盖原始
异常；丢弃与降级证据必须可见。

[原生诊断保存](../../../apps/desktop/src/native_export.rs#L351)必须：

1. **先**选择并准入目标位置，包括覆盖确认，**再**收集或创建归档。取消选择不创建
   归档或书籍导出任务。
2. 只读取自有诊断文件的完整行水位，拒绝链接和不安全目标；压缩时不持有项目锁或
   目录库写事务。
3. 再次校验安全字段，应用包内别名，明确缺失来源、丢弃/损坏记录、保留覆盖范围及
   大小限制导致的省略。
4. 校验 ZIP，在目标同目录写临时文件并原子发布。取消/失败保留已有目标，取消不算错误。

当前报告仅支持**本次启动、最近 24 小时、最近七天，范围为整个工作区**，没有项目/run
筛选或自定义时间范围。报告包含 `manifest.json`、`report.md`、`system.json` 及可用的
分来源 JSONL 日志，不含项目事件/配置/用量摘要或数据库副本。记录选择预算为 20 MiB，
最终 ZIP 上限为 25 MiB。`complete` 只表示对可用来源未发现采集损失，不承诺完整历史
覆盖，也不证明找到了根因。

[原生诊断控制](../../../apps/desktop/src/diagnostics_ui.rs#L152)必须在 Python 引擎或
React 树不可用时仍可使用。导出与清理互斥，日志写入可继续。完全无法启动的原生程序
不能依靠自身导出。

## 验证

使用临时数据根目录及外部项目库、fake 凭据存储/client 和同步屏障，不读取真实书籍或
调用付费模型/解析服务。覆盖根目录隔离、不支持的目录库拒绝、当前格式导入且启动不做
发现、并行项目、同项目互斥、上下文身份、重复投递、线程分配失败、pending/draining
退出、无重复计费的暂停/续跑、新模式拒绝及诊断隐私/故障路径。

安装完整 Python 开发工作区后：

```bash
uv run --no-sync pytest -q -n auto --dist loadgroup apps/desktop/backend/tests
uv run --no-sync pytest -q -n auto packages/core/tests/test_architecture_boundaries.py packages/core/tests/test_orchestrator_contract.py packages/core/tests/test_storage_injection.py
uv run --no-sync ruff check apps/desktop/backend
uv run --no-sync ruff format --check apps/desktop/backend
git diff --check
```

状态、锁和恢复变更还需完整 Python 回归集及受影响的共享 backend 测试。
原生变更运行 Rust 测试；共享 UI 变更按[前端开发](frontend.md)验证两端。
诊断隐私测试共用 [Desktop 夹具](../../../apps/desktop/diagnostic-fixtures.json#L1)。
浏览器 IPC mock 不能验证发行包的系统对话框、托盘、进程所有权或 OS 安装行为；
各支持目标必须分别验收，缺少的集成服务应单独说明。
