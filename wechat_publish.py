"""微信公众号新闻发布命令行入口。

用法：
    python wechat_publish.py                       # 抓新闻→翻译为简体→推草稿
    python wechat_publish.py --list                # 只看候选新闻
    python wechat_publish.py --dry-run             # 抓取翻译留档，不推送
    python wechat_publish.py --from-md a.md       # 直接发布本地 Markdown
    python wechat_publish.py --auth                # 检查公众号登录状态
    python wechat_publish.py --check               # 检查 wechatsync 是否可用
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import news_pipeline
import wechat
from sites import CrawlerError, get_site


def _print_candidates(site_key: str) -> int:
    site = get_site(site_key)
    items = site.list_items()
    print(f"站点 {site.name}（{site_key}）共 {len(items)} 条候选：")
    for index, item in enumerate(items, start=1):
        print(f"  {index:>2}. {item.title}（{item.source} {item.published}）")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()

    parser = argparse.ArgumentParser(description="抓取新闻并推送到微信公众号草稿箱")
    parser.add_argument("--site", help="新闻站点 key（默认取 .env 的 NEWS_SITE）")
    parser.add_argument("--list", action="store_true", help="只列出候选新闻，不发布")
    parser.add_argument("--dry-run", action="store_true", help="抓取+翻译+留档，不推送")
    parser.add_argument("--from-md", type=Path, help="直接发布本地 Markdown 文件")
    parser.add_argument("--title", help="配合 --from-md 指定标题")
    parser.add_argument("--cover", help="封面图本地路径或 URL")
    parser.add_argument("--platforms", help="目标平台，逗号分隔（默认 weixin）")
    parser.add_argument("--count-min", type=int, help="覆盖本次抓取数量下限")
    parser.add_argument("--count-max", type=int, help="覆盖本次抓取数量上限")
    parser.add_argument("--auth", action="store_true", help="检查公众号登录状态")
    parser.add_argument("--check", action="store_true", help="检查 wechatsync 是否可用")
    args = parser.parse_args()

    if args.count_min is not None:
        os.environ["NEWS_PICK_MIN"] = str(args.count_min)
    if args.count_max is not None:
        os.environ["NEWS_PICK_MAX"] = str(args.count_max)

    try:
        if args.check:
            print(f"wechatsync：{wechat.check_cli()}")
            return 0
        if args.auth:
            text = wechat.check_login(args.platforms)
            print(text)
            return 0 if wechat.is_logged_in(text) else 1
        if args.list:
            return _print_candidates(args.site or os.getenv("NEWS_SITE", "yahoo_hk_finance"))
        if args.from_md:
            title = args.title or args.from_md.stem
            result = wechat.publish_file(
                args.from_md,
                title=title,
                cover=args.cover,
                platforms=args.platforms,
                dry_run=args.dry_run,
            )
            print(f"发布完成：{result.get('url') or '草稿已创建'}")
            return 0

        results = news_pipeline.run(
            site_key=args.site,
            dry_run=args.dry_run,
            source="cli",
            log=print,
        )
        published = [item for item in results if item["published"]]
        failed = [item for item in results if item["error"]]
        print(f"\n完成：成功 {len(published)} 篇，失败 {len(failed)} 篇（共 {len(results)}）")
        for item in failed:
            print(f"  ✗ {item['title']}：{item['error']}")
        return 0 if not failed else 1
    except (CrawlerError, wechat.WechatError, news_pipeline.PipelineError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
