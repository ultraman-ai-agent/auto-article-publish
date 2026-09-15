"""Ars Technica AI 频道站点插件（纯 HTTP，无需浏览器）。

列表：https://arstechnica.com/ai/（SSR 卡片，实测约 13 条 /ai/ 分类文章）。
      主源重试后仍失败或 0 条时，兜底 feeds.arstechnica.com 的 RSS 并筛 /ai/ 链接。
      注意：站点根 sitemap.xml 是含 189 个子页的 sitemapindex，且子页无新闻字段，勿使用。
正文：文章页为 SSR（WordPress），正文在 class="post-content …" 容器内，
      截到首个 id="comments" / class="comments" / </article> 为止，
      按出现顺序取段落与 cdn.arstechnica.net 图片。
跨境访问偶有 TLS 重置，故 `_get()` 带有限次重试（`ARS_MAX_RETRIES` / `ARS_RETRY_DELAY_SEC`）。
注意：正文为英文，依赖 translator 的英译简能力。
"""

from __future__ import annotations

import html as html_module
import os
import re

import requests

from sites.base import (
    CrawlerError,
    ImageBlock,
    NewsArticle,
    NewsItem,
    NewsSite,
    TextBlock,
    http_get,
)

LIST_URL = "https://arstechnica.com/ai/"
RSS_URL = "https://feeds.arstechnica.com/arstechnica/index"
SITE_HOST = "https://arstechnica.com"
SITE_SOURCE = "Ars Technica"
REQUEST_TIMEOUT = 40
DEFAULT_MAX_CANDIDATES = 60
MIN_PARAGRAPH_CHARS = 60
# 正文图片都带 width 属性（WordPress 生成）；作者头像等小图标没有或很小，靠这个过滤
MIN_IMAGE_WIDTH = 400
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
CARD_PATTERN = re.compile(r'<article id="card-\d+".*?</article>', re.DOTALL)
CARD_LINK_PATTERN = re.compile(
    r'href="(https://arstechnica\.com/ai/\d{4}/\d{2}/[^"#?]+/)"'
)
TITLE_TAG_PATTERN = re.compile(r"<h[12][^>]*>(.*?)</h[12]>", re.DOTALL)
TIME_ATTR_PATTERN = re.compile(r'<time[^>]*datetime="([^"]+)"', re.DOTALL)
RSS_ITEM_PATTERN = re.compile(r"<item>(.*?)</item>", re.DOTALL)
RSS_LINK_PATTERN = re.compile(r"<link>(.*?)</link>", re.DOTALL)
RSS_TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.DOTALL)
RSS_DATE_PATTERN = re.compile(r"<pubDate>(.*?)</pubDate>", re.DOTALL)
OG_TITLE_PATTERN = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.DOTALL)
H1_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
BODY_START_MARKER = 'class="post-content'
BODY_END_MARKERS = ('id="comments"', 'class="comments', "</article>")
PARAGRAPH_PATTERN = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)
IMG_TAG_PATTERN = re.compile(r"<img[^>]*>", re.DOTALL)
IMG_SRC_PATTERN = re.compile(r'src="(https://cdn\.arstechnica\.net/[^"]+)"', re.DOTALL)
IMG_WIDTH_PATTERN = re.compile(r'width="(\d+)"', re.DOTALL)
BLOCK_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"[ \t\u00a0]+")


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _get(url: str) -> requests.Response:
    """带退避重试的 GET（该域名跨境访问偶发 TLS 重置）。"""
    return http_get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)


