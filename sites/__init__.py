"""新闻站点插件注册：自动发现 sites 包内暴露 SITE 的模块。

插件形式：在 sites/ 下新增一个模块，暴露模块级变量 SITE（NewsSite 实例）即可被自动注册。
"""

from __future__ import annotations

import importlib
import pkgutil

from sites.base import (
    CrawlerError,
    ImageBlock,
    NewsArticle,
    NewsItem,
    NewsSite,
    TextBlock,
)

_REGISTRY: dict[str, NewsSite] = {}


def _discover() -> None:
    for _, name, _ in pkgutil.iter_modules(__path__):
        if name.startswith("_") or name == "base":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except Exception:  # noqa: BLE001 单个插件导入失败不影响其它站点
            continue
        site = getattr(module, "SITE", None)
        if isinstance(site, NewsSite) and site.key:
            _REGISTRY[site.key] = site


def available_sites() -> dict[str, str]:
    """返回 {站点 key: 站点名称}。"""
    if not _REGISTRY:
        _discover()
    return {key: site.name or key for key, site in _REGISTRY.items()}


def get_site(key: str) -> NewsSite:
    """按 key 取站点插件，未注册则报错。"""
    if not _REGISTRY:
        _discover()
    site = _REGISTRY.get(key)
    if site is None:
        names = ", ".join(_REGISTRY) or "（无）"
        raise CrawlerError(f"未注册的新闻站点：{key}（可用：{names}）")
    return site


__all__ = [
    "CrawlerError",
    "ImageBlock",
    "NewsArticle",
    "NewsItem",
    "NewsSite",
    "TextBlock",
    "available_sites",
    "get_site",
]
