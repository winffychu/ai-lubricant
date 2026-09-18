# patches/ —— 对上游源码的补丁

本目录是「overlay 构建」里**唯一**允许表达"我们改了上游代码"的地方。

## 现状：**0 个补丁**

这不是遗漏，是刻意收敛的结果。本 fork 与上游的差异已经收敛到
「5 个 `.github/` 下的**纯新增**文件、**零上游文件修改**」：

```
$ git diff --name-status upstream/main
A   .github/pytest-known-failures.txt
A   .github/workflows/acceptance.yml
A   .github/workflows/ci.yml
A   .github/workflows/docker-publish.yml
A   .github/workflows/sync-upstream.yml
```

没有上游文件被修改 ⇒ 没有任何东西需要写成补丁。

## 什么时候该往这里放补丁

只有一种情况：**我们必须在镜像里改上游的代码**（而不是改我们的 CI）。
典型例子是那个已被移除的 codespace/supervisord 修复——它当时直接改了
`Dockerfile.codespace` 与 `native_deps/{cli,lifecycle}.py` 三个上游文件。
那份改动现在保存在 `deliverables/codespace-supervisord-fix.patch`，
**不在这里**，因为它属于创空间形态（由平台构建，我们的 Action 构建不到）。

## 补丁的写法

```bash
# 1. 在任意一份上游 clone 里改代码（不要在本仓库里改）
git clone https://github.com/wuxin-gh/ai-lubricant.git /tmp/up && cd /tmp/up
# ...编辑文件...
# 2. 导出为补丁（-p1 路径，与应用方式一致）
git diff > /path/to/this-repo/.github/patches/0001-<简述>.patch
# 3. 提交并推送
```

约定：

| 约定 | 说明 |
|---|---|
| 文件名 | `NNNN-<简述>.patch`，**按文件名升序应用**，编号决定顺序 |
| 路径层级 | `-p1`（即 `a/` `b/` 前缀），`git diff` 默认输出即可 |
| 一个补丁一件事 | 便于上游漂移时单独 rebase 或单独放弃 |
| 不要用 `sed` 就地改 | 补丁是**可审阅、可回滚、可版本化**的；`sed` 不是 |

## 上游漂移了怎么办

`git apply` 失败 ⇒ 工作流**硬失败**（不是警告），并打出「把补丁 rebase 到 `<sha>`
后重新提交」。这是刻意的：

- 宁可红，也不发出一个来源不明、补丁没生效的镜像；
- 失败信息里带上上游 SHA，直接 `git clone` 那个 SHA 就能复现。

## 为什么不用 sed 在流水线里就地改

有人会问：既然都要改，为什么不在 workflow 里 `sed -i`？

1. **不可审阅**：`sed` 表达式藏在 YAML 里，改动效果要跑一遍才知道；
   补丁是标准 diff，`git apply --check` 就能本地预演。
2. **静默失败**：`sed` 匹配不到时**默认成功**（退出码 0），上游一改就悄悄不生效了；
   `git apply` 匹配不到就报错。
3. **不可回滚**：补丁是文件，删掉即回滚；`sed` 改在内存里，没有痕迹。

**且它解决不了真正的问题**：`native_deps/cli.py supervisord-conf` 是容器**运行期**
由上游 `deploy/codespace/entrypoint.sh:33` 调用的，不是构建期——构建期的任何
sed / 补丁都碰不到它。那条路只能「改在仓库里」或「不修」，没有中间态。
