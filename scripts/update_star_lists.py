#!/usr/bin/env python3
"""从 GitHub Star Lists 生成仓库根目录的 README.md。

令牌优先读环境变量 STAR_LISTS_TOKEN，并用它写回 README。
只存在 GITHUB_TOKEN 时进入 dry-run：请求 API（若可行）并把 Markdown 打到标准输出，不改文件。
仓库 Actions 自带的 GITHUB_TOKEN 无法读取个人 User Lists。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

GRAPHQL_URL = "https://api.github.com/graphql"
PAGE_SIZE = 100
MAX_PAGES = 50
DESC_LIMIT = 100
OWNER_FALLBACK = "VkRainB"
TZ_CN = timezone(timedelta(hours=8))

UPDATED_COMMENT_RE = re.compile(r"^<!-- updated: .* -->$", re.MULTILINE)
UPDATED_LINE_RE = re.compile(r"^最后更新 / Last updated: .*$", re.MULTILINE)

LISTS_QUERY = """
query($cursor: String) {
  viewer {
    login
    lists(first: %d, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        name
        description
        isPrivate
        slug
        items(first: %d) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {
            ... on Repository {
              nameWithOwner
              url
              description
              stargazerCount
              isPrivate
              primaryLanguage { name }
            }
          }
        }
      }
    }
  }
}
""" % (PAGE_SIZE, PAGE_SIZE)

ITEMS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on UserList {
      items(first: %d, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          ... on Repository {
            nameWithOwner
            url
            description
            stargazerCount
            isPrivate
            primaryLanguage { name }
          }
        }
      }
    }
  }
}
""" % PAGE_SIZE

SETUP_SECTION = """\
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
"""

MISSING_TOKEN_HELP = """\
缺少令牌。请设置 STAR_LISTS_TOKEN（classic PAT，至少 read:user）。
仅用于本地 dry-run 时可以改用 GITHUB_TOKEN，脚本不会写 README.md。
仓库 Actions 的 GITHUB_TOKEN 不能读取个人 Star Lists。
""".strip()


class GitHubAPIError(RuntimeError):
    pass


def resolve_auth() -> tuple[str | None, str | None, bool]:
    """返回 (token, 来源环境变量, 是否只能 dry-run)。"""
    star = os.environ.get("STAR_LISTS_TOKEN", "").strip()
    if star.lower().startswith("bearer "):
        star = star[7:].strip()
    if star:
        return star, "STAR_LISTS_TOKEN", False

    github = os.environ.get("GITHUB_TOKEN", "").strip()
    if github.lower().startswith("bearer "):
        github = github[7:].strip()
    if github:
        return github, "GITHUB_TOKEN", True
    return None, None, False


def _scope_hint(message: str) -> str:
    lowered = message.lower()
    markers = (
        "insufficient_scopes",
        "resource not accessible",
        "requires authentication",
        "must have",
        "oauth",
        "scope",
        "forbidden",
    )
    if not any(marker in lowered for marker in markers):
        return message
    return (
        message
        + "\n提示: 请使用列表所有者的 classic PAT（至少 read:user），"
        "写入 Actions secret STAR_LISTS_TOKEN 后重新运行 workflow_dispatch。"
        "仓库 GITHUB_TOKEN 无法读取个人 Star Lists。"
    )


def format_errors(errors: list) -> str:
    messages = []
    for err in errors:
        if isinstance(err, dict):
            messages.append(str(err.get("message") or err))
        else:
            messages.append(str(err))
    return _scope_hint("; ".join(messages) or "GitHub GraphQL 返回错误")


