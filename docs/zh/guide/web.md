# Web 部署与运维

本文面向部署和运营 Wenyi 的用户。Web 包含浏览器前端、API、独立的流程 Worker 与导出 Worker、PostgreSQL 和 Redis。创建项目、翻译、人工校阅、术语、Review 和导出的共享操作统一见[工作台指南](workspace.md)。

**默认部署不提供独立账号、角色或按用户划分的项目权限。** 可选的共享 token 保护整个服务，不隔离不同用户的数据。本地安装应留在可信网络内；公开服务前必须完成[网络安全配置](#authentication-and-network)。

<a id="quick-start"></a>

## Docker Compose 快速启动

安装 Docker 和 Compose v2，进入准备运行的发布版本工作区：

```bash
# Create the environment file only if it does not already exist.
test -f deploy/.env || cp .env.example deploy/.env
# Edit deploy/.env and supply credentials for the providers you actually use.
cd deploy
docker compose up -d --build
```

默认 Compose 文件会启动全部服务：

| 服务 | 地址或用途 |
|---|---|
| Web | <http://localhost:8080> |
| API / OpenAPI | <http://localhost:8000> / <http://localhost:8000/docs> |
| `worker` | `wenyi:workflows`：解析、译前准备、翻译和 Review |
| `export-worker` | `wenyi:exports`：保存快照后的导出 |
| PostgreSQL 16 / Redis 7 | 默认仅容器网络，不发布宿主端口 |

Web 和 API 端口默认发布到宿主机的所有接口，并非仅回环地址。本地专用安装须限制绑定地址或使用防火墙。示例数据库凭证是开发默认值，不应使用这些凭证公开数据库。

后续运维命令在 `deploy/` 中执行，始终使用相同的 Compose project 名称和覆盖文件：

```bash
docker compose ps
docker compose logs -f api worker export-worker web
docker compose down
```

`down` 停止服务并保留命名卷。不要添加 `-v`，也不要清理仍包含所需数据的卷。

Docker 构建不包含 Git 元数据。构建发布镜像时，在 `deploy/.env` 中将 `WENYI_VERSION` 设为对应包版本；默认 `0.0.0+docker` 表示开发构建。默认使用官方软件源，可选镜像源见下表；带下载缓存的构建方式见 [Web 开发](../development/web.md#docker-builds)。

<a id="configuration-and-dependencies"></a>

## 配置与外部依赖

API 和两个 Worker 共用数据库／Redis 连接、配置挂载和文件数据卷。
当前 Compose 向三个容器注入相同的 `deploy/.env`，将配置只读挂载到 `/app/config.yaml`，
并共享 `/data`。这是部署上的便利，并非每个进程都必须持有全部凭据：
模型调用和 MinerU 解析需要各自密钥，导出 Worker 不需要模型或 MinerU 密钥。

| 设置 | 含义 |
|---|---|
| `DEEPSEEK_API_KEY` 等提供商变量 | 实际模型路由涉及的提供商凭证；自定义连接使用 `api_key_env` 指定的变量名。 |
| `MINERU_API_KEY` | MinerU PDF 解析的独立凭证，不是模型提供商密钥。 |
| `WENYI_CONFIG_FILE` | Compose 挂载的宿主配置文件；默认 `../config.yaml`，相对 `deploy/`。 |
| `WENYI_CONFIG` | 应用进程内的配置路径；Compose 设为 `/app/config.yaml`，本地进程默认为 `config.yaml`。 |
| `DATABASE_URL` / `REDIS_URL` | 应用服务连接。Compose 显式覆盖为内部服务地址，仅修改 `.env` 中这两个值不会改变容器连接目标。 |
| `DATA_DIR` | 上传原文、解析缓存和导出文件；Compose 设为共享 `data` 卷上的 `/data`。 |
| `WENYI_API_TOKEN` / `WENYI_CORS_ORIGINS` | 部署鉴权与允许的浏览器来源，见[网络安全配置](#authentication-and-network)。 |
| `WENYI_VERSION` | 无 Git 元数据的镜像构建所用版本。 |
| `DEBIAN_MIRROR`、`UV_DEFAULT_INDEX`、`NPM_REGISTRY`、`UV_IMAGE` | 可选的软件源及官方 `uv` 镜像覆盖。 |

提供商密钥保留在服务端环境变量中。Web 设置只存变量名，不存原始密钥，也不提供 Desktop 式的手动凭据存储。环境文件应保密，不纳入版本控制。

修改环境变量后，重新创建全部三个后端服务：

```bash
docker compose up -d --force-recreate api worker export-worker
```

单纯重启容器不会重新读取 Compose 的环境文件。有任务运行时，按[升级说明](#upgrades)协调重建。

尚未保存全局设置时，配置文件提供默认值，也是**恢复默认配置**的来源。一旦在 Web 中保存全局设置，生效文档保存在 PostgreSQL：修改挂载的 YAML 不会覆盖已保存设置。项目专属配置仍会应用；启动或恢复任务时会捕获该次执行的有效配置。详见[配置参考](../reference/configuration.md)和[工作台设置说明](workspace.md)。

获取提供商模型列表不消耗模型 token；**测试连接**会发送真实模型请求，可能产生费用。具体操作见工作台指南。

<a id="pdf-services"></a>

### PDF 服务

- **MinerU** 是默认 PDF 解析器。向执行 PDF 解析的进程注入 `MINERU_API_KEY`，并确认其能够访问服务。当前 Compose 通过共享 `.env` 分发此变量；导出不调用 MinerU。解析缓存与原文预览共用。
- **BabelDOC** 需要单独部署 HTTP bridge。将 `pipeline.pdf_backend` 设为 `babeldoc`，并把 `pipeline.babeldoc_bridge_url` 设为 API 和两个 Worker 均可访问的地址。**仓库提供的 Compose 不下载或启动该 bridge。**
- 容器内的 `127.0.0.1` 指该容器，不是宿主机或其他服务。使用可达的服务名或针对 Docker 环境配置的宿主网关，不能假设宿主回环 URL 在 Worker 内也有效。
- Web 后端镜像不提供本地 PDF 渲染引擎。PDF 导出仅供以 BabelDOC 后端保存的项目使用，通过 bridge 回填；更改当前解析器不会转换已有项目。MinerU 及其他书籍项目使用其他受支持的导出格式，见[格式参考](../reference/formats.md)。

<a id="authentication-and-network"></a>

## 认证、CORS 与 TLS

公开服务前：

1. 设置强随机、保密的 `WENYI_API_TOKEN`。HTTP 客户端发送 `Authorization: Bearer …`；Web 界面也会通过 WebSocket 鉴权消息发送 token。持有同一 token 的用户共享整个服务的访问权限。
2. 将 `WENYI_CORS_ORIGINS=*` 改为允许访问 API 的精确浏览器 origin，包含协议与非默认端口，例如 `https://wenyi.example.com`。多个 origin 用逗号分隔，不加空格或 URL 路径。CORS 是浏览器策略，不代替鉴权或项目权限。
3. 在反向代理终止 TLS，通过 HTTPS 提供浏览器访问，同时转发安全 WebSocket。代理需覆盖 `/api` 与 `/ws`，不能保留绕过防护的 API 8000 端口入口。
4. 用网络策略限制直接访问 API、PostgreSQL 和 Redis。不要把开发端口覆盖文件用于公开部署。

默认 Compose 不配置 TLS，也不是多用户授权系统。`WENYI_API_TOKEN` 默认为空，`WENYI_CORS_ORIGINS` 默认为 `*`，这不构成公网安全策略。允许来源的预检请求及鉴权失败响应保留正确的 CORS 行为。自建客户端应遵循 [HTTP 与 WebSocket 契约](../development/web.md#http-contract)。

<a id="backup-and-restore"></a>

## 同时备份 PostgreSQL 与文件数据

| 数据 | 职责 |
|---|---|
| PostgreSQL / `pgdata` | 权威可变状态：项目及配置、章节与段落、术语、Review 证据／检查点、Autofix 发布索引、任务、用量和计时 |
| `DATA_DIR` / `data` | 上传原文、解析资源／缓存及导出产物 |
| Redis / `redisdata` | 队列、实时进度／统计及运维计数；不是译文或用量账本 |

只保存导出书籍或只备份 `DATA_DIR` 都不等于备份项目。Web 不在文件数据目录中另存可变状态的 JSON／SQLite 副本。

以下是**运营方管理的维护流程**，不是产品内置的原子备份功能：

1. 禁止提交新任务，让流程在安全边界暂停，并等待活动导出完成。
2. 停止 Web、API 与两个 Worker，使数据库与文件树在备份期间不再变化。保留 PostgreSQL 运行以执行逻辑备份。
3. 在工作区之外新建受保护的目录，保存 PostgreSQL dump 及完整 `data` 卷归档。默认 Compose 服务名下，在 `deploy/` 执行：

   ```bash
   umask 077
   backup_dir="/absolute/path/to/new-private-backup"
   mkdir -p "$backup_dir"
   docker compose stop web api worker export-worker
   docker compose exec -T postgres pg_dump -U wenyi -d wenyi -Fc \
     > "$backup_dir/postgres.dump" &&
     docker compose run --rm --no-deps -T --entrypoint tar api \
       -C /data -czf - . > "$backup_dir/data.tar.gz"
   ```

4. 确认两条备份命令成功，随备份记录应用版本、配置文件和 Compose 覆盖文件。凭证通过受保护的密钥备份流程单独保存。若运维策略要求保留队列／计数连续性，也应保留 Redis 持久化数据。
5. 执行 `docker compose start api worker export-worker web` 启动已停止的服务。正式依赖备份前，在隔离部署中验证恢复。

数据库与文件归档须成对恢复。使用兼容的 Wenyi 版本，在开放写入前验证原文读取和导出。队列恢复或丢失后，可能需要手动**恢复**流程或重新创建导出；保留 Redis 不能代替 PostgreSQL 与文件备份。

<a id="upgrades"></a>

## 升级与旧版部署

升级前先备份，阅读目标版本的兼容性说明，并完成或暂停活动任务。兼容部署可切换到选定发布版本后运行 `docker compose up -d --build`，再检查服务健康、项目读取和两个队列。

**当前 PostgreSQL schema 不迁移旧 Web 架构的项目、策略或状态数据库。** 不要让新安装直接使用旧数据，并假定启动时会自动转换。保留旧服务及其数据卷，为新版使用独立的 Compose project、数据库和卷，例如：

```bash
COMPOSE_PROJECT_NAME=wenyi-new docker compose up -d --build
```

后续命令也须使用该 project 名称。两套服务同时运行时分配不同的发布端口；否则停止旧服务但不要删除其卷。把原文重新导入新项目不等于迁移译文或历史。独立核实保留结果前，继续保留旧数据。

<a id="troubleshooting"></a>

## 故障排查

| 现象 | 检查方向 |
|---|---|
| 流程一直排队 | 确认 `worker` 运行并消费 `wenyi:workflows`，API 与 Worker 使用同一 PostgreSQL 和 Redis 数据库；查看 `docker compose logs api worker`。 |
| 导出一直排队 | 启动消费 `wenyi:exports` 的 `export-worker`，普通 Worker 不消费导出；同时检查其环境及共享 `/data` 卷。 |
| 缺少模型凭证 | 连接测试检查 API 环境，翻译检查流程 Worker 环境中的对应提供商变量。修改 Compose 的 `.env` 后重新创建受影响容器。未使用的提供商和导出 Worker 不需要模型密钥。 |
| PDF 解析或导出失败 | 检查选定后端、独立凭证和容器内网络可达性；BabelDOC 需单独部署。 |
| 浏览器出现 `401` 或 CORS 错误 | 检查部署 token、精确的浏览器 origin 和反向代理转发。更改提供商密钥不会改变 API 鉴权。 |
| Web 代理返回 `502` | 对比 `http://localhost:8000/health` 与 `http://localhost:8080/api/health`，查看 `docker compose logs web api`。 |
| 构建时软件源返回 `403` | 检查源访问权限；PyPI 可尝试 `UV_DEFAULT_INDEX=https://pypi.org/simple`。清理缓存不能解决源拒绝访问；构建失败后不要以为单独运行 `--no-build` 已更新镜像。 |
| 配置被拒绝 | 按当前配置校验器及参考文档处理，不要假定已弃用字段或旧项目会自动迁移。 |

<a id="interrupted-work"></a>

### 中断的任务

Worker 异常退出后，先恢复服务并等待系统核对已保存任务。模型请求耗时较长本身不代表
任务失联，不要仅因累计耗时较长就删除锁或重置项目状态。

界面确认中断流程已暂停后，使用**恢复**从保存进度继续；中断的导出需重新创建。
恢复没有固定完成时限，数据库或 Redis 故障可能延迟核对。
判定规则统一见[开发者恢复契约](../development/web.md#execution-and-recovery)。

报告故障时保留项目／运行身份、响应中的 `X-Request-ID`（服务端错误还有 `X-Error-ID`），以及去除凭证和私有正文后的相关日志。[运维指标](../development/web.md#metrics)可帮助监控服务，但不是 token 用量或费用账本。
