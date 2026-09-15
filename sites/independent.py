"""The Independent 站点插件（浏览器渲染）。

列表：Google News sitemap（news:title / news:publication_date，取 /news/ 稿件）。
正文：站点为 React 前端渲染 + 注册墙，纯 HTTP 只能拿到登录/订阅样板文字，
      因此用 Playwright 驱动本机 Edge/Chrome 渲染页面，再按 DOM 顺序取 article 内段落与配图。
依赖：可选的 playwright（未安装时仅在本站点报错，不影响其它站点与小红书链路）。
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

SITEMAP_URL = "https://www.the-independent.com/sitemaps/googlenews"
SITE_SOURCE = "The Independent"
REQUEST_TIMEOUT = 40
PAGE_TIMEOUT_MS = 60000
RENDER_TIMEOUT_MS = 25000
DEFAULT_MAX_CANDIDATES = 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
# 注册墙/订阅等样板文字，命中即整段丢弃
DROP_KEYWORDS = (
    "removed from bookmarks",
    "please refresh the page",
    "would like to be emailed",
    "privacy notice",
    "sign up to",
    "already have an account",
    "already a subscriber",
    "newsletter",
    "subscribe",
    "cookies",
    "log in to your account",
)
URL_BLOCK_PATTERN = re.compile(r"<url>(.*?)</url>", re.DOTALL)
LOC_PATTERN = re.compile(r"<loc>(.*?)</loc>", re.DOTALL)
TITLE_PATTERN = re.compile(r"<news:title>(.*?)</news:title>", re.DOTALL)
PUBLISHED_PATTERN = re.compile(r"<news:publication_date>(.*?)</news:publication_date>", re.DOTALL)
WHITESPACE_PATTERN = re.compile(r"\s+")

# 渲染后按 DOM 顺序取段落与配图（article 内），并带出标题、时间、付费标记
EXTRACT_JS = """
() => {
  const article = document.querySelector('article');
  const nodes = [...document.querySelectorAll('article p, article figure')];
  return {
    title: (document.querySelector('h1') || {}).innerText || '',
    published:
      (document.querySelector('meta[property="article:published_time"]') || {}).content || '',
    premium: !!(article && article.dataset && article.dataset.isPremium === 'true'),
    nodes: nodes.map(node => {
      if (node.tagName !== 'P') {
        const img = node.querySelector('img');
        const cap = node.querySelector('figcaption');
        return {
          type: 'image',
          src: img ? (img.currentSrc || img.src || '') : '',
          caption: cap ? cap.innerText : '',
        };
      }
      return {type: 'text', text: node.innerText || ''};
    }),
  };
}
"""


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _launch(pw):
    """按顺序尝试本机 Edge/Chrome；均失败则报错（无需下载 Chromium）。"""
    channels = [c for c in (os.getenv("INDEPENDENT_BROWSER_CHANNEL") or "").split(",") if c]
    channels += ["msedge", "chrome"]
    last_error = ""
    for channel in channels:
        try:
            return pw.chromium.launch(channel=channel, headless=True)
        except Exception as exc:  # noqa: BLE001 逐个渠道尝试
            last_error = f"{channel}: {exc}"
    raise CrawlerError(
        "无法启动浏览器（需本机安装 Edge 或 Chrome，且 pip install playwright）：" + last_error
    )


def _clean_text(raw: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", html_module.unescape(raw or "")).strip()


def _is_boilerplate(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in DROP_KEYWORDS)


class IndependentSite(NewsSite):
    """The Independent 新闻（浏览器渲染抓正文）。"""

    key = "independent"
    name = "The Independent"

    def list_items(self) -> list[NewsItem]:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
        try:
            resp = _session().get(SITEMAP_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CrawlerError(f"抓取 The Independent 新闻列表失败：{exc}") from exc
        return _parse_sitemap(resp.text, limit)

    def fetch(self, item: NewsItem) -> NewsArticle:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CrawlerError(
                "未安装 playwright，无法抓取 The Independent 正文：pip install playwright"
            ) from exc

        # ponytail: 每篇起一次浏览器（约 2-4s），抓取速度成为瓶颈时再改成复用实例
        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-US",
                    viewport={"width": 1400, "height": 900},
                )
                page = context.new_page()
                try:
                    page.goto(item.url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                    page.wait_for_selector("article p", timeout=RENDER_TIMEOUT_MS)
                except Exception as exc:  # noqa: BLE001 playwright 异常按抓取失败处理
                    raise CrawlerError(f"渲染新闻页失败：{item.url}：{str(exc)[:200]}") from exc
                data = page.evaluate(EXTRACT_JS)
            finally:
                browser.close()

        if data.get("premium"):
            raise CrawlerError(f"该文章为付费内容，无法抓取正文：{item.url}")

        blocks = _blocks_from_nodes(data.get("nodes") or [])
        if not any(isinstance(block, TextBlock) for block in blocks):
            raise CrawlerError(f"未解析到正文文本（可能为付费/结构变更）：{item.url}")
        article = NewsArticle(
            title=_clean_text(data.get("title")) or item.title,
            blocks=blocks,
            source=SITE_SOURCE,
            source_url=item.url,
        )
        article.author = SITE_SOURCE
        return article


SITE = IndependentSite()


def _parse_sitemap(xml_text: str, limit: int | None = None) -> list[NewsItem]:
    """解析 Google News sitemap，只取 /news/ 稿件（供自检复用）。"""
    if limit is None:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for block in URL_BLOCK_PATTERN.findall(xml_text):
        loc = LOC_PATTERN.search(block)
        title = TITLE_PATTERN.search(block)
        if not loc or not title:
            continue
        url = html_module.unescape(loc.group(1)).strip()
        if "/news/" not in url or url in seen:
            continue
        seen.add(url)
        published = PUBLISHED_PATTERN.search(block)
        items.append(
            NewsItem(
                title=_clean_text(title.group(1)),
                url=url,
                source=SITE_SOURCE,
                published=_clean_text(published.group(1)) if published else "",
            )
        )
        if len(items) >= limit:
            break
    return items


def _blocks_from_nodes(nodes: list[dict]) -> list:
    """把渲染结果转成正文块，按 DOM 顺序保留图文，丢弃样板段与图标图。"""
    blocks: list = []
    for node in nodes:
        if node.get("type") == "text":
            text = _clean_text(node.get("text"))
            if len(text) < 2 or _is_boilerplate(text):
                continue
            blocks.append(TextBlock(text))
            continue
        url = (node.get("src") or "").split(" ")[0].strip()
        if not url or url.startswith("data:") or "/static-assets/" in url:
            continue
        blocks.append(ImageBlock(url, _clean_text(node.get("caption"))))
    return blocks


def _selftest() -> None:
    sitemap = (
        "<urlset><url><loc>https://www.the-independent.com/arts-entertainment/tv/news/a-b1.html</loc>"
        "<news:news><news:publication_date>2026-09-15T03:20:17Z</news:publication_date>"
        "<news:title>Emmys 2026: Matthew Rhys makes history</news:title></news:news></url>"
        "<url><loc>https://www.the-independent.com/sport/football/x-b2.html</loc>"
        "<news:news><news:title>Sport story</news:title></news:news></url>"
        "<url><loc>https://www.the-independent.com/news/uk/home-news/c-d3.html</loc>"
        "<news:news><news:publication_date>2026-09-14T10:00:00Z</news:publication_date>"
        "<news:title>UK story</news:title></news:news></url></urlset>"
    )
    items = _parse_sitemap(sitemap, 60)
    assert [item.title for item in items] == [
        "Emmys 2026: Matthew Rhys makes history",
        "UK story",
    ], items
    assert items[0].published == "2026-09-15T03:20:17Z", items[0].published
    assert items[0].source == "The Independent", items[0].source

    nodes = [
        {"type": "text", "text": "  Well, at least Matthew Rhys had a good night.  "},
        {"type": "text", "text": "Removed from bookmarks"},
        {"type": "text", "text": "I would like to be emailed about offers, events and updates."},
        {"type": "text", "text": "x"},
        {"type": "image", "src": "https://static.independent.co.uk/static-assets/images/a.png", "caption": ""},
        {
            "type": "image",
            "src": "https://static.the-independent.com/2026/09/15/02/awards.JPG?width=1200",
            "caption": "Taylor Swift makes surprise appearance",
        },
        {"type": "text", "text": "Rhys' twin triumphs were high points."},
    ]
    blocks = _blocks_from_nodes(nodes)
    kinds = [type(block).__name__ for block in blocks]
    assert kinds == ["TextBlock", "ImageBlock", "TextBlock"], kinds
    assert blocks[0].text == "Well, at least Matthew Rhys had a good night.", blocks[0].text
    assert blocks[1].url.endswith("awards.JPG?width=1200"), blocks[1].url
    assert blocks[1].caption == "Taylor Swift makes surprise appearance", blocks[1].caption
    print("sites/independent.py 自检通过")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    _selftest()
