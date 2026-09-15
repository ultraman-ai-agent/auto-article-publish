"""AIBase 资讯站点插件（中文源，纯 HTTP，不走翻译链路）。

列表：https://news.aibase.com/zh/news（Nuxt SSR）每页 20 条 `<a href="/zh/news/<id>">` 卡片；
      标题在 `class="… font600 …"` 的 div 里，元信息只有相对时间（如「今日」）与阅读量。
      注意：分页是前端渲染的（`?page=N` 返回同一批内容），且站内无 RSS，故候选固定 20 条、不做兜底。
正文：`div.articleContent > div.post-content` 到文末；取 `<p>`（>40 字）与 upload.chinaz.com 图片。
      `needs_translation = False`（内容已是简体中文，跳过 MiniMax 翻译），
      因此这里自带一层中文推广/署名段过滤补偿 translator 的清理。
"""

from __future__ import annotations

import html as html_module
import os
import re

from sites.base import (
    CrawlerError,
    ImageBlock,
    NewsArticle,
    NewsItem,
    NewsSite,
    TextBlock,
    http_get,
)

LIST_URL = "https://news.aibase.com/zh/news"
SITE_HOST = "https://news.aibase.com"
SITE_SOURCE = "AIBase"
REQUEST_TIMEOUT = 40
DEFAULT_MAX_CANDIDATES = 20
MIN_PARAGRAPH_CHARS = 40
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9",
}
CARD_PATTERN = re.compile(r'<a href="(/zh/news/\d+)"[^>]*>(.*?)</a>', re.DOTALL)
TITLE_PATTERN = re.compile(r'<div[^>]*class="[^"]*font600[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
TIME_TEXT_PATTERN = re.compile(r"icon-rili[^>]*>\s*</i>\s*([^<]{1,20})<", re.DOTALL)
H1_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
BODY_MARKER = "post-content"
PARAGRAPH_PATTERN = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)
IMG_SRC_PATTERN = re.compile(r'<img[^>]+src="(https://upload\.chinaz\.com/[^"]+)"', re.DOTALL)
BLOCK_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"[ \t\u00a0]+")
# 中文推广/署名段（跳过翻译后由这里兜住，命中即整段丢弃）
DROP_KEYWORDS = (
    "来源：", "编辑：", "本文由", "关注我们", "扫码", "广告", "推广",
    "免责声明", "未经授权", "转载自", "阅读原文", "更多精彩", "点击关注",
)


