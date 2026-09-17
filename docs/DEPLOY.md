# Ai Lubricant 部署指南

本文件是 Ai Lubricant **部署唯一权威文档**。历史 README 里的「Qwen 反向代理 / config.json」叙事已废弃，以本文件为准。

## 架构概览

服务由**两个独立 Python 进程**组成，共用同一份 `.env`、同一套 PostgreSQL / Redis：

| 进程 | 入口 | 服务器 | 协议 | 端口 | 可达性 |
|---|---|---|---|---|---|
| 数据服务 | `python main.py` | uvicorn | HTTP/1.1 + WebSocket + SSE | `8001` | 对外唯一入口 |
| 控制服务 | `python -m node_server` | Hypercorn + h2c | 先验式 HTTP/2 明文 | `8003` | 仅节点与数据服务内部可达 |

**谁连谁**：

- 浏览器 / App / 终端 / 文件 / 会话客户端 → 数据服务（`:8001`），不直连控制服务。
- 节点从各自机器拨到控制服务（`node_server_public_url`，`:8003`）。
- 数据服务 → 控制服务（`agent_compose_base_url`），用 `node_control_token` 鉴权。

依赖中间件：

| 服务 | 用途 | 必需 |
|---|---|---|
| PostgreSQL | 业务配置（渠道/模型/api_keys 等） | 是 |
| Redis | 运行态/冻结态真相源（账号池、模型级冻结镜像、智能选择评分） | 是 |
| ClickHouse | 请求 payload 双写 | 否（默认关闭） |

## 配置与环境变量

**所有启动依赖配置项与环境变量见** → [ENV_VARS.md](ENV_VARS.md)

优先级铁律：**环境变量 > `.env` 字段 > 内置默认值**。`.env` 含明文密码已 gitignore，用 `.env.example` 作模板复制。

至少要配置：PG 连接（host/port/user/password/database）、Redis 连接（host/port/db/prefix_key/max_connections）、`node_control_token` / `node_credential_encryption_key` / `agent_attachment_signing_key`（**容器创建前必须手填非空值**，生成方式见下方形态 A 第 2 步；留空时控制服务会尝试「生成后写回 `.env`」，在 Docker 单文件挂载上会因文件重命名报 EBUSY 而拒启）。

## 部署形态

### 形态 A：Docker Compose 一键（本机 / 测试，推荐）

`docker-compose.yml` 已覆盖两进程 + PG + Redis（ClickHouse 走 profile，按需启）：

```bash
# 1. 复制配置模板并填值（compose 起本机中间件，PG/Redis 地址用环境变量指向容器服务名）
cp .env.example .env
#   必填：POSTGRES_PASSWORD、AI_LUBRICANT_BOOTSTRAP_ADMIN_EMAIL/_PASSWORD
#   必填：NODE_CONTROL_TOKEN / NODE_CREDENTIAL_ENCRYPTION_KEY / AGENT_ATTACHMENT_SIGNING_KEY
#         （第 2 步生成后填入；留空会导致控制服务写回 .env 失败而拒启）

# 2. 生成三个共享密钥并写入 .env（三选一，任意一种即可，不需要 Python）：
#    a) 有 python：   python script/init_compose_env.py     # 自动写入，幂等
#    b) 有 openssl：  openssl rand -base64 32  → NODE_CONTROL_TOKEN=
#                     openssl rand -hex 32     → NODE_CREDENTIAL_ENCRYPTION_KEY=
#                     openssl rand -base64 48  → AGENT_ATTACHMENT_SIGNING_KEY=
#    c) 都没有：      浏览器搜「random hex generator」生成随机串填入

# 3. 一键起依赖 + 两进程
docker compose up -d --build          # postgres + redis + ai-lubricant + node-server
#   改了 .env 必须 up 重建，restart 不重新解析 env_file（改密钥后无效）

# 4.（可选）启用 ClickHouse
docker compose --profile clickhouse up -d
```