def _clean_text(raw: str) -> str:
    """去标签/注释/实体，剥掉 CDATA 包裹，压缩空白。"""
    text = (raw or "").replace("<![CDATA[", "").replace("]]>", "")
    text = BLOCK_COMMENT_PATTERN.sub("", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = TAG_PATTERN.sub("", text)
    text = html_module.unescape(text)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def _body_window(page: str) -> str:
    """截出正文容器内的 HTML（到评论/文章结束标记为止）。"""
    start = page.find(BODY_START_MARKER)
    if start < 0:
        raise CrawlerError("未找到正文容器（页面结构可能已变化）")
    ends = [page.find(marker, start) for marker in BODY_END_MARKERS]
    ends = [end for end in ends if end > 0]
    return page[start : min(ends) if ends else len(page)]


def _blocks_from_html(window: str) -> list:
    """把正文窗口内的段落与图片按出现顺序转成块。"""
    blocks: list[tuple[int, TextBlock | ImageBlock]] = []
    for match in PARAGRAPH_PATTERN.finditer(window):
        text = _clean_text(match.group(1))
        if len(text) < MIN_PARAGRAPH_CHARS:
            continue
        blocks.append((match.start(), TextBlock(text)))
    seen_images: set[str] = set()
    for match in IMG_TAG_PATTERN.finditer(window):
        tag = match.group(0)
        src = IMG_SRC_PATTERN.search(tag)
        if not src:
            continue
        width = IMG_WIDTH_PATTERN.search(tag)
        if not width or int(width.group(1)) < MIN_IMAGE_WIDTH:
            continue
        url = html_module.unescape(src.group(1)).strip()
        if not url or url in seen_images:
            continue
        seen_images.add(url)
        blocks.append((match.start(), ImageBlock(url)))
    blocks.sort(key=lambda pair: pair[0])
    return [block for _, block in blocks]


class ArsTechnicaSite(NewsSite):
    """Ars Technica AI 频道。"""

    key = "arstechnica"
    name = "Ars Technica"

    def list_items(self) -> list[NewsItem]:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
        items: list[NewsItem] = []
        errors: list[str] = []
        try:
            items = _parse_listing(_get(LIST_URL).text, limit)
        except CrawlerError as exc:
            errors.append(str(exc))
        if not items:
            try:
                items = _parse_rss(_get(RSS_URL).text, limit)
            except CrawlerError as exc:
                errors.append(str(exc))
        if not items and errors:
            raise CrawlerError("；".join(errors))
        return items

    def fetch(self, item: NewsItem) -> NewsArticle:
        resp = _get(item.url)
        article = parse_article_html(resp.text, fallback_title=item.title, url=item.url)
        article.source = SITE_SOURCE
        article.author = SITE_SOURCE
        return article


SITE = ArsTechnicaSite()


def _parse_listing(listing_html: str, limit: int | None = None) -> list[NewsItem]:
    """解析 /ai/ 页面的卡片，只取 /ai/YYYY/MM/ 分类文章（供自检复用）。"""
    if limit is None:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for card in CARD_PATTERN.findall(listing_html):
        link = CARD_LINK_PATTERN.search(card)
        if not link:
            continue
        url = link.group(1)
        if url in seen:
            continue
        title_match = TITLE_TAG_PATTERN.search(card)
        title = _clean_text(title_match.group(1)) if title_match else ""
        if not title:
            continue
        seen.add(url)
        time_match = TIME_ATTR_PATTERN.search(card)
        items.append(
            NewsItem(
                title=title,
                url=url,
                source=SITE_SOURCE,
                published=time_match.group(1) if time_match else "",
            )
        )
        if len(items) >= limit:
            break
    return items


def _parse_rss(xml_text: str, limit: int | None = None) -> list[NewsItem]:
    """解析 RSS 兜底，只取 /ai/ 链接（供自检复用）。"""
    if limit is None:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for block in RSS_ITEM_PATTERN.findall(xml_text):
        link = RSS_LINK_PATTERN.search(block)
        if not link:
            continue
        url = html_module.unescape(link.group(1)).strip()
        if "/ai/" not in url or url in seen:
            continue
        title_match = RSS_TITLE_PATTERN.search(block)
        title = _clean_text(title_match.group(1)) if title_match else ""
        if not title:
            continue
        seen.add(url)
        published = RSS_DATE_PATTERN.search(block)
        items.append(
            NewsItem(
                title=title,
                url=url,
                source=SITE_SOURCE,
                published=_clean_text(published.group(1)) if published else "",
            )
        )
        if len(items) >= limit:
            break
    return items


def parse_article_html(page: str, fallback_title: str = "", url: str = "") -> NewsArticle:
    """从文章 HTML 中解析正文（供自检复用）。"""
    title_match = OG_TITLE_PATTERN.search(page) or H1_PATTERN.search(page)
    title = _clean_text(title_match.group(1)) if title_match else ""
    title = re.sub(r"\s*[-|]\s*Ars Technica\s*$", "", title) or fallback_title

    window = _body_window(page)
    blocks = _blocks_from_html(window)
    if not any(isinstance(block, TextBlock) for block in blocks):
        raise CrawlerError(f"未解析到正文文本（可能是付费文章或结构已变化）：{url or fallback_title}")
    return NewsArticle(title=title, blocks=blocks, source=SITE_SOURCE, source_url=url)


def _selftest() -> None:
    listing = (
        '<article id="card-1" data-id="1" class="card-list-square post type-post category-ai">'
        '<a href="https://arstechnica.com/ai/2026/09/ai-bots-flood-the-internet/">'
        '<img src="https://cdn.arstechnica.net/wp-content/uploads/2026/01/ai-slop-300x300.jpg"></a>'
        '<h2><a href="https://arstechnica.com/ai/2026/09/ai-bots-flood-the-internet/">'
        "AI bots &#8220;Timmy,&#8221; &#8220;Ren&#8221; flood the Internet</a></h2>"
        '<time datetime="2026-09-14T17:04:32-04:00">9/14/2026</time></article>'
        '<article id="card-2" data-id="2" class="card-list-square post type-post category-apple">'
        '<a href="https://arstechnica.com/apple/2026/09/ios-27-released/">'
        '<img src="https://cdn.arstechnica.net/wp-content/uploads/2026/09/ios.jpg"></a>'
        "<h2><a href=\"https://arstechnica.com/apple/2026/09/ios-27-released/\">"
        "Apple releases iOS 27</a></h2>"
        '<time datetime="2026-09-14T15:00:00-04:00">9/14/2026</time></article>'
    )
    items = _parse_listing(listing, 60)
    assert len(items) == 1, items
    assert items[0].title == 'AI bots “Timmy,” “Ren” flood the Internet', items[0].title
    assert items[0].url == "https://arstechnica.com/ai/2026/09/ai-bots-flood-the-internet/", items[0].url
    assert items[0].published == "2026-09-14T17:04:32-04:00", items[0].published
    assert items[0].source == "Ars Technica", items[0].source
    assert _parse_listing(listing, 1) == items[:1]

    rss = (
        "<rss><channel>"
        "<item><title><![CDATA[AI leaders want to hit the brakes]]></title>"
        "<link>https://arstechnica.com/ai/2026/09/ai-leaders-want-to-hit-the-brakes/</link>"
        "<pubDate>Mon, 14 Sep 2026 19:06:13 +0000</pubDate></item>"
        "<item><title>Volvo increases the batteries</title>"
        "<link>https://arstechnica.com/cars/2026/09/volvo-batteries/</link>"
        "<pubDate>Tue, 15 Sep 2026 07:00:30 +0000</pubDate></item>"
        "</channel></rss>"
    )
    rss_items = _parse_rss(rss, 60)
    assert len(rss_items) == 1, rss_items
    assert rss_items[0].title == "AI leaders want to hit the brakes", rss_items[0].title
    assert rss_items[0].published == "Mon, 14 Sep 2026 19:06:13 +0000", rss_items[0].published

    page = (
        '<html><head><meta property="og:title" content="AI leaders want to hit the brakes - Ars Technica">'
        "</head><body>"
        '<p class="lede">这段在正文容器之前的导航文字很长很长很长很长很长很长很长很长很长很长很长很长很长</p>'
        '<div class="post-content post-content-double">'
        "<p>For years now, the major frontier AI labs have all been acting as if they were in an "
        "all-out, winner-take-all race to build the most capable models.</p>"
        '<figure><img width="1024" height="681" src="https://cdn.arstechnica.net/wp-content/uploads/2026/09/GettyImages-1.jpg">'
        "<figcaption>Getty Images</figcaption></figure>"
        '<figure><img width="1024" height="681" src="https://cdn.arstechnica.net/wp-content/uploads/2026/09/GettyImages-1.jpg"></figure>'
        '<figure><img width="1024" height="683" src="https://cdn.arstechnica.net/wp-content/uploads/2026/09/GettyImages-2.jpg"></figure>'
        '<img width="200" height="200" src="https://cdn.arstechnica.net/wp-content/uploads/2016/05/icon.png">'
        '<img class="absolute left-0 top-0 min-h-full" src="https://cdn.arstechnica.net/wp-content/uploads/2016/05/author.jpg">'
        "<p>Anthropic's Dario Amodei was at the forefront of this change in tone, arguing in a nearly "
        "4,000-word essay that the industry needed to slow down.</p>"
        "</div>"
        '<div id="comments"><p>This comment paragraph is long enough to pass the length filter but must '
        "be excluded because it lives in the comments section.</p></div>"
        "</body></html>"
    )
    article = parse_article_html(page, fallback_title="備用標題", url="http://x")
    kinds = [type(block).__name__ for block in article.blocks]
    assert article.title == "AI leaders want to hit the brakes", article.title
    # 同一 URL 只留一张；width<400 的小图标与无 width 的作者头像都要丢掉
    assert kinds == ["TextBlock", "ImageBlock", "ImageBlock", "TextBlock"], kinds
    assert article.blocks[0].text.startswith("For years now"), article.blocks[0].text
    assert article.blocks[1].url.endswith("GettyImages-1.jpg"), article.blocks[1].url
    assert article.blocks[2].url.endswith("GettyImages-2.jpg"), article.blocks[2].url
    urls = [block.url for block in article.blocks if isinstance(block, ImageBlock)]
    assert len(urls) == len(set(urls)) == 2, urls
    assert all("comment" not in block.text.lower() for block in article.blocks if isinstance(block, TextBlock))
    assert all("lede" not in block.text for block in article.blocks if isinstance(block, TextBlock))
    print("sites/arstechnica.py 自检通过")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    _selftest()
