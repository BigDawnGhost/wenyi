# Web 全栈使用说明

本分支在原有 CLI 之外提供 Web 模式：前端 + FastAPI + Arq Worker + Postgres + Redis。

CLI 本地翻译仍可单独使用，无需任何容器（见 [使用指南](usage.md)）。

## 前置依赖

| 方式 | 需要 |
|------|------|
| Docker 一键部署 | [Docker](https://docs.docker.com/get-docker/) + Compose v2 |
| 本地开发 | Python 3.10+、[uv](https://docs.astral.sh/uv/)、Node 20+、[pnpm](https://pnpm.io/)、Postgres 16、Redis 7 |

## 配置

### 1. 环境变量

复制样例并填写密钥：

```bash
cp .env.example deploy/.env
```

编辑 `deploy/.env`，至少设置：

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | **必填**。LLM 鉴权；不填则翻译会失败 |
| `WENYI_API_TOKEN` | 可选。填写后 HTTP 请求需带 `Authorization: Bearer <token>`（`/health`、`/ws/` 除外） |
| `WENYI_CORS_ORIGINS` | 可选。CORS 源，逗号分隔；默认 `*` |
| `DATABASE_URL` / `REDIS_URL` | Compose 内已写死服务名；仅本地开发时需改成 `localhost` |
| `DATA_DIR` | 上传原件与导出成品目录；Compose 内为容器卷 `/data` |

### 2. 内核默认值（可选）

仓库根目录的 `config.yaml` 控制模型档位、流水线开关、切分参数等。Web Worker 会读取该文件作为默认，再由项目策略覆盖。换其它 OpenAI 兼容服务时，改 `llm` 段并设置对应的 API Key 环境变量即可。

## 启动全套服务（推荐）

在仓库根目录执行：

```bash
cp .env.example deploy/.env   # 首次：填入 DEEPSEEK_API_KEY
docker compose -f deploy/docker-compose.yml --profile full up --build
```

或进入 `deploy/` 后：

```bash
docker compose --profile full up --build
```

启动后：

| 服务 | 地址 |
|------|------|
| Web UI | http://localhost:8080 |
| API / OpenAPI | http://localhost:8000 、http://localhost:8000/docs |
| Postgres / Redis | 仅容器内网，不对外暴露 |

停止：`Ctrl+C`，或加 `-d` 后台运行后用 `docker compose -f deploy/docker-compose.yml --profile full down`。

### 仅后端（无前端）

适合远程 CLI 或自建前端：

```bash
docker compose -f deploy/docker-compose.yml --profile server up --build
```

## 本地开发（可选）

基础设施仍建议用 Compose 起库与缓存，应用进程在宿主机跑：

```bash
# 1. 只起 Postgres + Redis（可用 full/server 中的依赖，或自行安装）
docker compose -f deploy/docker-compose.yml --profile server up postgres redis -d

# 2. Python 依赖
uv sync --all-packages
export DEEPSEEK_API_KEY=sk-...
export DATABASE_URL=postgresql://wenyi:wenyi@localhost:5432/wenyi
export REDIS_URL=redis://localhost:6379/0
export DATA_DIR=./data

# 3. 前端依赖
pnpm install

# 4. 三个进程（各开一个终端）
pnpm dev:api      # http://localhost:8000
pnpm dev:worker   # Arq 消费翻译/导出任务
pnpm dev:web      # http://localhost:5173（Vite 已代理 /api、/ws → 8000）
```

说明：`--profile server` 会一并定义 api/worker；若只想起数据库，也可本机安装 Postgres/Redis，并保证上述 `DATABASE_URL` / `REDIS_URL` 可连。

## 日常使用概要

1. 打开 Web UI，创建项目并上传 EPUB / FB2 / TXT。
2. 选择翻译策略（快速出稿 / 标准翻译 / 精翻，或自定义步骤）。
3. 开始翻译后可在进度页看实时状态；术语、审校、风格、导出等在对应页面操作。
4. 导出完成后在导出页下载成品。

API 契约以运行中的 `/openapi.json` 为准；需要时可生成前端类型：

```bash
pnpm gen:schema   # 要求 API 已在 :8000 运行
```
