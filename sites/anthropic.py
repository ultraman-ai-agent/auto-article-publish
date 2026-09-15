"""Anthropic 官方新闻站点插件。

列表：https://www.anthropic.com/news（服务端渲染）
      每个 /news/<slug> 锚点内含发布时间 <time>、分类 <span>*subject*、标题 <h*> 或 <span>*title*>。
正文：文章页 PostDetail 区域内 body-* post-text 段落与 e-imageWithCaption 图片，
      按页面出现顺序还原图文；图片 src 为 /_next/image?url=...，需解码出 CDN 原图。
注意：Anthropic 正文为英文，依赖 translator 的英译简能力。
"""

from __future__ import annotations

import html as html_module
import os
import re
from urllib.parse import parse_qs, unquote, urljoin

import requests

from sites.base import (
    CrawlerError,
    ImageBlock,
    NewsArticle,
    NewsItem,
    NewsSite,
    TextBlock,
)

LIST_URL = "https://www.anthropic.com/news"
SITE_HOST = "https://www.anthropic.com"
REQUEST_TIMEOUT = 40
DEFAULT_MAX_CANDIDATES = 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
ANCHOR_PATTERN = re.compile(r'<a href="(/news/[^"#?]+)"[^>]*>(.*?)</a>', re.DOTALL)
TIME_PATTERN = re.compile(r"<time[^>]*>(.*?)</time>", re.DOTALL)
SUBJECT_PATTERN = re.compile(r"<span[^>]*>(.*?)</span>", re.DOTALL)
META_PATTERN = re.compile(r'<div[^>]*class="[^"]*meta[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
HEADING_PATTERN = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.DOTALL)
TITLE_SPAN_PATTERN = re.compile(r'<span[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</span>', re.DOTALL)
OG_TITLE_PATTERN = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.DOTALL)
H1_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
PARAGRAPH_PATTERN = re.compile(
    r'<p[^>]*class="[^"]*post-text[^"]*"[^>]*>(.*?)</p>', re.DOTALL
)
FIGURE_PATTERN = re.compile(
    r'<figure[^>]*class="[^"]*e-imageWithCaption[^"]*"[^>]*>(.*?)</figure>', re.DOTALL
)
IMG_SRC_PATTERN = re.compile(r"<img[^>]+src=\"([^\"]+)\"", re.DOTALL)
FIGCAPTION_PATTERN = re.compile(r"<figcaption[^>]*>(.*?)</figcaption>", re.DOTALL)
BLOCK_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"[ \t\u00a0]+")


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _clean_text(raw_html: str) -> str:
    text = BLOCK_COMMENT_PATTERN.sub("", raw_html)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = TAG_PATTERN.sub("", text)
    text = html_module.unescape(text)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def _decode_next_image(src: str) -> str:
    """把 /_next/image?url=... 解成 CDN 原图地址，非该形式则原样返回。"""
    if not src:
        return ""
    src = html_module.unescape(src)
    if "_next/image" not in src:
        return src
    query = src.split("?", 1)[1] if "?" in src else ""
    params = parse_qs(query)
    target = (params.get("url") or [""])[0]
    return unquote(target) if target else ""