def _clean_text(raw: str) -> str:
    text = BLOCK_COMMENT_PATTERN.sub("", raw or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = TAG_PATTERN.sub("", text)
    text = html_module.unescape(text)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def _is_promo(text: str) -> bool:
    return any(keyword in text for keyword in DROP_KEYWORDS)


class AibaseSite(NewsSite):
    """AIBase 中文 AI 资讯。"""

    key = "aibase"
    name = "AIBase 资讯"
    needs_translation = False

    def list_items(self) -> list[NewsItem]:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
        resp = http_get(LIST_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        return _parse_listing(resp.text, limit)

    def fetch(self, item: NewsItem) -> NewsArticle:
        resp = http_get(item.url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        return parse_article_html(resp.text, fallback_title=item.title, url=item.url)


SITE = AibaseSite()


def _parse_listing(listing_html: str, limit: int | None = None) -> list[NewsItem]:
    """解析 /zh/news 的卡片（供自检复用）。"""
    if limit is None:
        limit = int(os.getenv("NEWS_MAX_CANDIDATES") or DEFAULT_MAX_CANDIDATES)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for path, inner in CARD_PATTERN.findall(listing_html):
        url = SITE_HOST + path
        if url in seen:
            continue
        title_match = TITLE_PATTERN.search(inner)
        title = _clean_text(title_match.group(1)) if title_match else ""
        if not title:
            continue
        seen.add(url)
        time_match = TIME_TEXT_PATTERN.search(inner)
        items.append(
            NewsItem(
                title=title,
                url=url,
                source=SITE_SOURCE,
                published=_clean_text(time_match.group(1)) if time_match else "",
            )
        )
        if len(items) >= limit:
            break
    return items


def parse_article_html(page: str, fallback_title: str = "", url: str = "") -> NewsArticle:
    """从文章 HTML 中解析正文（供自检复用）。"""
    title_match = H1_PATTERN.search(page)
    title = _clean_text(title_match.group(1)) if title_match else ""
    title = title or fallback_title

    start = page.find(BODY_MARKER)
    if start < 0:
        raise CrawlerError(f"未找到正文容器（页面结构可能已变化）：{url or fallback_title}")
    window = page[start:]

    blocks: list[tuple[int, TextBlock | ImageBlock]] = []
    for match in PARAGRAPH_PATTERN.finditer(window):
        text = _clean_text(match.group(1))
        if len(text) < MIN_PARAGRAPH_CHARS or _is_promo(text):
            continue
        blocks.append((match.start(), TextBlock(text)))
    seen_images: set[str] = set()
    for match in IMG_SRC_PATTERN.finditer(window):
        image_url = html_module.unescape(match.group(1)).strip()
        if not image_url or image_url in seen_images:
            continue
        seen_images.add(image_url)
        blocks.append((match.start(), ImageBlock(image_url)))
    blocks.sort(key=lambda pair: pair[0])
    ordered = [block for _, block in blocks]
    if not any(isinstance(block, TextBlock) for block in ordered):
        raise CrawlerError(f"未解析到正文文本（页面结构可能已变化）：{url or fallback_title}")
    article = NewsArticle(title=title, blocks=ordered, source=SITE_SOURCE, source_url=url)
    article.author = SITE_SOURCE
    return article


def _selftest() -> None:
    listing = (
        '<a href="/zh/news/31063" class=""><div class="bg-white smallRadius">'
        '<div class="smallRadius md:w-[246px]"><img src="https://upload.chinaz.com/2026/0915/a.jpg" '
        'alt="标题"></div><div class="flex-1 flex flex-col">'
        '<div class="md:text-[18px] font600 mainColor md:truncate1">'
        "跃跃宣布StepAudio3系列模型: 四款模型同步上线开放平台</div>"
        '<div class="hidden md:block"><div class="text-[14px] tipColor truncate2">摘要文本</div></div>'
        '<div class="tipColor flex gap-[32px]"><div class="flex items-center gap-[4px]">'
        '<i class="iconfont icon-rili text-[14px]"></i> 今日</div>'
        '<div class="flex items-center gap-[4px]"><i class="iconfont icon-fangwenliang1"></i> 5.2K'
        "</div></div></div></div></a>"
        '<a href="/zh/news/31062" class=""><div><img src="https://upload.chinaz.com/2026/0915/b.jpg">'
        '<div class="font600 mainColor">第二条新闻标题</div>'
        '<i class="iconfont icon-rili"></i> 3小时前</div></a>'
        '<a href="/zh/about" class=""><div class="font600">不是新闻的链接</div></a>'
    )
    items = _parse_listing(listing, 20)
    assert len(items) == 2, items
    assert items[0].title == "跃跃宣布StepAudio3系列模型: 四款模型同步上线开放平台", items[0].title
    assert items[0].url == "https://news.aibase.com/zh/news/31063", items[0].url
    assert items[0].published == "今日", items[0].published
    assert items[1].published == "3小时前", items[1].published
    assert _parse_listing(listing, 1) == items[:1]

    page = (
        "<html><head><title>忽略</title></head><body>"
        '<h1 class="text-[24px] font600">跃跃宣布StepAudio3系列模型: 四款模型同步上线开放平台</h1>'
        '<div class="flex gap-x-[40px]"><i class="iconfont icon-rili"></i><span>发布时间 : '
        "2026年9月15日 16:26</span></div>"
        '<div class="articleContent"><div class="overflow-hidden post-content text-wrap">'
        "<p>跃跃是阶跃星辰的语音模型系列，本次上线 Realtime、ASR、TTS、Gen、Music 四款模型，"
        "并在 Artificial Analysis 榜单上取得第一，覆盖实时对话与音乐生成等场景。</p>"
        '<p style="text-align:center">'
        '<img src="https://upload.chinaz.com/2026/0915/shot.jpg" alt="截图"/></p>'
        "<p>来源：AIbase 编辑部</p>"
        "<p>StepAudio3 ASR 在中英粤等多语种识别上表现突出，专业场景的 WER 低至 1.7%，"
        "TTS 支持音色克隆与停顿、笑声等副语言表现，可用于有声内容制作。</p>"
        '<img src="/_nuxt/userlogo.q1jFctRw.png" alt="avatar">'
        "</div></div></body></html>"
    )
    article = parse_article_html(page, fallback_title="備用標題", url="http://x")
    kinds = [type(block).__name__ for block in article.blocks]
    assert article.title == "跃跃宣布StepAudio3系列模型: 四款模型同步上线开放平台", article.title
    assert kinds == ["TextBlock", "ImageBlock", "TextBlock"], kinds
    assert article.blocks[1].url.endswith("shot.jpg"), article.blocks[1].url
    texts = [block.text for block in article.blocks if isinstance(block, TextBlock)]
    assert all("来源：" not in text for text in texts), texts
    assert not SITE.needs_translation
    print("sites/aibase.py 自检通过")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    _selftest()