def post_graphql(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    url = os.environ.get("GITHUB_GRAPHQL_URL", GRAPHQL_URL)
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-star-lists-sync",
        },
        method="POST",
    )
    delay = 2
    last_error = "GitHub GraphQL 请求失败"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            rate_limited = exc.code in (429, 502, 503, 504) or (
                exc.code == 403 and "rate limit" in raw.lower()
            )
            last_error = f"GitHub GraphQL HTTP {exc.code}: {raw[:500]}"
            if rate_limited and attempt < 4:
                time.sleep(int(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay = min(delay * 2, 30)
                continue
            raise GitHubAPIError(_scope_hint(last_error)) from exc
        except urllib.error.URLError as exc:
            last_error = f"GitHub GraphQL 网络错误: {exc.reason}"
            if attempt < 4:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise GitHubAPIError(last_error) from exc

        errors = body.get("errors") or []
        rate_limited = any(
            "RATE_LIMIT" in str(err.get("type", "")).upper()
            or "rate limit" in str(err.get("message", "")).lower()
            for err in errors
            if isinstance(err, dict)
        )
        if rate_limited and attempt < 4:
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if errors and not (body.get("data") or {}).get("viewer") and not (
            body.get("data") or {}
        ).get("node"):
            raise GitHubAPIError(format_errors(errors))
        return body
    raise GitHubAPIError(last_error)


def _language(node: dict) -> str | None:
    language = node.get("primaryLanguage") or {}
    name = language.get("name") if isinstance(language, dict) else None
    return name or None


def consume_items(nodes: list | None) -> tuple[list[dict], int, int]:
    """返回 (公开仓库, 略去的私有仓库数, 未能解析的节点数)。"""
    items: list[dict] = []
    omitted_private = 0
    skipped = 0
    for node in nodes or []:
        if not isinstance(node, dict) or not node.get("nameWithOwner"):
            skipped += 1
            continue
        if node.get("isPrivate"):
            omitted_private += 1
            continue
        items.append(
            {
                "name_with_owner": node["nameWithOwner"],
                "url": node.get("url") or "",
                "description": node.get("description"),
                "stargazer_count": int(node.get("stargazerCount") or 0),
                "language": _language(node),
            }
        )
    return items, omitted_private, skipped


def _page_items(connection: dict) -> tuple[list[dict], int, dict, int | None]:
    items, omitted, skipped = consume_items(connection.get("nodes"))
    page = connection.get("pageInfo") or {}
    total = connection.get("totalCount")
    return items, omitted, page, total if total is None else int(total)


def fetch_lists(token: str, request: Callable[[str, dict], dict] | None = None) -> tuple[str, list[dict]]:
    def default_request(query: str, variables: dict) -> dict:
        return post_graphql(token, query, variables)

    request = request or default_request
    login = OWNER_FALLBACK
    lists: list[dict] = []
    cursor = None

    for _ in range(MAX_PAGES):
        body = request(LISTS_QUERY, {"cursor": cursor})
        data = body.get("data") or {}
        viewer = data.get("viewer")
        if not viewer:
            raise GitHubAPIError(format_errors(body.get("errors") or []) or "响应中没有 viewer")
        login = viewer.get("login") or login
        connection = viewer.get("lists")
        if connection is None:
            raise GitHubAPIError(
                format_errors(body.get("errors") or [])
                or "响应中没有 viewer.lists。GITHUB_TOKEN 无法读取个人 Star Lists。"
            )
        for node in connection.get("nodes") or []:
            if not node:
                continue
            items, omitted, page, total = _page_items(node.get("items") or {})
            item_pages = 1
            while page.get("hasNextPage"):
                if item_pages >= MAX_PAGES:
                    raise GitHubAPIError(f"列表 {node.get('name') or ''} 的条目分页超过上限")
                item_cursor = page.get("endCursor")
                if not node.get("id") or not item_cursor:
                    raise GitHubAPIError(f"列表 {node.get('name') or ''} 缺少继续分页所需的 id 或 cursor")
                more = request(ITEMS_QUERY, {"id": node["id"], "cursor": item_cursor})
                more_node = ((more.get("data") or {}).get("node")) or {}
                extra, extra_omitted, page, total = _page_items(more_node.get("items") or {})
                items.extend(extra)
                omitted += extra_omitted
                item_pages += 1
            fetched = len(items) + omitted
            unresolved = max((total or fetched) - fetched, 0)
            if unresolved:
                print(
                    f"警告: 列表 {node.get('name') or ''} 声明 {total} 项，解析到 {fetched} 项",
                    file=sys.stderr,
                )
            lists.append(
                {
                    "name": node.get("name") or "Untitled",
                    "slug": node.get("slug") or "",
                    "description": node.get("description"),
                    "is_private": bool(node.get("isPrivate")),
                    "items": items,
                    "omitted_private": omitted,
                    "unresolved": unresolved,
                }
            )
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise GitHubAPIError("lists 分页缺少 endCursor")
    else:
        raise GitHubAPIError("Star Lists 分页超过上限，已中止")
    return login, lists


def order_lists(lists: list[dict], mode: str) -> list[dict]:
    if mode == "github":
        return list(lists)
    if mode == "count":
        indexed = list(enumerate(lists))
        indexed.sort(key=lambda pair: (-len(pair[1]["items"]), pair[0]))
        return [item for _, item in indexed]
    raise GitHubAPIError(f"未知排序方式: {mode}（可选 github 或 count）")


def clean_inline(text: str | None) -> str:
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    return collapsed.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate_text(text: str | None, limit: int = DESC_LIMIT) -> str:
    collapsed = re.sub(r"\s+", " ", (text or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
    if len(collapsed) <= limit:
        return clean_inline(collapsed)
    budget = max(limit - 1, 1)
    cut = collapsed[:budget].rstrip()
    if " " in cut:
        head, _, _tail = cut.rpartition(" ")
        if len(head) >= int(budget * 0.6):
            cut = head.rstrip()
    return clean_inline(cut + "…")


def slugify(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(r"[^\w\s-]", "", lowered, flags=re.UNICODE)
    lowered = lowered.replace("_", "")
    lowered = re.sub(r"\s+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-")
    return lowered or "list"


def html_id(slug: str, used: dict[str, int]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-") or "list"
    count = used.get(base, 0)
    used[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def repo_line(repo: dict) -> str:
    name = repo["name_with_owner"]
    url = repo["url"]
    description = truncate_text(repo.get("description") or "")
    suffix_parts = []
    if repo.get("language"):
        suffix_parts.append(clean_inline(repo["language"]))
    suffix_parts.append(f"★ {int(repo.get('stargazer_count') or 0):,}")
    suffix = " · ".join(suffix_parts)
    if description:
        return f"- [{name}]({url}) — {description} · {suffix}"
    return f"- [{name}]({url}) · {suffix}"


def render_readme(
    login: str,
    lists: list[dict],
    updated_at: datetime,
    *,
    placeholder: bool = False,
) -> str:
    stamp = updated_at.astimezone(TZ_CN).strftime("%Y-%m-%d %H:%M")
    profile = f"https://github.com/{login}"
    lines = [
        f"<!-- updated: {stamp} +0800 -->",
        "",
        "# GitHub Star Lists",
        "",
        f"{login} 的 GitHub Star 精选列表，按列表自动同步。",
        f"Curated repositories from [{login}]({profile})'s GitHub Star Lists, synced automatically.",
        "",
        f"最后更新 / Last updated: {stamp} (UTC+8)",
        "",
        "列表顺序默认与 GitHub 返回顺序一致。私有仓库不会出现在下面。",
        "Section order follows GitHub by default. Private repositories are omitted.",
        "",
        "## 目录 / Contents",
        "",
    ]

    if placeholder:
        lines.extend(
            [
                "- [占位 / Placeholder](#placeholder)",
                "- [如何更新 / Setup](#setup)",
                "",
                "> 列表尚未同步。请先按文末说明添加 secret `STAR_LISTS_TOKEN`，再手动运行 **Update Star Lists**。",
                "> Lists are not synced yet. Add the `STAR_LISTS_TOKEN` secret, then run **Update Star Lists** once.",
                "",
                '<a id="placeholder"></a>',
                "",
                "## 占位 / Placeholder",
                "",
                "同步成功后，每个 Star List 会成为一节，仓库条目形如：",
                "",
                "- [owner/repo](https://github.com/owner/repo) — 仓库简介 · Python · ★ 1,234",
                "",
            ]
        )
    else:
        used_ids: dict[str, int] = {}
        sections: list[tuple[str, dict]] = []
        for star_list in lists:
            raw_slug = star_list.get("slug") or slugify(star_list["name"])
            anchor = html_id(raw_slug, used_ids)
            sections.append((anchor, star_list))
            count = len(star_list["items"])
            private_mark = " · 私有" if star_list.get("is_private") else ""
            lines.append(f"- [{star_list['name']}](#{anchor})（{count}{private_mark}）")
        lines.append("- [如何更新 / Setup](#setup)")
        lines.append("")
        if not sections:
            lines.extend(
                [
                    "当前账号下没有可读的 Star List。",
                    "No readable Star Lists were returned for this account.",
                    "",
                ]
            )
        for anchor, star_list in sections:
            lines.append(f'<a id="{anchor}"></a>')
            lines.append("")
            lines.append(f"## {star_list['name']}")
            lines.append("")
            if star_list.get("is_private"):
                lines.append("> 私有列表 / Private list")
                lines.append("")
            description = clean_inline(star_list.get("description") or "")
            if description:
                lines.append(description)
                lines.append("")
            if star_list["items"]:
                for repo in star_list["items"]:
                    lines.append(repo_line(repo))
                lines.append("")
            else:
                lines.append("（此列表暂无公开仓库 / No public repositories in this list.）")
                lines.append("")
            omitted = int(star_list.get("omitted_private") or 0)
            unresolved = int(star_list.get("unresolved") or 0)
            if omitted:
                noun = "repository" if omitted == 1 else "repositories"
                lines.append(
                    f"> 已省略 {omitted} 个私有仓库，不把私有仓库名写入公开 README。"
                )
                lines.append(f"> Omitted {omitted} private {noun}.")
                lines.append("")
            if unresolved:
                lines.append(
                    f"> 另有 {unresolved} 项当前令牌看不到（通常是私有仓库），因此没有写入 README。"
                )
                lines.append(f"> {unresolved} items were not visible to this token and were left out.")
                lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(SETUP_SECTION.strip())
    lines.append("")
    return "\n".join(lines)


def canonical_body(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = UPDATED_COMMENT_RE.sub("<!-- updated: TIMESTAMP -->", normalized)
    normalized = UPDATED_LINE_RE.sub("最后更新 / Last updated: TIMESTAMP", normalized)
    return normalized.strip() + "\n"


def write_if_changed(path: Path, content: str) -> bool:
    """正文（忽略时间戳）有变化时才写入。返回是否写入。"""
    if not content.endswith("\n"):
        content += "\n"
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if canonical_body(current) == canonical_body(content):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 GitHub Star Lists 生成 README.md")
    parser.add_argument("--output", default="README.md", help="输出路径，默认仓库根目录 README.md")
    parser.add_argument("--dry-run", action="store_true", help="只打印 Markdown，不写文件")
    parser.add_argument("--init", action="store_true", help="写入尚未同步的占位 README，不访问网络")
    parser.add_argument(
        "--sort",
        choices=("github", "count"),
        default=os.environ.get("STAR_LISTS_SORT", "github") or "github",
        help="列表排序：github 保持接口顺序，count 按公开仓库数降序",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    now = datetime.now(TZ_CN)

    if args.init:
        content = render_readme(OWNER_FALLBACK, [], now, placeholder=True)
        if args.dry_run:
            sys.stdout.write(content if content.endswith("\n") else content + "\n")
            return 0
        if write_if_changed(output, content):
            print(f"已写入占位 {output}")
        else:
            print(f"{output} 正文未变化（忽略时间戳），跳过写入")
        return 0

    token, source, token_is_dry_run = resolve_auth()
    dry_run = args.dry_run or token_is_dry_run
    if not token:
        print(MISSING_TOKEN_HELP, file=sys.stderr)
        return 1
    if token_is_dry_run:
        print(
            "未设置 STAR_LISTS_TOKEN，改用 GITHUB_TOKEN 做 dry-run，不会写入 README.md。",
            file=sys.stderr,
        )
        print(
            "仓库 GITHUB_TOKEN 通常无法读取个人 Star Lists；正式同步请配置 STAR_LISTS_TOKEN。",
            file=sys.stderr,
        )

    try:
        login, lists = fetch_lists(token)
        lists = order_lists(lists, args.sort)
        content = render_readme(login, lists, now, placeholder=False)
    except GitHubAPIError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if token in content:
        print("生成结果意外包含令牌，已中止。", file=sys.stderr)
        return 1

    if dry_run:
        sys.stdout.write(content if content.endswith("\n") else content + "\n")
        print(
            f"dry-run 完成（令牌来源 {source}，{len(lists)} 个列表），未写入 {output}",
            file=sys.stderr,
        )
        return 0

    if write_if_changed(output, content):
        print(f"已更新 {output}（{login}，{len(lists)} 个列表）")
    else:
        print(f"{output} 正文未变化（忽略时间戳），跳过写入")
    return 0


if __name__ == "__main__":
    sys.exit(main())
