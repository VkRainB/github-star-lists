#!/usr/bin/env python3
"""update_star_lists 的离线测试。不访问网络。"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import update_star_lists as sync


UPDATED = datetime(2026, 10, 29, 8, 17, tzinfo=sync.TZ_CN)


def sample_lists() -> list[dict]:
    long_desc = "A " + ("very long description " * 20)
    return [
        {
            "name": "小清单",
            "slug": "small",
            "description": "只有一个仓库",
            "is_private": False,
            "items": [
                {
                    "name_with_owner": "octocat/hello",
                    "url": "https://github.com/octocat/hello",
                    "description": "Hello",
                    "stargazer_count": 12,
                    "language": "Go",
                }
            ],
            "omitted_private": 0,
        },
        {
            "name": "机器学习",
            "slug": "ml",
            "description": "常用论文复现 <draft>",
            "is_private": True,
            "items": [
                {
                    "name_with_owner": "pytorch/pytorch",
                    "url": "https://github.com/pytorch/pytorch",
                    "description": long_desc,
                    "stargazer_count": 90000,
                    "language": "Python",
                },
                {
                    "name_with_owner": "huggingface/transformers",
                    "url": "https://github.com/huggingface/transformers",
                    "description": None,
                    "stargazer_count": 1000,
                    "language": None,
                },
            ],
            "omitted_private": 2,
        },
    ]


class RenderTests(unittest.TestCase):
    def test_truncate_keeps_short_text_and_limits_long_text(self) -> None:
        self.assertEqual(sync.truncate_text("  hello\nworld  "), "hello world")
        truncated = sync.truncate_text("word " * 50)
        self.assertLessEqual(len(truncated), sync.DESC_LIMIT)
        self.assertTrue(truncated.endswith("…"))
        self.assertNotIn("\n", truncated)

    def test_readme_shape(self) -> None:
        text = sync.render_readme("VkRainB", sample_lists(), UPDATED)
        self.assertIn("<!-- updated: 2026-10-29 08:17 +0800 -->", text)
        self.assertIn("最后更新 / Last updated: 2026-10-29 08:17 (UTC+8)", text)
        self.assertIn("[VkRainB](https://github.com/VkRainB)", text)
        self.assertIn("- [机器学习](#ml)（2 · 私有）", text)
        self.assertIn("- [小清单](#small)（1）", text)
        self.assertIn(
            "- [octocat/hello](https://github.com/octocat/hello) — Hello · Go · ★ 12",
            text,
        )
        self.assertIn("· Python · ★ 90,000", text)
        self.assertIn(
            "- [huggingface/transformers](https://github.com/huggingface/transformers) · ★ 1,000",
            text,
        )
        self.assertIn("&lt;draft&gt;", text)
        self.assertNotIn("<draft>", text)
        self.assertIn("已省略 2 个私有仓库", text)
        self.assertIn("STAR_LISTS_TOKEN", text)
        self.assertIn("`read:user`", text)
        self.assertIn("GITHUB_TOKEN", text)

    def test_count_sort_is_stable(self) -> None:
        ordered = sync.order_lists(sample_lists(), "count")
        self.assertEqual([item["slug"] for item in ordered], ["ml", "small"])
        tied = [
            {"name": "a", "items": [{}]},
            {"name": "b", "items": [{}]},
        ]
        self.assertEqual([item["name"] for item in sync.order_lists(tied, "count")], ["a", "b"])

    def test_timestamp_only_difference_is_ignored(self) -> None:
        first = sync.render_readme("VkRainB", sample_lists(), UPDATED)
        later = sync.render_readme(
            "VkRainB",
            sample_lists(),
            datetime(2026, 10, 30, 8, 17, tzinfo=timezone.utc),
        )
        self.assertNotEqual(first, later)
        self.assertEqual(sync.canonical_body(first), sync.canonical_body(later))

    def test_write_skips_timestamp_churn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            original = sync.render_readme("VkRainB", sample_lists(), UPDATED)
            self.assertTrue(sync.write_if_changed(path, original))
            changed = sync.render_readme(
                "VkRainB",
                sample_lists(),
                datetime(2030, 1, 1, 0, 0, tzinfo=sync.TZ_CN),
            )
            self.assertFalse(sync.write_if_changed(path, changed))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            mutated = changed.replace("octocat/hello", "octocat/goodbye")
            self.assertTrue(sync.write_if_changed(path, mutated))
            self.assertIn("octocat/goodbye", path.read_text(encoding="utf-8"))


class FetchTests(unittest.TestCase):
    def test_pagination_skips_private_repositories(self) -> None:
        calls: list[tuple[str, dict]] = []

        def request(query: str, variables: dict) -> dict:
            calls.append((query, variables))
            if "viewer" in query and variables.get("cursor") is None:
                return {
                    "data": {
                        "viewer": {
                            "login": "VkRainB",
                            "lists": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "list-2"},
                                "nodes": [
                                    {
                                        "id": "list-1",
                                        "name": "工具",
                                        "description": "日常",
                                        "isPrivate": False,
                                        "slug": "tools",
                                        "items": {
                                            "totalCount": 3,
                                            "pageInfo": {"hasNextPage": True, "endCursor": "item-2"},
                                            "nodes": [
                                                {
                                                    "nameWithOwner": "owner/public",
                                                    "url": "https://github.com/owner/public",
                                                    "description": "Shown",
                                                    "stargazerCount": 3,
                                                    "isPrivate": False,
                                                    "primaryLanguage": {"name": "Python"},
                                                },
                                                {
                                                    "nameWithOwner": "owner/secret",
                                                    "url": "https://github.com/owner/secret",
                                                    "description": "Hidden",
                                                    "stargazerCount": 1,
                                                    "isPrivate": True,
                                                    "primaryLanguage": {"name": "Go"},
                                                },
                                            ],
                                        },
                                    }
                                ],
                            },
                        }
                    }
                }
            if "node(id:" in query:
                self.assertEqual(variables, {"id": "list-1", "cursor": "item-2"})
                return {
                    "data": {
                        "node": {
                            "items": {
                                "totalCount": 3,
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "nameWithOwner": "owner/more",
                                        "url": "https://github.com/owner/more",
                                        "description": "Next page",
                                        "stargazerCount": 8,
                                        "isPrivate": False,
                                        "primaryLanguage": None,
                                    }
                                ],
                            }
                        }
                    }
                }
            self.assertEqual(variables.get("cursor"), "list-2")
            return {
                "data": {
                    "viewer": {
                        "login": "VkRainB",
                        "lists": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "list-2",
                                    "name": "空列表",
                                    "description": None,
                                    "isPrivate": False,
                                    "slug": "empty",
                                    "items": {
                                        "totalCount": 0,
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                        "nodes": [],
                                    },
                                }
                            ],
                        },
                    }
                }
            }

        login, lists = sync.fetch_lists("test-token", request)
        self.assertEqual(login, "VkRainB")
        self.assertEqual([item["name"] for item in lists], ["工具", "空列表"])
        self.assertEqual(
            [repo["name_with_owner"] for repo in lists[0]["items"]],
            ["owner/public", "owner/more"],
        )
        self.assertEqual(lists[0]["omitted_private"], 1)
        self.assertEqual(len(calls), 3)
        rendered = sync.render_readme(login, lists, UPDATED)
        self.assertNotIn("owner/secret", rendered)
        self.assertIn("owner/public", rendered)
        self.assertIn("owner/more", rendered)


def fake_lists_response() -> dict:
    return {
        "data": {
            "viewer": {
                "login": "VkRainB",
                "lists": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "list-1",
                            "name": "Tools",
                            "description": "daily",
                            "isPrivate": False,
                            "slug": "tools",
                            "items": {
                                "totalCount": 1,
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "nameWithOwner": "a/b",
                                        "url": "https://github.com/a/b",
                                        "description": "Hi",
                                        "stargazerCount": 2,
                                        "isPrivate": False,
                                        "primaryLanguage": {"name": "Rust"},
                                    }
                                ],
                            },
                        }
                    ],
                },
            }
        }
    }


class CliTests(unittest.TestCase):
    def test_token_priority(self) -> None:
        previous = {
            "STAR_LISTS_TOKEN": os.environ.get("STAR_LISTS_TOKEN"),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
        }
        try:
            os.environ["STAR_LISTS_TOKEN"] = " Bearer star-token "
            os.environ["GITHUB_TOKEN"] = "github-token"
            token, source, dry = sync.resolve_auth()
            self.assertEqual(token, "star-token")
            self.assertEqual(source, "STAR_LISTS_TOKEN")
            self.assertFalse(dry)

            os.environ["STAR_LISTS_TOKEN"] = "  "
            token, source, dry = sync.resolve_auth()
            self.assertEqual(token, "github-token")
            self.assertEqual(source, "GITHUB_TOKEN")
            self.assertTrue(dry)

            os.environ["GITHUB_TOKEN"] = ""
            token, source, dry = sync.resolve_auth()
            self.assertIsNone(token)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_missing_token_exits(self) -> None:
        previous = {
            "STAR_LISTS_TOKEN": os.environ.get("STAR_LISTS_TOKEN"),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
        }
        try:
            os.environ.pop("STAR_LISTS_TOKEN", None)
            os.environ.pop("GITHUB_TOKEN", None)
            with tempfile.TemporaryDirectory() as tmp:
                with contextlib.redirect_stderr(io.StringIO()):
                    code = sync.main(["--output", str(Path(tmp) / "README.md")])
            self.assertEqual(code, 1)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_github_token_is_dry_run_only(self) -> None:
        previous = {
            "STAR_LISTS_TOKEN": os.environ.get("STAR_LISTS_TOKEN"),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
        }
        seen: list[str] = []

        def fake_post(token: str, query: str, variables: dict) -> dict:
            seen.append(token)
            self.assertIn("viewer", query)
            self.assertIsNone(variables.get("cursor"))
            return fake_lists_response()

        original = sync.post_graphql
        sync.post_graphql = fake_post
        try:
            os.environ.pop("STAR_LISTS_TOKEN", None)
            os.environ["GITHUB_TOKEN"] = "dry-token"
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "README.md"
                path.write_text("keep\n", encoding="utf-8")
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = sync.main(["--output", str(path)])
                self.assertEqual(code, 0)
                self.assertEqual(path.read_text(encoding="utf-8"), "keep\n")
                self.assertIn(
                    "- [a/b](https://github.com/a/b) — Hi · Rust · ★ 2",
                    stdout.getvalue(),
                )
                self.assertIn("dry-run", stderr.getvalue())
                self.assertEqual(seen, ["dry-token"])
        finally:
            sync.post_graphql = original
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_star_lists_token_writes_readme(self) -> None:
        previous = {
            "STAR_LISTS_TOKEN": os.environ.get("STAR_LISTS_TOKEN"),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
        }

        def fake_post(token: str, query: str, variables: dict) -> dict:
            self.assertEqual(token, "star-token")
            return fake_lists_response()

        original = sync.post_graphql
        sync.post_graphql = fake_post
        try:
            os.environ["STAR_LISTS_TOKEN"] = "star-token"
            os.environ["GITHUB_TOKEN"] = "ignored"
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "README.md"
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = sync.main(["--output", str(path)])
                self.assertEqual(code, 0)
                written = path.read_text(encoding="utf-8")
                self.assertIn("## Tools", written)
                self.assertIn("- [a/b](https://github.com/a/b) — Hi · Rust · ★ 2", written)
                self.assertNotIn("star-token", written)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    again = sync.main(["--output", str(path)])
                self.assertEqual(again, 0)
                self.assertEqual(path.read_text(encoding="utf-8"), written)
        finally:
            sync.post_graphql = original
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_init_is_idempotent_across_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sync.main(["--init", "--output", str(path)]), 0)
            first = path.read_text(encoding="utf-8")
            self.assertIn("列表尚未同步", first)
            self.assertIn("read:user", first)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sync.main(["--init", "--output", str(path)]), 0)
            self.assertEqual(sync.canonical_body(path.read_text(encoding="utf-8")), sync.canonical_body(first))
            self.assertEqual(path.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
