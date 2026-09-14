"""Yahoo 香港財經「最新新聞」站点插件。

列表：https://hk.finance.yahoo.com/topic/latest-news/
正文：data-testid="article-body" 下的 bodyItems-wrapper，
      仅白名单采集 text-block 段落与 article-figure-image 图片，天然过滤广告；
      并按规则丢弃署名/订阅/推广段（<em> 标记、NEWS_DROP_KEYWORDS）与末尾短署名。
"""

from __future__ import annotations

import html as html_module
import os
import re
from urllib.parse import unquote

import requests

from sites.base import (
    CrawlerError,
    ImageBlock,
    NewsArticle,
    NewsItem,
    NewsSite,
    TextBlock,
)

TOPIC_URL = "https://hk.finance.yahoo.com/topic/latest-news/"
REQUEST_TIMEOUT = 40
DEFAULT_MAX_CANDIDATES = 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}
DEFAULT_STRIP_PATTERNS = (
    "網址[:：]",
    r"www\.",
    "免責聲明",
    "風險提示",
    "以上內容",
    "僅供參考",
    "下載.*APP",
    "更多精彩",
    "關注我們",
    "掃碼",
    "阿思達克財經",
)
# 整段丢弃关键词（订阅/推广/免责等与正文无关的段落）
DEFAULT_DROP_KEYWORDS = (
    "訂閱", "订阅", "通訊", "通讯", "點擊這裡", "点击这里", "請點擊", "请点击",
    "免費", "免费", "關注我們", "关注我们", "掃碼", "扫码", "下載", "下载",
    "更多精彩", "newsletter", "subscribe", "免責", "免责",
)
DEFAULT_BYLINE_MAX_CHARS = 6
EMPHASIS_PATTERN = re.compile(r"<em[\s>/]", re.IGNORECASE)
SENTENCE_PUNCT_PATTERN = re.compile(r"[。，、；：！？.!?,;:\"'「」『』（）()《》〈〉…—～~]")
CJK_NAME_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}")
LATIN_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z.\- ]*")
LIST_ITEM_PATTERN = re.compile(r'<li class="list-item[^>]*>(.*?)</li>', re.DOTALL)
LINK_PATTERN = re.compile(r'href="(https://hk\.finance\.yahoo\.com/news/[^"]+\.html)"')
TITLE_ATTR_PATTERN = re.compile(r'<a[^>]+href="https://hk\.finance\.yahoo\.com/news/[^"]+"[^>]*title="([^"]*)"')
H3_PATTERN = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL)
PUBLISHER_PATTERN = re.compile(r'<span class="publisher">(.*?)</span>', re.DOTALL)
DATE_PATTERN = re.compile(r'<span class="published-date">(.*?)</span>', re.DOTALL)
ARTICLE_TITLE_PATTERN = re.compile(r'<h1[^>]*class="[^"]*cover-title[^"]*"[^>]*>(.*?)</h1>', re.DOTALL)
OG_TITLE_PATTERN = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.DOTALL)
AUTHOR_PATTERN = re.compile(r'class="byline-attr-author[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
PARAGRAPH_PATTERN = re.compile(
    r'<p[^>]*class="[^"]*text-block paragraph[^"]*"[^>]*>(.*?)</p>', re.DOTALL
)
FIGURE_PATTERN = re.compile(
    r'<figure[^>]*data-testid="article-figure-image"[^>]*>(.*?)</figure>', re.DOTALL
)
FIGURE_HREF_PATTERN = re.compile(r'<a[^>]+href="([^"]+)"')
IMG_SRC_PATTERN = re.compile(r'<img[^>]+src="(https?://[^"]+)"')
FIGCAPTION_PATTERN = re.compile(r"<figcaption[^>]*>(.*?)</figcaption>", re.DOTALL)
BLOCK_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"[ \t\u00a0]+")
EMBEDDED_URL_PATTERN = re.compile(r"(?i)https?%3a%2f%2f(.+)$")


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


def _strip_patterns() -> list[re.Pattern]:
    raw = os.getenv("NEWS_STRIP_PATTERNS", ",".join(DEFAULT_STRIP_PATTERNS))
    patterns: list[re.Pattern] = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            patterns.append(re.compile(item))
    return patterns


def _clean_paragraph(text: str, patterns: list[re.Pattern]) -> str:
    """去掉段尾的来源/推广杂质（同一段内以标记切尾），空段返回空串。"""
    text = text.strip()
    cut = len(text)
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            cut = min(cut, match.start())
    text = text[:cut].strip()
    text = re.sub(r"[\s~·]+$", "", text)
    text = re.sub(r"[（(]\s*[A-Za-z]{1,5}(?:\s*/\s*[A-Za-z]{1,5})?\s*[)）]\s*$", "", text)
    return text.strip() if len(text.strip()) >= 2 else ""


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _drop_keywords() -> list[str]:
    raw = os.getenv("NEWS_DROP_KEYWORDS")
    if raw is None:
        return list(DEFAULT_DROP_KEYWORDS)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _is_meta_paragraph(raw_html: str, text: str, drop_keywords: list[str], emphasis: bool) -> bool:
    """判断是否为署名/订阅/推广等非正文段落。"""
    if emphasis and EMPHASIS_PATTERN.search(raw_html):
        return True
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in drop_keywords)