compose 内 `ai-lubricant` / `node-server` / `tunnel-server` 共用同一镜像 `ai-lubricant:<TAG>`：**只由 `ai-lubricant` 服务 `build:` 一次**，另两个服务仅 `image:` 引用（`pull_policy: never`）——避免同 tag 并行构建互踩、以及被当成远程镜像去 pull。三者分别 `CMD python main.py` / `python -m node_server` / `python -m tunnel_server`，PG/Redis 通过环境变量指向容器服务名 `postgres`/`redis`，`node_control_token` 由 .env 传递（容器读同一文件）。

端口映射：宿主 `3006 → 8001`（数据服务）、`8003 → 8003`（控制服务）、`15432 → 5432`（PG）、`6479 → 6379`（Redis）。

#### 国内部署加速（可选）

国内网络直连 Docker Hub / npmjs / Alpine CDN 很慢。在 `.env` 里设：

```bash
MIRROR_MODE=cn                            # 节点镜像 build + 运行时 npm 全走国内源
DOCKER_REGISTRY_PREFIX=docker.1ms.run/    # compose 拉基础镜像走加速器（含结尾 /）
```

- `MIRROR_MODE=cn`：服务端渲染节点安装脚本时自动为节点镜像 build 注入
  `--build-arg`（Alpine 基础镜像 / apk / npm 三层海外源换国内），并给节点进程
  导出 `NPM_CONFIG_REGISTRY`，使其装/升级编辑器 CLI（claude/codex/gemini 等）走
  npmmirror。**无需改节点侧任何配置**。
- `DOCKER_REGISTRY_PREFIX`：compose 的镜像插值不认 `MIRROR_MODE`，所以这一行
  要单独设（主服务 `FROM` + postgres/redis/clickhouse 拉取都吃它）。
- 两行**都留空 = 直连海外**，行为与不开完全一致。

三个地址均可覆盖（指向私有镜像 / 阿里云个人加速器 `<id>.mirror.aliyuncs.com/` /
自建反代）：`DOCKER_REGISTRY_PREFIX` / `NPM_REGISTRY` / `APK_MIRROR`，默认值
`docker.1ms.run/`、`https://registry.npmmirror.com`、`mirrors.aliyun.com`。

> 注意：`docker.1ms.run` 是公共加速器，可用性可能变动；生产建议换成阿里云个人
> 加速器或自建 registry proxy。

### 形态 B：宿主机裸跑（指向远端 PG/Redis）

适合 PG/Redis 已在远端（如内网 `10.x.x.x`）的情况：

```bash
# 1. 装依赖
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 配置 .env 指向远端 PG/Redis
cp .env.example .env
#   [postgres] host=10.x.x.x port=15432 ...
#   [redis]    host=10.x.x.x port=6579 ...

# 3. 两个终端各起一个进程
python main.py            # 终端 1：数据服务 :8001
python -m node_server     # 终端 2：控制服务 :8003
```

中间件也可本机用 compose 起、应用裸跑：`docker compose up -d postgres redis`，再让 `.env` 指向 `127.0.0.1`。

### 形态 C：生产

Linux 容器或 systemd，沿用形态 B 的配置方式（.env 指向生产 PG/Redis）。生产铁律：

- **切换控制服务：先停旧控制进程再启新进程**，绝不让两个 Registry 同时对同一批节点下发命令。
- `node_credential_encryption_key` 一旦生成不得轮换，否则已加密节点凭据全部失效；建议在 .env 显式预置并在两进程同值。
- `node_control_token` 两进程必须同值；`node_control_token` / `node_credential_encryption_key` / `agent_attachment_signing_key` 均须启动前手填非空（生成后不可更改，多实例必须同值）。
- 数据服务前端产物需单独构建（见下），随镜像/部署包分发。

## 形态 D / E / F：无 Docker 原生启动

