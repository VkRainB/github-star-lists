<!-- updated: 2026-10-29 17:21 +0800 -->

# GitHub Star Lists

VkRainB 的 GitHub Star 精选列表，按列表自动同步。
Curated repositories from [VkRainB](https://github.com/VkRainB)'s GitHub Star Lists, synced automatically.

最后更新 / Last updated: 2026-10-29 17:21 (UTC+8)

列表顺序默认与 GitHub 返回顺序一致。私有仓库不会出现在下面。
Section order follows GitHub by default. Private repositories are omitted.

## 目录 / Contents

- [占位 / Placeholder](#placeholder)
- [如何更新 / Setup](#setup)

> 列表尚未同步。请先按文末说明添加 secret `STAR_LISTS_TOKEN`，再手动运行 **Update Star Lists**。
> Lists are not synced yet. Add the `STAR_LISTS_TOKEN` secret, then run **Update Star Lists** once.

<a id="placeholder"></a>

## 占位 / Placeholder

同步成功后，每个 Star List 会成为一节，仓库条目形如：

- [owner/repo](https://github.com/owner/repo) — 仓库简介 · Python · ★ 1,234

---

<a id="setup"></a>

## 如何更新 / Setup

本文件由 GitHub Actions 根据 Star Lists 重写。查询的是 `viewer.lists`，因此令牌必须属于列表所有者本人。

Actions 自带的 `GITHUB_TOKEN` 不能读取个人 User Lists，同步不会成功。

### 添加 Secret

1. 用列表所有者账号打开 [Personal access tokens](https://github.com/settings/tokens)。
2. 生成 **classic PAT**，至少勾选 **`read:user`**。这项同步只读取列表，不需要仓库写权限。
   也可以改用细粒度 PAT，只要它能读取 starred lists（Star Lists）。
3. 打开本仓库 **Settings → Secrets and variables → Actions → New repository secret**。
   - Name：`STAR_LISTS_TOKEN`
   - Secret：上一步的令牌
4. 打开 **Actions → Update Star Lists → Run workflow**，手动运行一次。
5. 之后每天 **00:17 UTC**（北京时间约 **08:17**）自动运行。列表正文没变时不会产生新 commit，只有时间戳不同会被忽略。

不要把令牌写进仓库、日志或截图。

### 本地运行

```bash
export STAR_LISTS_TOKEN="YOUR_TOKEN"
python3 scripts/update_star_lists.py

# 只打印 Markdown，不写 README.md
python3 scripts/update_star_lists.py --dry-run

# 没有 STAR_LISTS_TOKEN 时，若已设置 GITHUB_TOKEN，则只做 dry-run
export GITHUB_TOKEN="YOUR_TOKEN"
python3 scripts/update_star_lists.py
```

列表默认保持 GitHub 返回顺序。若要按公开仓库数量从多到少排列，运行前设置 `STAR_LISTS_SORT=count`。
私有仓库会被略去，避免把私有仓库名写进公开 README。