def _looks_like_byline(text: str, max_chars: int) -> bool:
    """末尾短署名启发式：无标点、≤N 字的中文短名或短英文名。"""
    if max_chars <= 0:
        return False
    text = text.strip()
    if not text or len(text) > max_chars:
        return False
    if SENTENCE_PUNCT_PATTERN.search(text):
        return False
    if CJK_NAME_PATTERN.fullmatch(text):
        return True
    if LATIN_NAME_PATTERN.fullmatch(text) and len(text.split()) <= 3:
        return True
    return False


def _trim_trailing_byline(blocks: list, max_chars: int) -> list:
    """删除正文末尾连续出现的短署名段（最多 2 段，至少保留 1 段正文）。"""
    if max_chars <= 0:
        return blocks
    text_count = sum(1 for block in blocks if isinstance(block, TextBlock))
    removed = 0
    while blocks and removed < 2 and text_count > 1:
        last = blocks[-1]
        if not isinstance(last, TextBlock) or not _looks_like_byline(last.text, max_chars):
            break
        blocks.pop()
        text_count -= 1
        removed += 1
    return blocks


def _original_image_url(href: str) -> str:
    """从 Yahoo 代理地址里解出原始图片地址，失败则用原地址。"""
    match = EMBEDDED_URL_PATTERN.search(href)
    if match:
        target = unquote(match.group(1))
        if target.startswith("media.") or target.startswith("s.yimg.com"):
            return "https://" + target
        return "https://" + target
    return href