`native_deps/` 包可在无 Docker 时首次运行下载并拉起 PostgreSQL / Redis / ClickHouse + 三个 app 服务。三种形态共用同一套下载/初始化/健康检查逻辑：

| 形态 | 入口 | 适用 |
|------|------|------|
| D. exe 内嵌 webview | `AiLubricant.exe`（桌面打包，见 `desktop/README.md`） | Windows 双击 |
| E. supervisord | `bash script/native_launch.sh supervisord-conf && supervisord -c <生成配置> -n` | Linux 生产裸跑 |
| F. Linux 单脚本 | `bash script/native_launch.sh up` | VPS / 单机一键 |

首次运行自动下载 PostgreSQL/Redis/ClickHouse 二进制到用户数据目录（Windows `%LOCALAPPDATA%\AiLubricant\native-deps`、Linux `~/.local/share/ai-lubricant/native-deps`，`NATIVE_DEPS_ROOT` 可覆盖），`initdb` 初始化 PG、生成 Redis/ClickHouse 配置，并写入 `.env` 的连接键（本地 `127.0.0.1` + 非默认端口 15432/6479，避免与系统 PG/Redis 冲突）。

```bash
# 形态 F：Linux 单脚本一键起
bash script/native_launch.sh up        # 下载+起 PG/Redis/CH + main/node/tunnel，前台
# 形态 E：supervisord
bash script/native_launch.sh supervisord-conf   # 生成 conf（含 6 个 program）
supervisord -c <生成的 conf 路径> -n
```

环境变量覆盖（airgapped/镜像/版本锁定）：

| 变量 | 作用 |
|------|------|
| `NATIVE_POSTGRES_VERSION` / `NATIVE_REDIS_VERSION` / `NATIVE_CLICKHOUSE_VERSION` | 锁定版本 |
| `NATIVE_<PG\|REDIS\|CLICKHOUSE>_DOWNLOAD_URL` + `NATIVE_<...>_SHA256` | 覆盖下载源（内网镜像/自建）；必须同时给 SHA256，拒绝无校验下载 |
| `NATIVE_DEPS_PROXY` | 下载走 HTTP 代理（如 `http://<proxy-host>:<port>`） |
| `NATIVE_DEPS_ROOT` | 二进制/数据/配置根目录 |
| `CLICKHOUSE_REQUEST_PAYLOAD_ENABLED` | `true` 才下载并拉起 ClickHouse（默认不拉，省 200MB+） |

**Windows Redis 硬约束**：官方无 Redis 6+ Windows 版（本系统强制 RESP3，Redis ≥6.0）。Windows 形态默认走社区 Redis 7 构建（非官方），生产建议 Memurai 或 WSL2。用 `NATIVE_REDIS_DOWNLOAD_URL` + `NATIVE_REDIS_SHA256` 指向你的 Redis 7 Windows zip。检测到 <6.0 会明确报错而非静默起坏实例。

**已知限制**：
- ClickHouse 首次下载 ~200MB+；国内走 `NATIVE_DEPS_PROXY` 或用系统 clickhouse（命中 PATH 则跳下载）。
- Linux supervisord/单脚本在 musl/Alpine 上无法运行官方 glibc 二进制，用 glibc 发行版。
- PG 主版本固定 16（与 compose 一致），不自动跨版本升级。

### 形态 G：托管平台单容器（创空间类）

面向「推送 git 仓库触发构建、跑 Dockerfile、单容器、无 compose、无持久卷、应用须自己绑固定端口」的托管平台（如创空间）。用 [Dockerfile.codespace](Dockerfile.codespace) + [deploy/codespace/entrypoint.sh](deploy/codespace/entrypoint.sh)：一个容器内用 supervisord 拉起 PG + Redis + node-server + main + tunnel-server，**对外只监听 7860**（平台约定端口，`SERVER_PORT` 驱动 main）。

平台侧配置（对照 ModelScope `deploy_schema.json`）：

