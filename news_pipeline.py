"""新闻采集发布编排：候选选取 → 抓正文 → 下载配图 → 翻译为简体 → 推公众号草稿。

选取规则（全部走 .env）：
- 每次随机取 NEWS_PICK_MIN ~ NEWS_PICK_MAX 篇；
- NEWS_AI_FIRST=true 且存在 AI 相关候选时，只在 AI 候选中随机；
- 始终随机抽样，不取列表前几条。
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import random
import traceback
import types
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests
from PIL import Image

import images as images_module
import imagegen
import translator
import wechat
from sites import CrawlerError, ImageBlock, NewsItem, TextBlock, available_sites, get_site

BASE_DIR = Path(__file__).resolve().parent
ARTICLES_DIR = BASE_DIR / "wechat_articles"
LOGS_DIR = BASE_DIR / "logs"
HISTORY_FILE = LOGS_DIR / "wechat_publish_history.jsonl"
DEFAULT_PICK_MIN = 3
DEFAULT_PICK_MAX = 5
DEFAULT_AUTO_IMAGE_MIN = 3
DEFAULT_AUTO_IMAGE_MAX = 5
DEFAULT_IMAGE_KEYWORD = "finance news"
TRUTHY = ("1", "true", "yes", "on")
IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


class PipelineError(Exception):
    """新闻发布编排异常。"""


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in TRUTHY


def _safe_slug(text: str) -> str:
    ascii_text = "".join(c if c.isascii() and c.isalnum() else "_" for c in text).strip("_")
    return ascii_text[:30] or "news"


def is_ai_related(title: str, keywords: list[str]) -> bool:
    lowered = title.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def pick_items(items: list[NewsItem], log: Callable[[str], None]) -> list[NewsItem]:
    """按配置随机选取本期新闻。"""
    low = int(os.getenv("NEWS_PICK_MIN") or DEFAULT_PICK_MIN)
    high = int(os.getenv("NEWS_PICK_MAX") or DEFAULT_PICK_MAX)
    if high < low:
        high = low
    count = random.randint(low, high)
    if not items:
        return []

    if _truthy(os.getenv("NEWS_AI_FIRST", "true"), default=True):
        keywords = [k.strip() for k in os.getenv("NEWS_AI_KEYWORDS", "").split(",") if k.strip()]
        ai_items = [item for item in items if is_ai_related(item.title, keywords)]
        if ai_items:
            log(f"命中 {len(ai_items)} 条 AI 相关候选，优先抽取")
            if len(ai_items) >= count:
                return random.sample(ai_items, count)
            others = [item for item in items if item not in ai_items]
            picked = ai_items + random.sample(others, min(count - len(ai_items), len(others)))
            random.shuffle(picked)
            return picked

    return random.sample(items, min(count, len(items)))


def _download_image(url: str, dst_path: Path, max_bytes: int, max_side: int) -> None:
    resp = requests.get(url, headers=IMAGE_HEADERS, timeout=60)
    resp.raise_for_status()
    image = Image.open(io.BytesIO(resp.content)).convert("RGB")
    image.thumbnail((max_side, max_side))
    quality = 80
    while True:
        image.save(dst_path, "JPEG", quality=quality, optimize=True)
        if dst_path.stat().st_size <= max_bytes or quality <= 40:
            break
        quality -= 10


def download_images(article, out_dir: Path, log: Callable[[str], None]) -> None:
    """把正文图片下载压缩到本地，写回 block.local（相对文件名）。"""
    max_images = int(os.getenv("NEWS_MAX_IMAGES") or "10")
    max_bytes = int(os.getenv("NEWS_IMAGE_MAX_KB") or "500") * 1024
    max_side = int(os.getenv("NEWS_IMAGE_MAX_SIDE") or "1440")
    index = 0
    for block in article.blocks:
        if not isinstance(block, ImageBlock):
            continue
        if index >= max_images:
            break
        filename = f"img_{index + 1}.jpg"
        try:
            _download_image(block.url, out_dir / filename, max_bytes, max_side)
        except (requests.RequestException, OSError, ValueError) as exc:
            log(f"图片下载失败，已跳过：{block.url}（{exc}）")
            continue
        block.local = filename
        index += 1


def _dedup_by_hash(paths: list[str]) -> list[str]:
    """按文件内容去重，重复文件删除，返回保留的路径。"""
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        try:
            digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()
        except OSError:
            continue
        if digest in seen:
            try:
                Path(path).unlink()
            except OSError:
                pass
            continue
        seen.add(digest)
        unique.append(path)
    return unique


def _insert_images(blocks: list, paths: list[str]) -> list:
    """把图片均匀插入各文本段之后。"""
    if not paths:
        return blocks
    if not any(isinstance(block, TextBlock) for block in blocks):
        return blocks + [ImageBlock("", local=Path(path).name) for path in paths]
    text_positions = [i for i, block in enumerate(blocks) if isinstance(block, TextBlock)]
    total = len(text_positions)
    image_after: dict[int, list[str]] = {}
    for order, path in enumerate(paths, start=1):
        position = text_positions[min(total - 1, (order * total) // (len(paths) + 1))]
        image_after.setdefault(position, []).append(path)
    result: list = []
    for i, block in enumerate(blocks):
        result.append(block)
        for path in image_after.get(i, []):
            result.append(ImageBlock("", local=Path(path).name))
    return result


def add_auto_images(article, out_dir: Path, log: Callable[[str], None]) -> None:
    """正文无图时自动配图：公司/人名优先检索图库，完全无命中则用 MiniMax 文生图生成。"""
    if any(isinstance(block, ImageBlock) for block in article.blocks):
        return
    low = int(os.getenv("NEWS_AUTO_IMAGE_MIN") or DEFAULT_AUTO_IMAGE_MIN)
    high = int(os.getenv("NEWS_AUTO_IMAGE_MAX") or DEFAULT_AUTO_IMAGE_MAX)
    if high < low:
        high = low
    count = random.randint(low, high)

    text_blocks = [block.text for block in article.blocks if isinstance(block, TextBlock)]
    source_text = "\n".join([article.title, *text_blocks])[:3000]
    companies, people, keywords = translator.entity_terms(source_text, log=log)
    if companies or people or keywords:
        log(f"配图实体：公司 {companies}｜人物 {people}｜关键词 {keywords}")
    queries = companies + people + keywords or [DEFAULT_IMAGE_KEYWORD]

    paths: list[str] = []
    try:
        paths = images_module.fetch_images(queries, out_dir, count=count, topic=article.title)
    except (images_module.ImageError, requests.RequestException) as exc:
        log(f"正文无图，图库未命中：{exc}")
        paths = []
    unique = _dedup_by_hash(paths)

    if not unique:
        subjects = companies + people or [article.title]
        log("图库无命中，改用 MiniMax 文生图生成配图")
        unique = imagegen.generate_body_images(subjects, count, out_dir, log=log)
        if not unique:
            log("正文无图，且图库与生成均失败，跳过配图")
            return

    article.blocks = _insert_images(article.blocks, unique)
    log(f"正文无图，已补充 {len(unique)} 张配图")


def _blocks_to_markdown(article) -> str:
    parts: list[str] = []
    for block in article.blocks:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ImageBlock):
            source = block.local or block.url
            parts.append(f"![{block.caption or '图片'}]({source})")
    return "\n\n".join(parts)


def _resolve_cover(article, out_dir: Path, log: Callable[[str], None], source_cover: str = "") -> str:
    """封面优先级：文章自身图 → MiniMax 文生图 → 默认封面 → 无（不阻断发布）。"""
    if source_cover:
        return source_cover
    generated = imagegen.generate_cover(article.title, out_dir, log=log)
    if generated:
        return generated
    default_cover = os.getenv("WECHAT_DEFAULT_COVER", "").strip()
    if default_cover and Path(default_cover).exists():
        log(f"使用默认封面：{default_cover}")
        return str(Path(default_cover).resolve())
    log("未获取到封面图，将无封面发布")
    return ""


def _write_markdown(path: Path, article) -> None:
    front = f"---\ntitle: {article.title}\nauthor: {article.author}\n---\n\n"
    path.write_text(front + _blocks_to_markdown(article), encoding="utf-8")


def _record(article, url: str, out_dir: Path, source: str) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "title": article.title,
            "url": url,
            "source_url": article.source_url,
            "source": source,
            "dir": str(out_dir),
        }
        with HISTORY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _meta_source_url(out_dir: str) -> str:
    try:
        data = json.loads((Path(out_dir) / "meta.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    return str(data.get("source_url") or "").strip()


def published_source_urls() -> set[str]:
    """已发布原文链接集合（成功发布才写历史；老记录缺 source_url 时回退 meta.json）。"""
    urls: set[str] = set()
    if not HISTORY_FILE.exists():
        return urls
    try:
        with HISTORY_FILE.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                source_url = str(record.get("source_url") or "").strip()
                if not source_url and record.get("dir"):
                    source_url = _meta_source_url(str(record["dir"]))
                if source_url:
                    urls.add(source_url)
    except OSError:
        return urls
    return urls


def list_candidates(site_key: str | None = None) -> list[NewsItem]:
    """抓取站点候选新闻（供 GUI 选择）。"""
    site = get_site(site_key or os.getenv("NEWS_SITE", "yahoo_hk_finance"))
    return site.list_items()


def run(
    site_key: str | None = None,
    dry_run: bool = False,
    source: str = "manual",
    log: Callable[[str], None] | None = None,
    on_step: Callable[[str], None] | None = None,
    selected: list[NewsItem] | None = None,
) -> list[dict]:
    """抓取并发布新闻，返回每篇的结果；传入 selected 时只发布指定篇。"""
    logger = log or (lambda _msg: None)
    notify = on_step or (lambda _stage: None)
    site_key = site_key or os.getenv("NEWS_SITE", "yahoo_hk_finance")
    site = get_site(site_key)

    if selected is None:
        notify("抓取候选")
        logger(f"站点：{site.name}（{site_key}）")
        items = site.list_items()
        logger(f"共获取 {len(items)} 条候选新闻")
        if not items:
            raise PipelineError("未获取到候选新闻，请检查网络或站点结构")
        selected = pick_items(items, logger)

    logger(f"本期选取 {len(selected)} 篇：")
    for item in selected:
        logger(f"  - {item.title}")

    results: list[dict] = []
    for index, item in enumerate(selected, start=1):
        result = {"title": item.title, "url": "", "dir": "", "published": False, "error": ""}
        try:
            notify("抓取正文")
            logger(f"[{index}/{len(selected)}] 抓取正文：{item.title}")
            article = site.fetch(item)

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = ARTICLES_DIR / f"{stamp}_{index:02d}_{_safe_slug(article.title)}"
            out_dir.mkdir(parents=True, exist_ok=True)
            result["dir"] = str(out_dir)

            notify("下载图片")
            download_images(article, out_dir, logger)
            source_cover = ""
            for block in article.blocks:
                if isinstance(block, ImageBlock) and block.local:
                    source_cover = str((out_dir / block.local).resolve())
                    break
            add_auto_images(article, out_dir, logger)

            if _truthy(os.getenv("TRANSLATE_ENABLED", "true"), default=True):
                notify("翻译")
                logger("正在翻译为简体 ...")
                article.blocks = translator.translate_blocks(article.blocks, log=logger)
                article.title = translator.translate_title(article.title, log=logger)

            md_path = out_dir / "article.md"
            _write_markdown(md_path, article)
            (out_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "title": article.title,
                        "author": article.author,
                        "source": article.source,
                        "source_url": article.source_url,
                        "site": site_key,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            if dry_run:
                logger(f"dry-run：已留档 {md_path}，跳过发布")
                result["published"] = False
                results.append(result)
                continue

            notify("发布")
            cover = _resolve_cover(article, out_dir, logger, source_cover=source_cover)
            publish_result = wechat.publish_file(
                md_path, title=article.title, cover=cover or None, log=logger
            )
            result["url"] = publish_result.get("url", "")
            result["published"] = True
            _record(article, result["url"], out_dir, source)
            logger(f"发布成功：{article.title}（{result['url'] or '草稿已创建'}）")
        except (CrawlerError, translator.TranslateError, wechat.WechatError, PipelineError) as exc:
            result["error"] = str(exc)
            logger(f"处理失败：{item.title}：{exc}")
        except Exception as exc:  # noqa: BLE001 保证单篇失败不影响整批
            result["error"] = str(exc)
            logger(f"处理异常：{item.title}：{exc}\n{traceback.format_exc()}")
        results.append(result)
    return results


def _selftest() -> None:
    assert is_ai_related("DeepSeek發布新模型", ["deepseek", "AI"])
    assert not is_ai_related("招銀國際料美國加息", ["deepseek", "AI"])
    assert _safe_slug("股價大漲") == "news"
    assert "yahoo_hk_finance" in available_sites()

    article = types.SimpleNamespace(
        source="AASTOCKS",
        author="AASTOCKS",
        source_url="http://x",
        blocks=[
            TextBlock("第一段"),
            ImageBlock("http://img/1.jpg", caption="图注", local="img_1.jpg"),
            TextBlock("第二段"),
        ],
    )
    md = _blocks_to_markdown(article)
    assert md.startswith("第一段\n\n") and "![图注](img_1.jpg)" in md, md

    import tempfile

    original = HISTORY_FILE
    tmp = Path(tempfile.mkdtemp())
    try:
        globals()["HISTORY_FILE"] = tmp / "history.jsonl"
        (tmp / "history.jsonl").write_text(
            json.dumps({"title": "a", "source_url": "http://a"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        archive = tmp / "d1"
        archive.mkdir()
        (archive / "meta.json").write_text(
            json.dumps({"source_url": "http://b"}, ensure_ascii=False), encoding="utf-8"
        )
        with (tmp / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"title": "b", "dir": str(archive)}, ensure_ascii=False) + "\n")
        assert published_source_urls() == {"http://a", "http://b"}, published_source_urls()
    finally:
        globals()["HISTORY_FILE"] = original

    out_img = tmp / "imgs"
    out_img.mkdir()
    made: list[str] = []
    for i in range(3):
        path = out_img / f"x{i}.jpg"
        path.write_bytes(b"\xff\xd8fake" + bytes([i]))
        made.append(str(path))
    dup = out_img / "dup.jpg"
    dup.write_bytes(Path(made[0]).read_bytes())
    original_fetch = images_module.fetch_images
    original_entities = translator.entity_terms
    original_gen = imagegen.generate_cover
    original_body_gen = imagegen.generate_body_images
    try:
        translator.entity_terms = lambda *args, **kwargs: (["SoftBank"], ["孙正义"], ["semiconductor"])
        images_module.fetch_images = lambda *args, **kwargs: made + [str(dup)]
        article_empty = types.SimpleNamespace(
            title="t", blocks=[TextBlock("第一段。"), TextBlock("第二段。")]
        )
        add_auto_images(article_empty, out_img, lambda _msg: None)
        assert sum(1 for b in article_empty.blocks if isinstance(b, ImageBlock)) == 3, article_empty.blocks
        assert not dup.exists(), "重复图片未被删除"
        article_with = types.SimpleNamespace(title="t", blocks=[ImageBlock("http://x", local="a.jpg")])
        before = len(article_with.blocks)
        add_auto_images(article_with, out_img, lambda _msg: None)
        assert len(article_with.blocks) == before, "有图时不应补图"

        # 图库完全无命中 → 用文生图生成补足
        def _no_hit(*args, **kwargs):
            raise images_module.ImageError("stub")

        images_module.fetch_images = _no_hit
        generated: list[str] = []
        for i in range(2):
            gen_path = out_img / f"auto_gen_{i + 1}.jpg"
            gen_path.write_bytes(b"\xff\xd8gen" + bytes([i]))
            generated.append(str(gen_path))
        imagegen.generate_body_images = lambda *args, **kwargs: list(generated)
        article_no = types.SimpleNamespace(
            title="t", blocks=[TextBlock("第一段。"), TextBlock("第二段。")]
        )
        add_auto_images(article_no, out_img, lambda _msg: None)
        assert sum(1 for b in article_no.blocks if isinstance(b, ImageBlock)) == 2, article_no.blocks

        # 图库与生成都失败 → 不插图且不抛错
        imagegen.generate_body_images = lambda *args, **kwargs: []
        article_fail = types.SimpleNamespace(
            title="t", blocks=[TextBlock("第一段。"), TextBlock("第二段。")]
        )
        add_auto_images(article_fail, out_img, lambda _msg: None)
        assert not any(isinstance(b, ImageBlock) for b in article_fail.blocks)

        # 封面优先级链：自身图 → MiniMax 生成 → 默认封面 → 空
        art = types.SimpleNamespace(title="t", blocks=[])
        assert _resolve_cover(
            art, out_img, lambda _msg: None, source_cover=str(out_img / "a.jpg")
        ).endswith("a.jpg")
        gen = out_img / "cover_ai.jpg"
        gen.write_bytes(b"\xff\xd8gen")
        imagegen.generate_cover = lambda *args, **kwargs: str(gen)
        assert _resolve_cover(art, out_img, lambda _msg: None).endswith("cover_ai.jpg")
        imagegen.generate_cover = lambda *args, **kwargs: ""
        default = out_img / "default.jpg"
        default.write_bytes(b"\xff\xd8default")
        os.environ["WECHAT_DEFAULT_COVER"] = str(default)
        assert _resolve_cover(art, out_img, lambda _msg: None).endswith("default.jpg")
        os.environ["WECHAT_DEFAULT_COVER"] = ""
        assert _resolve_cover(art, out_img, lambda _msg: None) == ""
    finally:
        images_module.fetch_images = original_fetch
        translator.entity_terms = original_entities
        imagegen.generate_cover = original_gen
        imagegen.generate_body_images = original_body_gen
        os.environ.pop("WECHAT_DEFAULT_COVER", None)
    print("news_pipeline.py 自检通过")


if __name__ == "__main__":
    _selftest()