class YahooHkFinanceSite(NewsSite):
    """Yahoo 香港財經最新新聞。"""

    key = "yahoo_hk_finance"
    name = "Yahoo香港財經"

    def list_items(self) -> list[NewsItem]:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
        try:
            resp = _session().get(TOPIC_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CrawlerError(f"抓取 Yahoo 列表页失败：{exc}") from exc
        resp.encoding = "utf-8"
        items: list[NewsItem] = []
        seen: set[str] = set()
        for chunk in LIST_ITEM_PATTERN.findall(resp.text):
            if "native-ad" in chunk:
                continue
            link = LINK_PATTERN.search(chunk)
            if not link:
                continue
            url = link.group(1)
            if url in seen:
                continue
            title = ""
            title_attr = TITLE_ATTR_PATTERN.search(chunk)
            if title_attr:
                title = html_module.unescape(title_attr.group(1)).strip()
            if not title:
                h3 = H3_PATTERN.search(chunk)
                if h3:
                    title = _clean_text(h3.group(1))
            if not title:
                continue
            seen.add(url)
            publisher = PUBLISHER_PATTERN.search(chunk)
            published = DATE_PATTERN.search(chunk)
            items.append(
                NewsItem(
                    title=title,
                    url=url,
                    source=_clean_text(publisher.group(1)) if publisher else "",
                    published=_clean_text(published.group(1)) if published else "",
                )
            )
            if len(items) >= limit:
                break
        return items

    def fetch(self, item: NewsItem) -> NewsArticle:
        try:
            resp = _session().get(item.url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CrawlerError(f"抓取新闻正文失败：{item.url}：{exc}") from exc
        resp.encoding = "utf-8"
        article = parse_article_html(resp.text, fallback_title=item.title, url=item.url)
        article.source = item.source or article.author
        return article


SITE = YahooHkFinanceSite()


def parse_article_html(page: str, fallback_title: str = "", url: str = "") -> NewsArticle:
    """从新闻正文 HTML 中解析出文章（供自检复用）。"""
    site = YahooHkFinanceSite()
    item = NewsItem(title=fallback_title, url=url)
    resp_text = page
    title = ""
    title_match = ARTICLE_TITLE_PATTERN.search(resp_text) or OG_TITLE_PATTERN.search(resp_text)
    if title_match:
        title = _clean_text(title_match.group(1)) or fallback_title
    title = title or fallback_title
    author_match = AUTHOR_PATTERN.search(resp_text)
    author = _clean_text(author_match.group(1)) if author_match else ""
    body_start = resp_text.find("bodyItems-wrapper")
    body = resp_text[body_start:] if body_start >= 0 else resp_text
    patterns = _strip_patterns()
    drop_keywords = _drop_keywords()
    emphasis = _truthy(os.getenv("NEWS_DROP_EMPHASIS"), True)
    blocks: list[tuple[int, TextBlock | ImageBlock]] = []
    for match in PARAGRAPH_PATTERN.finditer(body):
        raw = match.group(1)
        text = _clean_text(raw)
        if _is_meta_paragraph(raw, text, drop_keywords, emphasis):
            continue
        text = _clean_paragraph(text, patterns)
        if not text:
            continue
        blocks.append((match.start(), TextBlock(text)))
    for match in FIGURE_PATTERN.finditer(body):
        href = FIGURE_HREF_PATTERN.search(match.group(1))
        if href:
            image_url = _original_image_url(href.group(1))
        else:
            img = IMG_SRC_PATTERN.search(match.group(1))
            image_url = img.group(1) if img else ""
        if not image_url or image_url.startswith("data:"):
            continue
        caption_match = FIGCAPTION_PATTERN.search(match.group(1))
        caption = _clean_text(caption_match.group(1)) if caption_match else ""
        blocks.append((match.start(), ImageBlock(image_url, caption)))
    blocks.sort(key=lambda pair: pair[0])
    ordered = [block for _, block in blocks]
    if not any(isinstance(block, TextBlock) for block in ordered):
        raise CrawlerError(f"未解析到正文文本（页面结构可能已变化）：{url or fallback_title}")
    byline_max = int(os.getenv("NEWS_BYLINE_MAX_CHARS") or DEFAULT_BYLINE_MAX_CHARS)
    ordered = _trim_trailing_byline(ordered, byline_max)
    return NewsArticle(
        title=title,
        blocks=ordered,
        author=author,
        source_url=url,
    )


def _selftest() -> None:
    page = (
        '<html><head><meta property="og:title" content="測試標題"></head><body>'
        '<div data-testid="article-body"><div class="bodyItems-wrapper">'
        '<p class="text text-block paragraph yf-x">第一段正文內容。</p>'
        '<div class="wrapper" data-testid="inarticle-ad"><div data-testid="ad-container"></div></div>'
        '<figure data-testid="article-figure-image"><div class="image-wrapper">'
        '<a href="https://s.yimg.com/lo/mysterio/api/abc/lightyear_networkapi/'
        'resizefit_w960%3Bquality_80%3Bformat_webp/https%3A%2F%2Fmedia.zenfs.com%2Fko%2Ftest.jpg">'
        '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></a>'
        "</div><figcaption>圖注文字</figcaption></figure>"
        '<p class="text text-block paragraph yf-x">阿思達克財經網 網址: www.aastocks.com</p>'
        '<p class="text text-block paragraph yf-x">第二段正文內容。</p>'
        "</div></div></body></html>"
    )
    article = parse_article_html(page, fallback_title="備用標題", url="http://x")
    kinds = [type(block).__name__ for block in article.blocks]
    assert article.title == "測試標題", article.title
    assert kinds == ["TextBlock", "ImageBlock", "TextBlock"], kinds
    assert article.blocks[1].url == "https://media.zenfs.com/ko/test.jpg", article.blocks[1].url
    assert article.blocks[1].caption == "圖注文字", article.blocks[1].caption
    assert all("aastocks" not in b.text for b in article.blocks if isinstance(b, TextBlock))
    assert _clean_paragraph("正文內容。(ST)", _strip_patterns()) == "正文內容。"
    assert _clean_paragraph("正文。(jl/da)~阿思達克財經網 網址: www.aastocks.com", _strip_patterns()) == "正文。"
    assert _clean_paragraph("正文。(gc/u)~阿思達克財經新聞", _strip_patterns()) == "正文。"
    os.environ["NEWS_MAX_CANDIDATES"] = ""
    try:
        assert int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES) == DEFAULT_MAX_CANDIDATES
    finally:
        os.environ.pop("NEWS_MAX_CANDIDATES", None)

    saved = {
        key: os.environ.pop(key, None)
        for key in ("NEWS_DROP_EMPHASIS", "NEWS_DROP_KEYWORDS", "NEWS_BYLINE_MAX_CHARS")
    }
    try:
        page2 = (
            '<html><body><div data-testid="article-body"><div class="bodyItems-wrapper">'
            '<p class="text text-block paragraph yf-x">第一段正文內容。</p>'
            '<p class="text text-block paragraph yf-x"><em>李世達</em></p>'
            '<p class="text text-block paragraph yf-x"><em>欲訂閱詠竹坊每周免費通訊，請點擊</em>'
            '<a href="https://thebambooworks.com/zh/x">這裡</a></p>'
            '<p class="text text-block paragraph yf-x">第二段正文內容。</p>'
            '<p class="text text-block paragraph yf-x">張三</p>'
            "</div></div></body></html>"
        )
        article2 = parse_article_html(page2, fallback_title="標題", url="http://y")
        texts = [block.text for block in article2.blocks if isinstance(block, TextBlock)]
        assert texts == ["第一段正文內容。", "第二段正文內容。"], texts
        assert _looks_like_byline("李世達", 6)
        assert _looks_like_byline("張三", 6)
        assert not _looks_like_byline("第一段正文內容。", 6)
        assert not _looks_like_byline("很長的結尾句子", 6)
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    print("sites/yahoo_hk_finance.py 自检通过")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    _selftest()
