"""The Rundown AI 站点共用逻辑（私有模块，`_` 前缀不会被自动注册）。

站内两类内容（用户要求都接入，故拆成两个站点 key）：
- `/news/<slug>`：单条 AI 新闻（正文容器 `class="news-prose"`，实测约 17 段，干净）；
- `/articles/<slug>`：每日简报（正文容器 `id="content-blocks"`，长、含赞助/推广段，需过滤）。

列表来源：
- 新闻：`/news` 分页卡片（每页约 19 条），兜底 `news-sitemap.xml`（通常只有 1~2 条）；
- 简报：`/feed`（RSS，一次 50 条，全是 /articles），不足时翻 `/articles` HTML 列表补齐。

图片：站内图走相对路径 `/news/images/*.webp?dpl=…`（webp，PIL 可读，管线会转 JPEG）；
简报图在 `media.beehiiv.com` / beehiiv S3。统一补全域名并去掉 `?dpl=` 部署参数。
"""

from __future__ import annotations

import html as html_module
import os
import re

from sites.base import CrawlerError, ImageBlock, NewsArticle, NewsItem, TextBlock, http_get

SITE_HOST = "https://www.therundown.ai"
NEWS_LIST_URL = f"{SITE_HOST}/news"
NEWS_SITEMAP_URL = f"{SITE_HOST}/news-sitemap.xml"
ARTICLES_LIST_URL = f"{SITE_HOST}/articles"
FEED_URL = f"{SITE_HOST}/feed"
SITE_SOURCE = "The Rundown AI"
REQUEST_TIMEOUT = 40
DEFAULT_MAX_CANDIDATES = 30
DEFAULT_MAX_PAGES = 10
NEWS_MIN_PARAGRAPH_CHARS = 60
ARTICLES_MIN_PARAGRAPH_CHARS = 40
IMAGE_WIDTH_MIN = 400
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
# 简报里的赞助/推广/订阅段，命中即整段丢弃
DROP_KEYWORDS = (
    "together with", "sponsor", "sponsored", "subscribe", "25% off", "book a demo",
    "register now", "advertisement", "upgrade to", "refer a friend", "get 25% off",
)
BAD_IMAGE_HINTS = ("icon", "logo", "avatar", "favicon", "placeholder")
ARTICLE_BLOCK_PATTERN = re.compile(r"<article[^>]*>.*?</article>", re.DOTALL)
NEWS_LINK_PATTERN = re.compile(r'href="(/news/[^"#?]+)"', re.DOTALL)
NEWS_TITLE_PATTERN = re.compile(r"<h[23][^>]*>(.*?)</h[23]>", re.DOTALL)
TIME_PATTERN = re.compile(r'<time[^>]*date[Tt]ime="([^"]+)"', re.DOTALL)
ARTICLES_CARD_PATTERN = re.compile(
    r'<a[^>]+href="(/articles/[^"#?]+)"[^>]*>(.*?)</a>', re.DOTALL
)
CARD_TITLE_PATTERN = re.compile(r"<h[123][^>]*>(.*?)</h[123]>", re.DOTALL)
FEED_ITEM_PATTERN = re.compile(r"<item>(.*?)</item>", re.DOTALL)
FEED_LINK_PATTERN = re.compile(r"<link>(.*?)</link>", re.DOTALL)
FEED_TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.DOTALL)
FEED_DATE_PATTERN = re.compile(r"<pubDate>(.*?)</pubDate>", re.DOTALL)
SITEMAP_URL_PATTERN = re.compile(r"<url>(.*?)</url>", re.DOTALL)
SITEMAP_LOC_PATTERN = re.compile(r"<loc>(.*?)</loc>", re.DOTALL)
SITEMAP_TITLE_PATTERN = re.compile(r"<news:title>(.*?)</news:title>", re.DOTALL)
SITEMAP_DATE_PATTERN = re.compile(r"<news:publication_date>(.*?)</news:publication_date>", re.DOTALL)
OG_TITLE_PATTERN = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.DOTALL)
H1_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
PUBLISHED_PATTERN = re.compile(
    r'property="article:published_time"[^>]*content="([^"]*)"', re.DOTALL
)
PARAGRAPH_PATTERN = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)
IMG_TAG_PATTERN = re.compile(r"<img[^>]*>", re.DOTALL)
IMG_SRC_PATTERN = re.compile(r'src="([^"]+)"', re.DOTALL)
IMG_WIDTH_PATTERN = re.compile(r'width="(\d+)"', re.DOTALL)
NEWS_IMAGE_PATH_PATTERN = re.compile(r"^/news/images/")
ARTICLE_IMAGE_HOSTS = (
    "/articles/images/",
    "https://media.beehiiv.com/",
    "https://beehiiv-images-production.s3.amazonaws.com/",
)
BLOCK_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"[ \t\u00a0]+")
PROSE_MARKER = 'class="news-prose"'
CONTENT_BLOCKS_MARKER = 'id="content-blocks"'
ARTICLE_END_MARKER = "<footer"


