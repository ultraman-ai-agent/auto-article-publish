"""TechCrunch 科技新闻站点插件（纯 HTTP，无需浏览器）。

列表：https://techcrunch.com/news-sitemap.xml（Google News sitemap，约 20 余条最新新闻，
      每条含 <news:title> / <news:publication_date>）。
      注意：站点根 sitemap.xml 是含 2000+ 子页的 sitemapindex，且子页无新闻字段，勿使用。
正文：文章页为 WordPress 服务端渲染，正文在 `entry-content wp-block-post-content`
      容器内；取其中的段落与图片，按出现顺序还原图文。
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
)

SITEMAP_URL = "https://techcrunch.com/news-sitemap.xml"
SITE_HOST = "https://techcrunch.com"
SITE_SOURCE = "TechCrunch"
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
TITLE_PATTERN = re.compile(r"<news:title>(.*?)</news:title>", re.DOTALL)
PUB_DATE_PATTERN = re.compile(r"<news:publication_date>(.*?)</news:publication_date>", re.DOTALL)
URL_BLOCK_PATTERN = re.compile(r"<url>(.*?)</url>", re.DOTALL)
LOC_PATTERN = re.compile(r"<loc>(.*?)</loc>", re.DOTALL)
OG_TITLE_PATTERN = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.DOTALL)
H1_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
BODY_START_MARKER = "entry-content wp-block-post-content"
BODY_END_MARKERS = ("newsletter-signup", "rightrail-promo", "Topics", "<footer")
PARAGRAPH_PATTERN = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)
FIGURE_PATTERN = re.compile(r"<figure[^>]*>(.*?)</figure>", re.DOTALL)
IMG_SRC_PATTERN = re.compile(r'<img[^>]+src="([^"]+)"', re.DOTALL)
FIGCAPTION_PATTERN = re.compile(r"<figcaption[^>]*>(.*?)</figcaption>", re.DOTALL)
BLOCK_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"[ \t\u00a0]+")
# 正文窗口内的推广/订阅段，命中即整段丢弃
DROP_KEYWORDS = (
    "25% off", "tickets now", "disrupt", "subscribe", "newsletter",
    "sign up", "exhibit table", "expo hall",
)
MIN_PARAGRAPH_CHARS = 30


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _clean_text(raw_html: str) -> str:
    """去标签/注释/实体，剥掉 CDATA 包裹，压缩空白。"""
    text = (raw_html or "").replace("<![CDATA[", "").replace("]]>", "")
    text = BLOCK_COMMENT_PATTERN.sub("", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = TAG_PATTERN.sub("", text)
    text = html_module.unescape(text)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def _is_promo(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in DROP_KEYWORDS)


def _body_window(page: str) -> str:
    """截出正文容器内的 HTML（到订阅/推广/标签等首个后置标记为止）。"""
    start = page.find(BODY_START_MARKER)
    if start < 0:
        return ""
    ends = [page.find(marker, start) for marker in BODY_END_MARKERS]
    ends = [end for end in ends if end > 0]
    return page[start : min(ends) if ends else len(page)]


def _blocks_from_html(window: str) -> list:
    """把正文窗口内的段落与图片按出现顺序转成块。"""
    blocks: list[tuple[int, TextBlock | ImageBlock]] = []
    for match in PARAGRAPH_PATTERN.finditer(window):
        text = _clean_text(match.group(1))
        if len(text) < MIN_PARAGRAPH_CHARS or _is_promo(text):
            continue
        blocks.append((match.start(), TextBlock(text)))
    for match in FIGURE_PATTERN.finditer(window):
        img = IMG_SRC_PATTERN.search(match.group(1))
        url = html_module.unescape(img.group(1)).strip() if img else ""
        if not url or url.startswith("data:"):
            continue
        caption = FIGCAPTION_PATTERN.search(match.group(1))
        blocks.append(
            (match.start(), ImageBlock(url, _clean_text(caption.group(1)) if caption else ""))
        )
    blocks.sort(key=lambda pair: pair[0])
    return [block for _, block in blocks]


class TechCrunchSite(NewsSite):
    """TechCrunch 科技新闻。"""

    key = "techcrunch"
    name = "TechCrunch"

    def list_items(self) -> list[NewsItem]:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
        try:
            resp = _session().get(SITEMAP_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CrawlerError(f"抓取 TechCrunch 列表失败：{exc}") from exc
        resp.encoding = "utf-8"
        return _parse_sitemap(resp.text, limit)

    def fetch(self, item: NewsItem) -> NewsArticle:
        try:
            resp = _session().get(item.url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CrawlerError(f"抓取新闻正文失败：{item.url}：{exc}") from exc
        resp.encoding = "utf-8"
        article = parse_article_html(resp.text, fallback_title=item.title, url=item.url)
        article.source = SITE_SOURCE
        article.author = SITE_SOURCE
        return article


SITE = TechCrunchSite()


def _parse_sitemap(xml_text: str, limit: int | None = None) -> list[NewsItem]:
    """解析 news-sitemap.xml，取 <news:title> + <news:publication_date>。"""
    if limit is None:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for block in URL_BLOCK_PATTERN.findall(xml_text):
        loc = LOC_PATTERN.search(block)
        if not loc:
            continue
        url = html_module.unescape(loc.group(1)).strip()
        if not url.startswith(f"{SITE_HOST}/") or url in seen:
            continue
        title_match = TITLE_PATTERN.search(block)
        if not title_match:
            continue
        seen.add(url)
        published = PUB_DATE_PATTERN.search(block)
        items.append(
            NewsItem(
                title=_clean_text(title_match.group(1)),
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
    title = re.sub(r"\s*\|\s*TechCrunch\s*$", "", title) or fallback_title

    window = _body_window(page)
    blocks = _blocks_from_html(window)
    if not any(isinstance(block, TextBlock) for block in blocks):
        raise CrawlerError(f"未解析到正文文本（页面结构可能已变化）：{url or fallback_title}")
    return NewsArticle(title=title, blocks=blocks, source=SITE_SOURCE, source_url=url)


def _selftest() -> None:
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        "<url>"
        "<loc>https://techcrunch.com/2026/09/14/nvidia-ceo-tells-trump/</loc>"
        "<news:news>"
        "<news:publication_date>2026-09-14T20:15:02Z</news:publication_date>"
        "<news:title><![CDATA[Nvidia CEO tells Trump: we&apos;re not going to let an AI slowdown happen]]>"
        "</news:title>"
        "</news:news>"
        "</url>"
        "<url>"
        "<loc>https://techcrunch.com/2026/09/14/openai-buys-glass-imaging/</loc>"
        "<news:news>"
        "<news:publication_date>2026-09-14T18:00:00Z</news:publication_date>"
        "<news:title>OpenAI buys smartphone camera maker Glass Imaging</news:title>"
        "</news:news>"
        "</url>"
        "</urlset>"
    )
    items = _parse_sitemap(sitemap, 60)
    assert len(items) == 2, items
    assert items[0].title == "Nvidia CEO tells Trump: we're not going to let an AI slowdown happen", (
        items[0].title
    )
    assert items[0].published == "2026-09-14T20:15:02Z", items[0].published
    assert items[0].source == "TechCrunch", items[0].source
    assert items[0].url == "https://techcrunch.com/2026/09/14/nvidia-ceo-tells-trump/", items[0].url
    assert _parse_sitemap(sitemap, 1) == items[:1]
    # 只有 <loc>/<lastmod>、没有 news:title 的普通 sitemap 不应产出候选（旧版误用 sitemap.xml 的坑）
    plain = (
        "<urlset><url><loc>https://techcrunch.com/press-release/x/</loc>"
        "<lastmod>2026-09-14</lastmod></url></urlset>"
    )
    assert _parse_sitemap(plain, 60) == [], _parse_sitemap(plain, 60)

    page = (
        '<html><head><meta property="og:title" content="OpenAI buys Glass Imaging | TechCrunch">'
        "</head><body>"
        '<p class="wp-block-paragraph">订阅推广段，应被丢弃：25% off tickets now</p>'
        '<div class="entry-content wp-block-post-content is-layout-constrained">'
        '<p class="wp-block-paragraph">Posted:</p>'
        '<p class="wp-block-paragraph">OpenAI has bought smartphone camera maker Glass Imaging '
        "in a deal worth over $300 million, according to a report.</p>"
        "<figure class=\"wp-block-image\">"
        '<img src="https://techcrunch.com/wp-content/uploads/2026/09/lead.jpg?resize=1200,802">'
        "<figcaption>IMAGE CREDITS: YIN WENJIE / GETTY IMAGES</figcaption></figure>"
        '<p class="wp-block-paragraph">Glass Imaging was founded by Ziv Attar and Tom Bishop, '
        "a pair of former Apple engineers.</p>"
        "</div>"
        '<p class="newsletter-signup-item__description">Every weekday you can get the best of '
        "TechCrunch coverage.</p>"
        "</body></html>"
    )
    article = parse_article_html(page, fallback_title="備用標題", url="http://x")
    kinds = [type(block).__name__ for block in article.blocks]
    assert article.title == "OpenAI buys Glass Imaging", article.title
    assert kinds == ["TextBlock", "ImageBlock", "TextBlock"], kinds
    assert article.blocks[0].text.startswith("OpenAI has bought"), article.blocks[0].text
    assert article.blocks[1].url.endswith("lead.jpg?resize=1200,802"), article.blocks[1].url
    assert article.blocks[1].caption == "IMAGE CREDITS: YIN WENJIE / GETTY IMAGES", (
        article.blocks[1].caption
    )
    assert all("newsletter" not in block.text for block in article.blocks if isinstance(block, TextBlock))
    print("sites/techcrunch.py 自检通过")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    _selftest()
