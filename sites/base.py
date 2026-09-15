"""新闻站点插件基类：统一采集接口与数据结构。

新增站点只需在 sites/ 下放一个模块，暴露名为 SITE 的 NewsSite 实例即可自动注册。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import requests

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SEC = 2
DEFAULT_REQUEST_TIMEOUT = 40
RETRYABLE_STATUS = (403, 408, 429, 500, 502, 503, 504)


class CrawlerError(Exception):
    """新闻抓取失败。"""


def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
) -> requests.Response:
    """带退避重试的 GET（跨境源偶发 TLS 重置时靠它兜住），失败抛 CrawlerError。

    重试次数与退避基数走 NEWS_HTTP_MAX_RETRIES / NEWS_HTTP_RETRY_DELAY_SEC（默认 3 / 2s）。
    """
    retries = int(os.getenv("NEWS_HTTP_MAX_RETRIES") or DEFAULT_MAX_RETRIES)
    delay = int(os.getenv("NEWS_HTTP_RETRY_DELAY_SEC") or DEFAULT_RETRY_DELAY_SEC)
    session = requests.Session()
    if headers:
        session.headers.update(headers)
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout or DEFAULT_REQUEST_TIMEOUT)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code not in RETRYABLE_STATUS:
                break
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(delay * attempt)
    raise CrawlerError(f"请求失败（已重试 {retries} 次）：{url}：{last_error}")


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
    # 该站内容是否还需要过翻译链路（中文源可置 False，跳过英译简/繁转简）
    needs_translation: bool = True

    def list_items(self) -> list[NewsItem]:
        """抓取列表页，返回候选新闻。"""
        raise NotImplementedError

    def fetch(self, item: NewsItem) -> NewsArticle:
        """抓取单篇正文。"""
        raise NotImplementedError


def _selftest() -> None:
    """离线校验 http_get 的重试与失败语义（monkeypatch session）。"""
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.encoding = ""
            self.text = "ok"

    real_get = requests.Session.get
    saved_retries = os.environ.get("NEWS_HTTP_MAX_RETRIES")
    saved_delay = os.environ.get("NEWS_HTTP_RETRY_DELAY_SEC")
    os.environ["NEWS_HTTP_RETRY_DELAY_SEC"] = "0"  # 自检不真等
    os.environ.pop("NEWS_HTTP_MAX_RETRIES", None)
    try:
        def flaky(self, url, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise requests.exceptions.SSLError("boom")
            return FakeResp(200)

        requests.Session.get = flaky
        assert http_get("https://example.com").status_code == 200
        assert calls["n"] == 2, calls

        calls["n"] = 0

        def not_found(self, url, timeout=None):
            calls["n"] += 1
            return FakeResp(404)

        requests.Session.get = not_found
        try:
            http_get("https://example.com")
            raise AssertionError("404 不该被当成成功")
        except CrawlerError:
            pass
        assert calls["n"] == 1, calls  # 非可重试状态码不重试

        calls["n"] = 0

        def always_timeout(self, url, timeout=None):
            calls["n"] += 1
            raise requests.exceptions.Timeout("t")

        requests.Session.get = always_timeout
        try:
            http_get("https://example.com")
            raise AssertionError("连续超时应抛错")
        except CrawlerError as exc:
            assert "已重试 3 次" in str(exc), exc
        assert calls["n"] == 3, calls

        os.environ["NEWS_HTTP_MAX_RETRIES"] = "1"
        calls["n"] = 0
        try:
            http_get("https://example.com")
            raise AssertionError("应抛错")
        except CrawlerError:
            pass
        assert calls["n"] == 1, calls
    finally:
        requests.Session.get = real_get
        os.environ.pop("NEWS_HTTP_MAX_RETRIES", None)
        if saved_retries is not None:
            os.environ["NEWS_HTTP_MAX_RETRIES"] = saved_retries
        if saved_delay is None:
            os.environ.pop("NEWS_HTTP_RETRY_DELAY_SEC", None)
        else:
            os.environ["NEWS_HTTP_RETRY_DELAY_SEC"] = saved_delay
    print("sites/base.py 自检通过")


if __name__ == "__main__":
    _selftest()
