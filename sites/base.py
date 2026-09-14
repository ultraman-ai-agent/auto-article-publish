"""新闻站点插件基类：统一采集接口与数据结构。

新增站点只需在 sites/ 下放一个模块，暴露名为 SITE 的 NewsSite 实例即可自动注册。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class CrawlerError(Exception):
    """新闻抓取失败。"""


@dataclass
class NewsItem:
    """列表页的一条候选新闻。"""

    title: str
    url: str
    source: str = ""
    published: str = ""


@dataclass
class TextBlock:
    """正文文本块。"""

    text: str


@dataclass
class ImageBlock:
    """正文图片块。"""

    url: str
    caption: str = ""
    local: str = ""


@dataclass
class NewsArticle:
    """一篇抓取到的新闻，正文按阅读顺序保存为块列表。"""

    title: str
    blocks: list[TextBlock | ImageBlock] = field(default_factory=list)
    author: str = ""
    source_url: str = ""
    source: str = ""


class NewsSite:
    """新闻站点插件接口。"""

    key: str = ""
    name: str = ""

    def list_items(self) -> list[NewsItem]:
        """抓取列表页，返回候选新闻。"""
        raise NotImplementedError

    def fetch(self, item: NewsItem) -> NewsArticle:
        """抓取单篇正文。"""
        raise NotImplementedError
