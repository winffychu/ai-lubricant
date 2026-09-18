# Docker 镜像构建与发布（GHCR）

本文件说明如何用 GitHub Actions 构建 Ai Lubricant 数据服务镜像并发布到镜像仓库，
以及如何使用发布出来的镜像。部署形态与端口详见 [DEPLOY.md](DEPLOY.md)。

## 一、发布什么镜像

仓库根目录的 `Dockerfile` 产出**一个镜像、三个入口**，与 `docker-compose.yml` 中
`ai-lubricant:<TAG>` 的用法完全一致：

| 入口 | 命令 | 端口 |
|---|---|---|
| 数据服务 | `python main.py` | 8001（compose 宿主映射 3006） |
| 节点控制面 | `python -m node_server` | 8003 |
| Tunnel Runtime | `python -m tunnel_server` | 8004（仅内网） |

`docker-compose.yml` 里只有 `ai-lubricant` 服务持有 `build:`，`node-server` /
`tunnel-server` 仅 `image:` 引用同一 tag 并按服务分配 `command`。因此 CI 只需构建
一个镜像即可覆盖三种进程。

> `Dockerfile.codespace` 是另一条独立链路（托管平台单容器，端口 7860），本工作流
> 不构建它；如需发布可参照本文另建一个 workflow 指定 `file: Dockerfile.codespace`。

## 二、GitHub Actions 工作流

工作流文件：`.github/workflows/docker-publish.yml`。

触发条件：

| 事件 | 行为 |
|---|---|
| push 到 `main` | 构建并推送 `main` 标签 + `latest` |
| push tag `v*` | 构建并推送该 tag 标签 + `latest` |
| pull_request 到 `main` | 仅构建校验，**不推送** |
| workflow_dispatch | 手动触发；可填 `version` input 指定版本号（留空则自动推导） |

**版本 tag 由谁生成**：权威发布路径是 `.github/workflows/sync-upstream.yml`（每周同步上游 →
`ci.yml` 通过 → 在 GitHub 侧生成单调递增的 `vYYMMDD.N` tag → 用该 tag 触发本工作流）。
人工发布可以推一个 `vYYMMDD.N` tag，也可以在 Actions 页面用 `workflow_dispatch` 填 `version`。

**镜像内的版本标识**：镜像内置 `AI_LUBRICANT_VERSION`（由 `APP_VERSION` build-arg 写入）：

| 触发方式 | `AI_LUBRICANT_VERSION` | `org.opencontainers.image.version` |
|---|---|---|
| tag 驱动发布（权威路径） | 版本 tag，如 `v260917.1` | 同左 |
| `workflow_dispatch` 填了 `version` | 该输入值 | 该输入值 |
| 分支推送（开发构建） | `main-<7hex>` | `main`（metadata-action 固有行为） |

> 分支推送时 OCI label 仍是 `main`，**请一律用 `AI_LUBRICANT_VERSION` 核验实际版本**：
> `docker exec ai-lubricant printenv AI_LUBRICANT_VERSION`。
>
> ⚠ **镜像 tag 是可变的**：对同一个 ref 重复发布（重跑工作流、或对同一个
> `vYYMMDD.N` 再 dispatch 一次），tag 会指向新的 digest（`org.opencontainers.image.created`
> 变化）—— 实测同一个 `v260917.1` 二次发布后 digest 由 `3e79b516…` 变为 `99d78463…`。
> 因此**不要用"tag → digest"做版本核验**，核验运行版本一律以镜像内
> `AI_LUBRICANT_VERSION` 为准（`script/upgrade_compose.sh` 就是这么做的）。如需
> 不可变 tag，需另开 GHCR 的 immutable tag 策略或改用 digest 部署。

产出的镜像地址：

```
ghcr.io/<owner>/ai-lubricant:<tag>
```

例如 `ghcr.io/wuxin-gh/ai-lubricant:latest`、`ghcr.io/wuxin-gh/ai-lubricant:v260917.1`。

### 关键点

- **必须 checkout 子模块**：`user-frontend/dist`（前端产物）随 `user-frontend` 子模块
  提交，镜像依赖它作为用户门户/管理端 SPA。工作流用 `submodules: recursive` 保证其存在。
- **多架构**：默认构建 `linux/amd64,linux/arm64` 单 tag 多架构清单。
- **PyPI 源**：`Dockerfile` 默认用清华源（国内构建友好）；海外 CI 通过
  `--build-arg PIP_INDEX_URL=https://pypi.org/simple` 直连 PyPI。
- **认证**：使用仓库自带的 `GITHUB_TOKEN` 推送到本仓库的 GHCR，无需额外密钥。需在
  仓库 **Settings → Actions → General → Workflow permissions** 允许写 packages。
