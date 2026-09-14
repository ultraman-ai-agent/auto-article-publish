"""命令行入口与编排：生成笔记、配图并发布到小红书。"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

import images as images_module
import xhs
from generate import Article, GenerateError, generate_unique

ARTICLES_DIR = Path(__file__).resolve().parent / "articles"
LOGS_DIR = Path(__file__).resolve().parent / "logs"
PUBLISH_HISTORY_FILE = LOGS_DIR / "publish_history.jsonl"
DEFAULT_IMAGE_COUNT_MIN = 3
DEFAULT_IMAGE_COUNT_MAX = 6
DEFAULT_REFERENCE_COUNT = 5
TRUTHY = ("1", "true", "yes", "on")


class PublishError(Exception):
    """发布编排过程中的异常。"""


def record_publish(article: Article, visibility: str, source: str = "manual") -> None:
    """记录一次发布成功，用于“今日已发布”统计（自动与手动都记录）。"""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "title": article.title,
            "visibility": visibility,
            "source": source,
        }
        with PUBLISH_HISTORY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


@dataclass
class PublishResult:
    """一次发布流程的结果。"""

    article: Article
    out_dir: Path
    json_path: Path
    publish_result: str | None = None
    dry_run: bool = False


def _safe_slug(text: str) -> str:
    ascii_text = "".join(c if c.isascii() and c.isalnum() else "_" for c in text).strip("_")
    return ascii_text[:30] or "note"


def _load_article(path: Path) -> Article:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return Article.from_dict(data)


def pick_image_count() -> int:
    """在配置范围内随机取一次配图数量。"""
    low = int(os.getenv("IMAGE_COUNT_MIN", str(DEFAULT_IMAGE_COUNT_MIN)))
    high = int(os.getenv("IMAGE_COUNT_MAX", str(DEFAULT_IMAGE_COUNT_MAX)))
    if high < low:
        high = low
    return random.randint(low, high)


def build_reference(topic: str, log: Callable[[str], None]) -> str:
    """抓取小红书真实笔记标题作为热点参考，失败则跳过。"""
    if os.getenv("XHS_REFERENCE_ENABLED", "true").strip().lower() not in TRUTHY:
        return "（无）"
    count = int(os.getenv("XHS_REFERENCE_COUNT", str(DEFAULT_REFERENCE_COUNT)))
    try:
        titles = xhs.search_feeds(topic, count)
    except xhs.XhsError as exc:
        log(f"实时参考获取失败，已跳过：{exc}")
        return "（无）"
    if not titles:
        return "（无）"
    log(f"已获取 {len(titles)} 条参考标题")
    return "\n".join(f"- {title}" for title in titles)


def build_article(
    topic: str,
    gen: str | None = None,
    model: str | None = None,
    count: int | None = None,
    out_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> tuple[Article, Path]:
    """生成内容并抓取配图，返回 (文章, 归档目录)。"""
    logger = log or (lambda _msg: None)
    notify = on_step or (lambda _stage: None)
    image_count = count or pick_image_count()
    reference = build_reference(topic, logger)
    notify("生成文案")
    logger(f"正在生成文案（后端：{gen or '默认'}，配图 {image_count} 张）...")
    article, _sim = generate_unique(
        topic,
        backend=gen,
        model=model,
        image_count=image_count,
        reference_block=reference,
        log=logger,
    )
    if out_dir is None:
        out_dir = ARTICLES_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{_safe_slug(topic)}"
    notify("配图")
    logger(f"正在获取并压缩配图（{len(article.image_keywords)} 张）...")
    article.images = images_module.fetch_images(
        article.image_keywords,
        out_dir,
        count=len(article.image_keywords),
        keywords_zh=article.image_keywords_zh,
        topic=topic,
    )
    return article, out_dir


def _ensure_images(article: Article, out_dir: Path, count: int) -> None:
    if article.images:
        return
    keywords = article.image_keywords or ["nature"]
    article.images = images_module.fetch_images(
        keywords,
        out_dir,
        count=count,
        keywords_zh=article.image_keywords_zh,
        topic=article.title,
    )


def _save(article: Article, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "article.json"
    path.write_text(json.dumps(article.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run(
    topic: str | None = None,
    article: Article | None = None,
    gen: str | None = None,
    model: str | None = None,
    count: int | None = None,
    visibility: str | None = None,
    original: bool = False,
    dry_run: bool = False,
    out_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
    on_step: Callable[[str], None] | None = None,
    source: str = "manual",
) -> PublishResult:
    """执行 生成→配图→留档→发布 的完整流程，供 CLI 与 GUI 复用。"""
    logger = log or (lambda _msg: None)
    notify = on_step or (lambda _stage: None)

    if article is None:
        if not topic:
            raise PublishError("请提供主题")
        article, out_dir = build_article(
            topic, gen=gen, model=model, count=count, out_dir=out_dir, log=logger, on_step=notify
        )
    else:
        if not topic:
            topic = article.title
        if out_dir is None:
            out_dir = ARTICLES_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{_safe_slug(topic)}"
        notify("配图")
        _ensure_images(article, out_dir, count or pick_image_count())

    notify("留档")
    json_path = _save(article, out_dir)
    logger(f"已留档：{json_path}")

    if dry_run:
        return PublishResult(article=article, out_dir=out_dir, json_path=json_path, dry_run=True)

    notify("发布")
    target_visibility = visibility or os.getenv("XHS_VISIBILITY", "公开可见")
    logger(f"正在发布到小红书（可见范围：{target_visibility}）...")
    publish_result = xhs.publish(article, visibility=target_visibility, is_original=original)
    record_publish(article, target_visibility, source=source)
    return PublishResult(
        article=article,
        out_dir=out_dir,
        json_path=json_path,
        publish_result=publish_result,
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="生成并发布小红书图文笔记")
    parser.add_argument("topic", nargs="?", help="笔记主题")
    parser.add_argument("--gen", choices=["openai", "anthropic"], help="内容生成后端")
    parser.add_argument("--model", help="覆盖默认模型")
    parser.add_argument("--from-json", type=Path, help="跳过生成，直接读取已有 article.json")
    parser.add_argument("--count", type=int, help="指定配图数量（默认按配置范围随机）")
    parser.add_argument("--visibility", help="可见范围：公开可见/仅自己可见/仅互关好友可见")
    parser.add_argument("--original", action="store_true", help="声明原创")
    parser.add_argument("--dry-run", action="store_true", help="只生成与配图，不发布")
    args = parser.parse_args()

    load_dotenv()

    try:
        if args.from_json:
            article = _load_article(args.from_json)
            topic = article.title
        elif args.topic:
            article = None
            topic = args.topic
        else:
            parser.error("请提供主题，或使用 --from-json 指定已有文章")
            return 2

        result = run(
            topic=topic,
            article=article,
            gen=args.gen,
            model=args.model,
            count=args.count,
            visibility=args.visibility,
            original=args.original,
            dry_run=args.dry_run,
            log=print,
            source="cli",
        )

        print(f"标题：{result.article.title}")
        print(f"标签：{result.article.tags}")
        print(f"配图：{len(result.article.images)} 张")
        if result.dry_run:
            print("dry-run：已跳过发布")
        else:
            print(f"发布完成：{result.publish_result}")
        return 0
    except (GenerateError, xhs.XhsError, images_module.ImageError, PublishError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