class AnthropicSite(NewsSite):
    """Anthropic 官方新闻。"""

    key = "anthropic"
    name = "Anthropic新聞"

    def list_items(self) -> list[NewsItem]:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
        try:
            resp = _session().get(LIST_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CrawlerError(f"抓取 Anthropic 新闻列表失败：{exc}") from exc
        resp.encoding = "utf-8"
        return _parse_listing(resp.text, limit)

    def fetch(self, item: NewsItem) -> NewsArticle:
        try:
            resp = _session().get(item.url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CrawlerError(f"抓取新闻正文失败：{item.url}：{exc}") from exc
        resp.encoding = "utf-8"
        article = parse_article_html(resp.text, fallback_title=item.title, url=item.url)
        article.source = item.source or "Anthropic"
        return article


SITE = AnthropicSite()


def _extract_meta(inner_html: str) -> tuple[str, str]:
    """从锚点内解析（分类, 发布日期），无 meta 容器时退回整段锚点。"""
    meta = META_PATTERN.search(inner_html)
    region = meta.group(1) if meta else inner_html
    subject = SUBJECT_PATTERN.search(region)
    published = TIME_PATTERN.search(region)
    return (
        _clean_text(subject.group(1)) if subject else "",
        _clean_text(published.group(1)) if published else "",
    )


def _extract_title(inner_html: str) -> str:
    for pattern in (HEADING_PATTERN, TITLE_SPAN_PATTERN):
        match = pattern.search(inner_html)
        if match:
            title = _clean_text(match.group(1))
            if title:
                return title
    return ""


def parse_article_html(page: str, fallback_title: str = "", url: str = "") -> NewsArticle:
    """从文章 HTML 中解析出正文（供自检复用）。"""
    title_match = OG_TITLE_PATTERN.search(page) or H1_PATTERN.search(page)
    title = _clean_text(title_match.group(1)) if title_match else ""
    title = title or fallback_title

    blocks: list[tuple[int, TextBlock | ImageBlock]] = []
    for match in PARAGRAPH_PATTERN.finditer(page):
        text = _clean_text(match.group(1))
        if len(text) < 2:
            continue
        blocks.append((match.start(), TextBlock(text)))
    for match in FIGURE_PATTERN.finditer(page):
        src = IMG_SRC_PATTERN.search(match.group(1))
        image_url = _decode_next_image(src.group(1)) if src else ""
        if not image_url or image_url.startswith("data:"):
            continue
        caption_match = FIGCAPTION_PATTERN.search(match.group(1))
        caption = _clean_text(caption_match.group(1)) if caption_match else ""
        blocks.append((match.start(), ImageBlock(image_url, caption)))
    blocks.sort(key=lambda pair: pair[0])
    ordered = [block for _, block in blocks]
    if not any(isinstance(block, TextBlock) for block in ordered):
        raise CrawlerError(f"未解析到正文文本（页面结构可能已变化）：{url or fallback_title}")
    return NewsArticle(title=title, blocks=ordered, source_url=url)


def _selftest() -> None:
    listing = (
        '<a href="/news/claude-opus-5" class="x__sideLink x__gridItem">'
        '<div class="x__meta"><span class="caption bold">Product</span>'
        '<time class="x__date caption bold">Jul 24, 2026</time></div>'
        '<h4 class="headline-6 x__title">Introducing Claude Opus 5</h4>'
        '<p class="body-3 serif x__body">Opus 5 is a step change.</p></a>'
        '<a href="/news/enterprise-frontier-safeguards" class="x__listItem">'
        '<div class="x__meta"><time class="x__date body-3">Sep 1, 2026</time>'
        '<span class="x__subject body-3">Announcements</span></div>'
        '<span class="x__title body-3">Developing Enterprise Frontier Safeguards</span></a>'
    )
    os.environ["NEWS_MAX_CANDIDATES"] = "60"
    try:
        items = _parse_listing(listing)
    finally:
        os.environ.pop("NEWS_MAX_CANDIDATES", None)
    assert len(items) == 2, items
    assert items[0].title == "Introducing Claude Opus 5", items[0].title
    assert items[0].source == "Product" and items[0].published == "Jul 24, 2026", items[0]
    assert items[0].url == "https://www.anthropic.com/news/claude-opus-5", items[0].url
    assert items[1].title == "Developing Enterprise Frontier Safeguards", items[1].title
    assert items[1].published == "Sep 1, 2026", items[1].published

    page = (
        '<html><head><meta property="og:title" content="Introducing Claude Opus 5"></head><body>'
        '<section class="Nav-module-scss-module__aaa__nav"><p class="body-3 serif">导航噪音</p></section>'
        '<div class="PostDetail-module-scss-module__bbb__post">'
        '<p class="Body-module-scss-module__ccc__reading-column body-2 serif post-text">'
        'Claude Opus 5 is available today.</p>'
        '<figure class="ImageWithCaption-module-scss-module__ddd__e-imageWithCaption">'
        '<img src="/_next/image?url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2Fab.png'
        '%3Fwid%3D1200&amp;w=3840&amp;q=75">'
        "<figcaption>Benchmark chart</figcaption></figure>"
        '<p class="Body-module-scss-module__ccc__reading-column body-2 serif post-text">'
        'Opus 5 is the new state of the art.</p>'
        "</div></body></html>"
    )
    article = parse_article_html(page, fallback_title="備用標題", url="http://x")
    kinds = [type(block).__name__ for block in article.blocks]
    assert article.title == "Introducing Claude Opus 5", article.title
    assert kinds == ["TextBlock", "ImageBlock", "TextBlock"], kinds
    assert article.blocks[1].url == "https://www-cdn.anthropic.com/images/ab.png?wid=1200", (
        article.blocks[1].url
    )
    assert article.blocks[1].caption == "Benchmark chart", article.blocks[1].caption
    assert _decode_next_image("https://cdn.example.com/a.png") == "https://cdn.example.com/a.png"
    print("sites/anthropic.py 自检通过")


def _parse_listing(listing_html: str, limit: int | None = None) -> list[NewsItem]:
    """从列表页 HTML 解析候选（供自检复用）。"""
    if limit is None:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for match in ANCHOR_PATTERN.finditer(listing_html):
        href, inner = match.group(1), match.group(2)
        url = urljoin(SITE_HOST, href)
        if url in seen:
            continue
        title = _extract_title(inner)
        if not title:
            continue
        seen.add(url)
        source, published = _extract_meta(inner)
        items.append(NewsItem(title=title, url=url, source=source, published=published))
        if len(items) >= limit:
            break
    return items


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    _selftest()