- **构建上下文**：由 `.dockerignore` 收敛，仅保留运行时业务文件（见第五节）。

### 首次发布后：把 GHCR 包设为公开（可选）

GHCR 包默认继承仓库可见性。若要让他人免登录拉取，在仓库右侧 **Packages** →
选择该包 → **Package settings** → **Change visibility → Public**。

## 三、使用发布出来的镜像

### 方式 1：替换 compose 中的 build（推荐，保留全部编排）

在 `docker-compose.yml` 的 `ai-lubricant` 服务上，把 `build:` 换成远程 `image:`，
并给另两个引用服务设置相同 tag：

```yaml
# ai-lubricant 服务
  ai-lubricant:
    # 删掉 <<: *app-build（或注释掉 build:），改用远程镜像
    image: ghcr.io/wuxin-gh/ai-lubricant:latest
```

或直接用环境变量覆盖共享 tag（不改文件）：

```bash
# 先拉取发布镜像
docker pull ghcr.io/wuxin-gh/ai-lubricant:v260917.1
docker tag  ghcr.io/wuxin-gh/ai-lubricant:v260917.1 ai-lubricant:local
# compose 中 TAG=local 时，三个服务都会复用这个本地 tag
docker compose up -d --force-recreate
```

> 说明：`x-app-image` 锚点把共享 tag 固定为 `ai-lubricant:${TAG:-local}`。把发布镜像
> 重新 tag 成 `ai-lubricant:local` 即可让 compose 直接复用，无需改编排。
>
> ⚠ **`docker compose up -d` 单独用不会重建容器**：compose 判断是否重建看的是服务配置
> 哈希（镜像引用串、env、volumes…），**不比对本地镜像 ID**。`docker tag` 只改了
> `ai-lubricant:local` 这个名字背后的镜像 ID，配置哈希不变 → 容器不会重建，跑的还是
> 旧镜像。**升级必须加 `--force-recreate`**，完整流程见下一节。

### 方式 2：直接运行单进程（不用 compose）

```bash
docker run -d --name ai-lubricant \
  --env-file .env \
  -p 3006:8001 \
  ghcr.io/wuxin-gh/ai-lubricant:latest
```

数据服务需可达 PostgreSQL / Redis（`--env-file` 中的 `POSTGRES_HOST` / `REDIS_HOST`
指向它们），并保证 `.env` 中三个共享密钥已手填非空（见 [DEPLOY.md](DEPLOY.md) 形态 A）。

## 四、升级已部署实例（换新版本）

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

## 五、发布到其他镜像仓库

工作流默认发布 GHCR。如需发布到 Docker Hub / 阿里云 ACR / 自建 Harbor，在 workflow
中把 `REGISTRY`、`images` 与登录步骤改为对应仓库即可，例如 Docker Hub：

```yaml
env:
  REGISTRY: docker.io
  IMAGE_NAME: <你的用户名>/ai-lubricant
# 登录改用 secrets.DOCKERHUB_USERNAME / secrets.DOCKERHUB_TOKEN
```

推送国内仓库时，可同时把 `DOCKER_REGISTRY_PREFIX` 指向加速器前缀以加速基础镜像拉取。

## 六、构建上下文与"非业务内容"清理

发布镜像只应包含运行时业务文件。`.dockerignore` 负责收敛上下文，已排除：

- 版本控制/IDE、Python 构建产物、本地工具目录；
- 文档（`README.md` / `AGENTS.md` / `docs-site/` / `script/` / `specs/` / `archive/`）；
- 与服务器镜像无关的子模块（`mobile/` / `nodes/` / `device-control/` / `desktop/`）；
- **开发过程残留**（`.stage2-server-progress.md` / `.stage3-wda-job-progress.md` /
  `.footer-verify.js`）——本次补充排除，避免随 `COPY . .` 打进发布镜像。

`node_server/` 与 `user-frontend/` 随镜像发布；`user-frontend/node_modules/` 排除。

> 提示：`script/publish_github.sh` 是**内网源码快照发布脚本**（推内网 Gitea + 造
> GitHub 公开快照），与 Docker 镜像发布无关，其中含开发者本机代理等非业务内容，
> 且 `script/` 已被 `.dockerignore` 排除、不会进入镜像。
>
> ⚠ **该脚本已非权威发布入口**。镜像的权威发布路径是 GitHub Actions
> （`sync-upstream.yml` → `ci.yml` → `docker-publish.yml`，见本文第二节），
> 版本 tag 由同步工作流生成；`publish_github.sh` 仅用于内网 → GitHub 公开快照的
> 内部/遗留流程，不参与权威发布链路。