def candidates_limit() -> int:
    """候选上限：NEWS_MAX_CANDIDATES 有值以它为准，否则默认 30。"""
    return int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)


def _clean_text(raw: str) -> str:
    """去标签/注释/实体，剥掉 CDATA 包裹，压缩空白。"""
    text = (raw or "").replace("<![CDATA[", "").replace("]]>", "")
    text = BLOCK_COMMENT_PATTERN.sub("", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = TAG_PATTERN.sub("", text)
    text = html_module.unescape(text)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def _absolute(url: str) -> str:
    return url if url.startswith("http") else SITE_HOST + url


def _drop_query(url: str) -> str:
    return url.split("?", 1)[0]


def _is_promo(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in DROP_KEYWORDS)


def parse_news_listing(listing_html: str, limit: int | None = None) -> list[NewsItem]:
    """解析 `/news` 页的卡片（featured 用 h2、列表用 h3）。"""
    if limit is None:
        limit = candidates_limit()
    items: list[NewsItem] = []
    seen: set[str] = set()
    for block in ARTICLE_BLOCK_PATTERN.findall(listing_html):
        link = NEWS_LINK_PATTERN.search(block)
        if not link:
            continue
        url = _absolute(link.group(1))
        if url in seen:
            continue
        title_match = NEWS_TITLE_PATTERN.search(block)
        title = _clean_text(title_match.group(1)) if title_match else ""
        if not title:
            continue
        seen.add(url)
        time_match = TIME_PATTERN.search(block)
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


def parse_news_sitemap(xml_text: str, limit: int | None = None) -> list[NewsItem]:
    """解析 news-sitemap.xml（兜底，通常只有 1~2 条）。"""
    if limit is None:
        limit = candidates_limit()
    items: list[NewsItem] = []
    seen: set[str] = set()
    for block in SITEMAP_URL_PATTERN.findall(xml_text):
        loc = SITEMAP_LOC_PATTERN.search(block)
        title_match = SITEMAP_TITLE_PATTERN.search(block)
        if not loc or not title_match:
            continue
        url = html_module.unescape(loc.group(1)).strip()
        if "/news/" not in url or url in seen:
            continue
        seen.add(url)
        published = SITEMAP_DATE_PATTERN.search(block)
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


def parse_feed(xml_text: str, limit: int | None = None) -> list[NewsItem]:
    """解析 /feed（RSS，全部是 /articles 简报）。"""
    if limit is None:
        limit = candidates_limit()
    items: list[NewsItem] = []
    seen: set[str] = set()
    for block in FEED_ITEM_PATTERN.findall(xml_text):
        link = FEED_LINK_PATTERN.search(block)
        title_match = FEED_TITLE_PATTERN.search(block)
        if not link or not title_match:
            continue
        url = html_module.unescape(link.group(1)).strip()
        title = _clean_text(title_match.group(1))
        if not url or not title or url in seen:
            continue
        seen.add(url)
        published = FEED_DATE_PATTERN.search(block)
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


def parse_articles_listing(listing_html: str, limit: int | None = None) -> list[NewsItem]:
    """解析 `/articles` 页的卡片（该页无发布时间）。"""
    if limit is None:
        limit = candidates_limit()
    items: list[NewsItem] = []
    seen: set[str] = set()
    for path, inner in ARTICLES_CARD_PATTERN.findall(listing_html):
        url = _absolute(path)
        if url in seen:
            continue
        title_match = CARD_TITLE_PATTERN.search(inner)
        title = _clean_text(title_match.group(1)) if title_match else ""
        if not title:
            continue
        seen.add(url)
        items.append(NewsItem(title=title, url=url, source=SITE_SOURCE))
        if len(items) >= limit:
            break
    return items


def _paragraphs(window: str, min_chars: int, drop_promo: bool) -> list:
    blocks = []
    for match in PARAGRAPH_PATTERN.finditer(window):
        text = _clean_text(match.group(1))
        if len(text) < min_chars:
            continue
        if drop_promo and _is_promo(text):
            continue
        blocks.append((match.start(), TextBlock(text)))
    return blocks


def _is_sponsor_image(window: str, position: int) -> bool:
    """简报里赞助区块的配图（上文 600 字内出现 TOGETHER WITH / sponsor）。"""
    context = window[max(0, position - 600) : position].lower()
    return "together with" in context or "sponsor" in context


def _images(window: str, kind: str) -> list:
    """按 host 规则收集图片，补全域名、去 ?dpl=，过滤图标/小图/赞助图。"""
    blocks = []
    seen: set[str] = set()
    for match in IMG_TAG_PATTERN.finditer(window):
        tag = match.group(0)
        src = IMG_SRC_PATTERN.search(tag)
        if not src:
            continue
        raw = html_module.unescape(src.group(1)).strip()
        if kind == "news":
            if not NEWS_IMAGE_PATH_PATTERN.match(raw):
                continue
        elif not any(host in raw for host in ARTICLE_IMAGE_HOSTS):
            continue
        if kind == "articles" and _is_sponsor_image(window, match.start()):
            continue
        url = _drop_query(_absolute(raw))
        if not url or url in seen:
            continue
        lowered = url.lower()
        if any(hint in lowered for hint in BAD_IMAGE_HINTS):
            continue
        width = IMG_WIDTH_PATTERN.search(tag)
        if width and int(width.group(1)) < IMAGE_WIDTH_MIN:
            continue
        seen.add(url)
        blocks.append((match.start(), ImageBlock(url)))
    return blocks


def _article_title(page: str, fallback_title: str) -> str:
    match = OG_TITLE_PATTERN.search(page) or H1_PATTERN.search(page)
    title = _clean_text(match.group(1)) if match else ""
    title = re.sub(r"\s*[-|]\s*The Rundown.*$", "", title).strip()
    return title or fallback_title


def _build_article(page: str, kind: str, fallback_title: str, url: str) -> NewsArticle:
    if kind == "news":
        marker = page.find(PROSE_MARKER)
        hero = page.find("<figure")
        start = hero if 0 <= hero < (marker if marker > 0 else len(page)) else marker
        min_chars, drop_promo = NEWS_MIN_PARAGRAPH_CHARS, False
    else:
        start = page.find(CONTENT_BLOCKS_MARKER)
        min_chars, drop_promo = ARTICLES_MIN_PARAGRAPH_CHARS, True
    if start < 0:
        raise CrawlerError(f"未找到正文容器（页面结构可能已变化）：{url or fallback_title}")
    end = page.find(ARTICLE_END_MARKER, start)
    window = page[start : end if end > 0 else len(page)]

    blocks = _paragraphs(window, min_chars, drop_promo) + _images(window, kind)
    blocks.sort(key=lambda pair: pair[0])
    ordered = [block for _, block in blocks]
    if not any(isinstance(block, TextBlock) for block in ordered):
        raise CrawlerError(f"未解析到正文文本（可能是付费文章或结构已变化）：{url or fallback_title}")
    article = NewsArticle(
        title=_article_title(page, fallback_title),
        blocks=ordered,
        source=SITE_SOURCE,
        source_url=url,
    )
    article.author = SITE_SOURCE
    return article


def list_news_items(limit: int | None = None) -> list[NewsItem]:
    """新闻候选：/news 分页抓取，失败则回落 news-sitemap。"""
    if limit is None:
        limit = candidates_limit()
    items: list[NewsItem] = []
    seen: set[str] = set()
    errors: list[str] = []
    page = 1
    while len(items) < limit and page <= DEFAULT_MAX_PAGES:
        url = NEWS_LIST_URL if page == 1 else f"{NEWS_LIST_URL}?page={page}"
        try:
            listing = http_get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT).text
        except CrawlerError as exc:
            errors.append(str(exc))
            break
        fresh = [item for item in parse_news_listing(listing, limit) if item.url not in seen]
        if not fresh:
            break
        for item in fresh:
            seen.add(item.url)
            items.append(item)
        page += 1
    if not items:
        try:
            xml = http_get(NEWS_SITEMAP_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT).text
            items = parse_news_sitemap(xml, limit)
        except CrawlerError as exc:
            errors.append(str(exc))
    if not items and errors:
        raise CrawlerError("；".join(errors))
    return items[:limit]


