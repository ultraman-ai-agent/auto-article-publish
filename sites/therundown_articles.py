"""The Rundown AI·简报 站点插件（/articles 每日简报，纯 HTTP）。

列表：`https://www.therundown.ai/feed`（RSS，一次 50 条，全是 /articles），受
      `NEWS_MAX_CANDIDATES` 约束（未配置时默认 30 条）；不足时翻 `/articles` 列表补齐。
正文：`id="content-blocks"` 到 `<footer` 之间的段落与图片，丢弃 `TOGETHER WITH` 等赞助/推广段。

共用逻辑见 `sites/_therundown.py`；正文为英文，依赖 translator 的英译简能力。
"""

from __future__ import annotations

import os

from sites import _therundown as trd
from sites.base import NewsArticle, NewsItem, NewsSite


class TherundownArticlesSite(NewsSite):
    """The Rundown AI 每日简报。"""

    key = "therundown_articles"
    name = "The Rundown AI·简报"

    def list_items(self) -> list[NewsItem]:
        return trd.list_articles_items()

    def fetch(self, item: NewsItem) -> NewsArticle:
        return trd.fetch_article(item)


SITE = TherundownArticlesSite()


def _selftest() -> None:
    assert SITE.key == "therundown_articles" and SITE.name == "The Rundown AI·简报"
    assert trd.candidates_limit() == int(os.getenv("NEWS_MAX_CANDIDATES") or 30)
    print("sites/therundown_articles.py 自检通过")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    _selftest()
