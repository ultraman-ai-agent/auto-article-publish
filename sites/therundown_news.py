"""The Rundown AI·新闻 站点插件（/news 单条 AI 新闻，纯 HTTP）。

列表：`https://www.therundown.ai/news` 分页卡片（每页约 19 条），受 `NEWS_MAX_CANDIDATES`
      约束（未配置时默认 30 条）；主源失败时兜底 `news-sitemap.xml`（通常只有 1~2 条）。
正文：`class="news-prose"`（含 hero 图）到 `<footer` 之间的段落与 `/news/images/*.webp`。

共用逻辑见 `sites/_therundown.py`；正文为英文，依赖 translator 的英译简能力。
"""

from __future__ import annotations

from sites import _therundown as trd
from sites.base import NewsArticle, NewsItem, NewsSite


class TherundownNewsSite(NewsSite):
    """The Rundown AI 单条新闻。"""

    key = "therundown_news"
    name = "The Rundown AI·新闻"

    def list_items(self) -> list[NewsItem]:
        return trd.list_news_items()

    def fetch(self, item: NewsItem) -> NewsArticle:
        return trd.fetch_news(item)


SITE = TherundownNewsSite()


def _selftest() -> None:
    assert SITE.key == "therundown_news" and SITE.name == "The Rundown AI·新闻"
    assert trd.candidates_limit() == int(__import__("os").getenv("NEWS_MAX_CANDIDATES") or 30)
    print("sites/therundown_news.py 自检通过")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    _selftest()