```json
{
  "sdk_type": "docker",
  "port": 7860,
  "resource_configuration": "platform/2v-cpu-16g-mem",
  "environment_variables": [
    { "name": "AI_LUBRICANT_BOOTSTRAP_ADMIN_EMAIL", "value": "you@example.com" },
    { "name": "AI_LUBRICANT_BOOTSTRAP_ADMIN_PASSWORD", "value": "<强密码>" }
  ]
}
```

本地验证：

```bash
docker build -f Dockerfile.codespace -t ai-lubricant-codespace .
docker run --rm -p 7860:7860 \
  -e AI_LUBRICANT_BOOTSTRAP_ADMIN_EMAIL=you@example.com \
  -e AI_LUBRICANT_BOOTSTRAP_ADMIN_PASSWORD='<强密码>' \
  ai-lubricant-codespace
```

要点：

- **平台约定**：`sdk_type=docker`、`port` 必须恰为 `7860`（schema 里写死 `const: 7860`）；`resource_configuration` 免费档 `platform/2v-cpu-16g-mem` 即 2 vCPU/16G，足够跑全套。
- **端口**：数据服务绑 `SERVER_PORT`（默认 8001，容器内设 7860）；控制面/穿透绑容器内网 `127.0.0.1:8003`/`8004`，经穿透对外，不需宿主映射。节点拨号地址用 `AGENT_COMPOSE_NODE_SERVER_PUBLIC_URL`（穿透后的公网地址）；节点 hello 里的网关 origin 用 `AI_LUBRICANT_GATEWAY_PUBLIC_URL`（创空间公网域名）显式覆盖。
- **自举幂等**：入口脚本先跑 `python -m native_deps.cli supervisord-conf`（initdb/写 `.env`/生成密钥，全部幂等）再 `exec supervisord -n`。容器 restart 保留文件 → 初始化跳过、PG 数据存活；重建清空 → 重新建库并重种管理员。
- **管理员**：`AI_LUBRICANT_BOOTSTRAP_ADMIN_*` 走平台**运行时**环境变量注入（不烘焙进镜像），应用启动时 `seed_bootstrap_admin` 播种（幂等：邮箱已存在则不重置密码）。变量名见上表；`_NAME` 可选，默认 `admin`。
- **中间件来源**：PG 16 走 PGDG apt 源、Redis 走 Debian apt（`native_deps.binaries.ensure_dep` 先 `which` 命中系统二进制，跳过 `native_deps` 的下载路径——其 Linux PG 二进制 URL 上游已 404、Redis 默认是源码 tarball）。构建期跑一次 `provision` 完成 initdb + 写 `.env`，容器启动不再下载。若坚持用自备二进制，设 `NATIVE_*_DOWNLOAD_URL` + `SHA256`。
- **资源**：单容器内 PG + Redis + 3 个 Python 进程共用内存；16G 档充裕（PG `shared_buffers=128MB`、Redis `noeviction`+`save ""`、ClickHouse 默认关）。2 vCPU 下首次构建较慢属正常。

## 前端产物

数据服务启动时查找 `user-frontend/dist/index.html` 作为用户门户与管理端共用 SPA（挂 `/admin-static`，API 前缀走后端、其余回退 index.html）。没有构建产物时根路径只返回前端构建提示，API 仍可正常运行。

```bash
cd user-frontend
pnpm install && pnpm build    # 产物落到 user-frontend/dist
```

## 启动顺序与切换铁律

1. 起 PG → Redis（或确认远端可达）。
2. 起控制服务 `python -m node_server`（或 compose 的 `node-server`）。
3. 起数据服务 `python main.py`（或 compose 的 `ai-lubricant`）。
4. 切换控制服务版本：**先停旧进程，再启新进程**，禁止双 Registry 同时下发。

## 验证