def list_articles_items(limit: int | None = None) -> list[NewsItem]:
    """简报候选：/feed 一次给 50 条；feed 失败才翻 /articles 列表兜底。"""
    if limit is None:
        limit = candidates_limit()
    items: list[NewsItem] = []
    seen: set[str] = set()
    errors: list[str] = []
    try:
        feed = http_get(FEED_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT).text
        for item in parse_feed(feed, limit):
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
    except CrawlerError as exc:
        errors.append(str(exc))

    page = 1
    while not items and page <= DEFAULT_MAX_PAGES:
        url = ARTICLES_LIST_URL if page == 1 else f"{ARTICLES_LIST_URL}?page={page}"
        try:
            listing = http_get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT).text
        except CrawlerError as exc:
            errors.append(str(exc))
            break
        fresh = [item for item in parse_articles_listing(listing, limit) if item.url not in seen]
        if not fresh:
            break
        for item in fresh:
            seen.add(item.url)
            items.append(item)
        page += 1

    if not items and errors:
        raise CrawlerError("；".join(errors))
    return items[:limit]


def fetch_news(item: NewsItem) -> NewsArticle:
    resp = http_get(item.url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    return _build_article(resp.text, "news", item.title, item.url)


def fetch_article(item: NewsItem) -> NewsArticle:
    resp = http_get(item.url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    return _build_article(resp.text, "articles", item.title, item.url)


def _selftest() -> None:
    listing = (
        '<article aria-label="Featured story" class="x"><a href="/news/first-story">'
        '<img width="1672" height="941" src="/news/images/a.webp?dpl=dpl_x"></a>'
        '<h2 class="t">Dario Amodei calls for an AI slowdown</h2>'
        '<time dateTime="2026-09-14T14:19:46.265Z">September 14, 2026</time></article>'
        '<article class="y"><a href="/news/second-story"><img width="800" src="/news/images/b.webp"></a>'
        '<h3 class="t">Zoox expands robotaxi marketing</h3>'
        '<time dateTime="2026-09-13T10:00:00.000Z">September 13, 2026</time></article>'
        '<article class="z"><a href="/articles/daily-brief">'
        '<h3>Daily brief should not appear</h3></a></article>'
    )
    items = parse_news_listing(listing, 30)
    assert len(items) == 2, items
    assert items[0].title == "Dario Amodei calls for an AI slowdown", items[0].title
    assert items[0].url == f"{SITE_HOST}/news/first-story", items[0].url
    assert items[0].published == "2026-09-14T14:19:46.265Z", items[0].published
    assert items[1].title == "Zoox expands robotaxi marketing", items[1].title
    assert parse_news_listing(listing, 1) == items[:1]
    mixed = parse_articles_listing(listing, 30)
    assert [item.url for item in mixed] == [f"{SITE_HOST}/articles/daily-brief"], mixed

    sitemap = (
        "<urlset><url><loc>https://www.therundown.ai/news/brief-news</loc><news:news>"
        "<news:publication_date>2026-09-12T10:00:00.000Z</news:publication_date>"
        "<news:title><![CDATA[Brief news title]]></news:title></news:news></url>"
        "<url><loc>https://www.therundown.ai/articles/daily</loc><news:news>"
        "<news:title>Daily brief</news:title></news:news></url></urlset>"
    )
    sm_items = parse_news_sitemap(sitemap, 30)
    assert len(sm_items) == 1, sm_items
    assert sm_items[0].title == "Brief news title", sm_items[0].title

    feed = (
        "<rss><channel><item><title><![CDATA[Top AI labs want to pump the brakes]]></title>"
        "<link>https://www.therundown.ai/articles/top-ai-labs</link>"
        "<pubDate>Mon, 14 Sep 2026 09:00:00 GMT</pubDate></item>"
        "<item><title>Apple turns Watch into AI notetaker</title>"
        "<link>https://www.therundown.ai/articles/apple-watch</link>"
        "<pubDate>Sun, 13 Sep 2026 09:00:00 GMT</pubDate></item></channel></rss>"
    )
    feed_items = parse_feed(feed, 30)
    assert len(feed_items) == 2, feed_items
    assert feed_items[0].title == "Top AI labs want to pump the brakes", feed_items[0].title
    assert feed_items[0].published == "Mon, 14 Sep 2026 09:00:00 GMT", feed_items[0].published

    articles_listing = (
        '<a class="group relative block" href="/articles/top-ai-labs">'
        "<span class=\"badge\">AI</span><h3>Top AI labs want to pump the brakes</h3></a>"
        '<a class="group relative block" href="/articles/apple-watch">'
        "<h3>Apple turns Watch into AI notetaker</h3></a>"
    )
    art_items = parse_articles_listing(articles_listing, 30)
    assert len(art_items) == 2, art_items
    assert art_items[0].url == f"{SITE_HOST}/articles/top-ai-labs", art_items[0].url

    news_page = (
        '<html><head><meta property="og:title" content="Dario Amodei calls for an AI slowdown ">'
        '<meta property="article:published_time" content="2026-09-14T14:19:46.265Z"></head><body>'
        '<h1>Dario Amodei calls for an AI slowdown</h1>'
        '<figure class="mt-8"><img width="1672" height="941" src="/news/images/hero.webp?dpl=dpl_x">'
        "<figcaption>Image source: X</figcaption></figure>"
        '<div class="news-prose"><p>Anthropic CEO Dario Amodei has called on AI labs to slow '
        "capability gains so safety can catch up, drawing rare support from rivals.</p>"
        '<img width="1200" src="/news/images/body.webp?dpl=dpl_x">'
        "<p>In his essay he argues AI is speeding its own development, and in 6 to 12 months a "
        "swarm of agents could be doing much of this work.</p>"
        "<p>Short line.</p></div>"
        '<footer id="site-footer"><p>This footer paragraph is long enough to pass the length filter '
        "but must be excluded because the window stops at the footer element.</p></footer>"
        "</body></html>"
    )
    news_article = _build_article(news_page, "news", "備用標題", "http://x")
    kinds = [type(block).__name__ for block in news_article.blocks]
    assert news_article.title == "Dario Amodei calls for an AI slowdown", news_article.title
    assert kinds == ["ImageBlock", "TextBlock", "ImageBlock", "TextBlock"], kinds
    assert news_article.blocks[0].url == f"{SITE_HOST}/news/images/hero.webp", news_article.blocks[0].url
    assert news_article.blocks[2].url == f"{SITE_HOST}/news/images/body.webp", news_article.blocks[2].url
    assert all("footer" not in block.text.lower() for block in news_article.blocks if isinstance(block, TextBlock))

    articles_page = (
        '<html><head><meta property="og:title" content="Top AI labs want to pump the brakes">'
        "</head><body>"
        '<div id="content-blocks"><style>p{color:red}</style>'
        "<p>Good morning, AI enthusiasts, and welcome to our new readers. Last week industry "
        "insiders were busy arguing about pacing the frontier.</p>"
        '<img width="800" src="https://media.beehiiv.com/uploads/asset/file/abc/hero.png">'
        "<p>TOGETHER WITH OUR SPONSOR: book a demo today and get 25% off your first year of the "
        "platform that helps your team ship faster.</p>"
        '<img width="800" src="https://media.beehiiv.com/uploads/asset/file/abc/sponsor.png">'
        '<img width="32" src="/articles/images/icon-share.png">'
        "<p>The Rundown: Anthropic CEO Dario Amodei is known for sprawling essays, and in his latest "
        "he argues the industry should deliberately pace capability gains.</p></div>"
        '<footer><p>Footer paragraph that is long enough to be captured by the paragraph regex but '
        "should be excluded by the window end marker.</p></footer></body></html>"
    )
    art_article = _build_article(articles_page, "articles", "備用標題", "http://y")
    kinds = [type(block).__name__ for block in art_article.blocks]
    assert art_article.title == "Top AI labs want to pump the brakes", art_article.title
    assert kinds == ["TextBlock", "ImageBlock", "TextBlock"], kinds
    texts = [block.text for block in art_article.blocks if isinstance(block, TextBlock)]
    assert all("together with" not in t.lower() for t in texts), texts
    assert all("footer paragraph" not in t.lower() for t in texts), texts
    images = [block.url for block in art_article.blocks if isinstance(block, ImageBlock)]
    assert images == ["https://media.beehiiv.com/uploads/asset/file/abc/hero.png"], images
    print("sites/_therundown.py 自检通过")


if __name__ == "__main__":
    _selftest()