```bash
# 数据服务健康 + 模型列表
curl http://localhost:8001/v1/models

# 控制服务监听（h2c，curl 普通 HTTP/1.1 会协商失败或返回 426，看端口监听即可）
# Linux:  ss -ltnp | grep 8003
# macOS:  lsof -iTCP:8003 -sTCP:LISTEN
```

数据服务有响应即说明 PG/Redis 连通、主链路就绪；控制服务端口监听即说明 h2c 服务已起。

## 升级（已部署实例换新版本）

> **为什么必须按本节做**：`docker compose up -d` **不会**重建容器。compose 判断是否重建
> 看的是服务**配置哈希**（镜像引用串、env、volumes、ports…），**不比对本地镜像 ID**。
> `docker tag` 只改变了 `ai-lubricant:local` 这个名字背后的镜像 ID，配置哈希没变
> → 容器不重建 → **跑的还是旧镜像**（表现为"重新构建后发布的还是旧版本"）。
> 因此升级必须显式加 `--force-recreate`。

### 版本号从哪来

镜像的权威发布路径是 GitHub Actions：

```
.github/workflows/sync-upstream.yml   每周同步上游；ci.yml 通过后在 GitHub 侧生成
                                      vYYMMDD.N（如 v260917.1、v260917.2，当日序号递增）
        ↓ push tag
.github/workflows/docker-publish.yml  用该 tag 构建并推送镜像
```

- **版本 tag `vYYMMDD.N` 由 `sync-upstream.yml` 生成**，人工也可以直接推一个 `vYYMMDD.N`
  tag，或在 Actions 页面用 `workflow_dispatch` 填 `version`。
- 镜像内置 `AI_LUBRICANT_VERSION`，**这是核验"当前跑的是哪个版本"的唯一可靠手段**：
  tag 驱动发布时等于版本 tag，分支推送（开发构建）时为 `main-<7hex>`。
- 关于 fork 同步的三个事实（避免重复探索）：GitHub **没有**任何内置的"定时自动同步 fork"
  能力（仓库页只有手动 Sync fork 按钮）；`POST /repos/{owner}/{repo}/merge-upstream`
  **只支持快进**；本 fork 已分叉（比上游多出若干提交），因此该 API 与 Sync fork 按钮
  **当前都不可用**。每周同步由 `sync-upstream.yml` 用 `git merge --no-ff` 完成。

### 步骤

```bash
# 0. 选定目标版本（见上一节；也可用 gh run list 查看最近一次发布）
VERSION=v260917.1

# 1. 拉取目标版本镜像
docker pull ghcr.io/<owner>/ai-lubricant:$VERSION

# 2. 重新打成本地共享 tag（compose 的 ai-lubricant:${TAG:-local} 认这个名字）
docker tag ghcr.io/<owner>/ai-lubricant:$VERSION ai-lubricant:local

# 3. 强制重建容器（关键：不加 --force-recreate 容器不会重建）
docker compose up -d --force-recreate

# 4. 核验：容器内版本必须等于 $VERSION
docker exec ai-lubricant printenv AI_LUBRICANT_VERSION
```

**第 4 步不等于 `$VERSION` 即视为升级失败**，回滚：

```bash
docker tag ghcr.io/<owner>/ai-lubricant:<上一个版本> ai-lubricant:local
docker compose up -d --force-recreate
```

> 也可以直接用 `bash script/upgrade_compose.sh <VERSION>` 一步完成上面 4 步
> （脚本只做 pull / tag / up --force-recreate / 版本核验，**不含任何 `docker build`**；
> 核验不一致会退出非 0 并打印回滚命令）。

### 反向验证（证明 `--force-recreate` 是必需的）

在同一台机器上：旧版本在跑时，**跳过 `--force-recreate` 只执行 `docker compose up -d`**
→ `docker exec ai-lubricant printenv AI_LUBRICANT_VERSION` **不变**（仍是旧版本）。
这一步说明"镜像换了但容器没重建"，也是排查"还是旧版本"类问题的第一现场。
