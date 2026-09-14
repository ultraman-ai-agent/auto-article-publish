"""小红书发布模块：通过本地 xiaohongshu-mcp 服务发布图文笔记。"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from generate import CONTENT_MAX_LEN, TITLE_MAX_LEN, Article

DEFAULT_MCP_URL = "http://localhost:18060/mcp"
DEFAULT_VISIBILITY = "公开可见"
NOT_LOGGED_IN_HINTS = ("未登录", "not login", "not logged", "please login")
STATUS_TIMEOUT_SECONDS = 60
SEARCH_TIMEOUT_SECONDS = 90
PUBLISH_TIMEOUT_SECONDS = 330
PUBLISH_TIMEOUT_HINT = "发布可能仍在小红书后台进行，请先到 App 确认是否已发出，避免重复发布；也可用非无头模式（MCP_HEADLESS=false）查看浏览器卡在哪一步"
BASE64_DATA_URL_PATTERN = re.compile(r"data:image/[a-zA-Z]+;base64,([A-Za-z0-9+/=\s]+)")
BASE64_BLOB_PATTERN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


class XhsError(Exception):
    """小红书发布失败。"""


def is_logged_in(status_text: str) -> bool:
    """根据 check_login_status 返回文本判断是否已登录。"""
    return not any(hint in status_text.lower() for hint in NOT_LOGGED_IN_HINTS)


async def _call_tool(
    session: ClientSession,
    name: str,
    arguments: dict,
    timeout_seconds: int = STATUS_TIMEOUT_SECONDS,
    timeout_hint: str = "",
) -> str:
    """调用 MCP 工具并返回文本结果，带超时保护。"""
    try:
        result = await session.call_tool(
            name, arguments, read_timeout_seconds=timedelta(seconds=timeout_seconds)
        )
    except Exception as exc:  # noqa: BLE001 统一转为中文错误
        message = f"MCP 工具 {name} 调用失败或超时（{timeout_seconds}s）：{exc}"
        if timeout_hint:
            message = f"{message}。{timeout_hint}"
        raise XhsError(message) from exc
    texts = [getattr(item, "text", "") for item in result.content]
    output = "\n".join(t for t in texts if t)
    if getattr(result, "isError", False):
        raise XhsError(f"MCP 工具 {name} 执行失败：{output}")
    return output


def _extract_qrcode_base64(result) -> str:
    """从 MCP 结果中提取二维码 base64，兼容图片内容与文本内嵌两种形式。"""
    texts: list[str] = []
    for item in result.content:
        if getattr(item, "type", "") == "image":
            data = getattr(item, "data", "")
            if data:
                return data
        text = getattr(item, "text", "") or ""
        if text:
            texts.append(text)
    blobs = " \n".join(texts)
    match = BASE64_DATA_URL_PATTERN.search(blobs)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match = BASE64_BLOB_PATTERN.search(blobs)
    if match:
        return match.group(0)
    raise XhsError(f"未从 MCP 返回中解析到二维码：{blobs[:200]}")


def _validate(article: Article) -> None:
    if len(article.title) > TITLE_MAX_LEN:
        raise XhsError(f"标题超过 {TITLE_MAX_LEN} 字：{article.title}")
    if len(article.content) > CONTENT_MAX_LEN:
        raise XhsError(f"正文超过 {CONTENT_MAX_LEN} 字")
    if not article.images:
        raise XhsError("缺少配图，无法发布")
    missing = [p for p in article.images if not Path(p).exists()]
    if missing:
        raise XhsError(f"配图文件不存在：{missing}")


async def _publish_async(article: Article, visibility: str, is_original: bool) -> str:
    url = os.getenv("XHS_MCP_URL", DEFAULT_MCP_URL).strip()
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            login_status = await _call_tool(session, "check_login_status", {})
            if not is_logged_in(login_status):
                raise XhsError(f"小红书未登录，请在界面点“扫码登录”扫码。状态：{login_status}")

            result = await _call_tool(
                session,
                "publish_content",
                {
                    "title": article.title,
                    "content": article.content,
                    "images": article.images,
                    "tags": article.tags,
                    "visibility": visibility,
                    "is_original": is_original,
                },
                timeout_seconds=PUBLISH_TIMEOUT_SECONDS,
                timeout_hint=PUBLISH_TIMEOUT_HINT,
            )
            if any(word in result for word in ("失败", "错误", "error", "fail")):
                raise XhsError(f"发布返回异常：{result}")
            return result


def publish(article: Article, visibility: str | None = None, is_original: bool = False) -> str:
    """发布图文笔记到小红书，返回 MCP 结果文本。"""
    _validate(article)
    visibility = (visibility or os.getenv("XHS_VISIBILITY", DEFAULT_VISIBILITY)).strip()
    try:
        return asyncio.run(_publish_async(article, visibility, is_original))
    except XhsError:
        raise
    except Exception as exc:
        raise XhsError(f"连接小红书 MCP 服务失败，请确认已启动 xiaohongshu-mcp-windows-amd64.exe：{exc}") from exc


async def _status_async() -> str:
    url = os.getenv("XHS_MCP_URL", DEFAULT_MCP_URL).strip()
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await _call_tool(session, "check_login_status", {})


def check_login() -> str:
    """检查小红书登录状态，返回 MCP 结果文本。"""
    try:
        return asyncio.run(_status_async())
    except Exception as exc:
        raise XhsError(f"连接小红书 MCP 服务失败，请确认已启动 xiaohongshu-mcp-windows-amd64.exe：{exc}") from exc


async def _qrcode_async() -> str:
    url = os.getenv("XHS_MCP_URL", DEFAULT_MCP_URL).strip()
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                result = await session.call_tool(
                    "get_login_qrcode", {}, read_timeout_seconds=timedelta(seconds=STATUS_TIMEOUT_SECONDS)
                )
            except Exception as exc:  # noqa: BLE001
                raise XhsError(f"获取登录二维码超时或失败（{STATUS_TIMEOUT_SECONDS}s）：{exc}") from exc
            if getattr(result, "isError", False):
                raise XhsError("获取登录二维码失败")
            return _extract_qrcode_base64(result)


def get_login_qrcode() -> str:
    """获取登录二维码的 base64 图片数据。"""
    try:
        return asyncio.run(_qrcode_async())
    except XhsError:
        raise
    except Exception as exc:
        raise XhsError(f"获取登录二维码失败，请确认 MCP 服务已启动：{exc}") from exc


async def _search_async(keyword: str) -> str:
    url = os.getenv("XHS_MCP_URL", DEFAULT_MCP_URL).strip()
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await _call_tool(
                session, "search_feeds", {"keyword": keyword}, timeout_seconds=SEARCH_TIMEOUT_SECONDS
            )


def search_feeds_raw(keyword: str) -> str:
    """搜索小红书内容，返回 MCP 原始文本。"""
    try:
        return asyncio.run(_search_async(keyword))
    except XhsError:
        raise
    except Exception as exc:
        raise XhsError(f"搜索小红书内容失败，请确认 MCP 服务已启动：{exc}") from exc


def search_feeds(keyword: str, limit: int = 5) -> list[str]:
    """搜索小红书内容，返回参考用标题列表。"""
    raw = search_feeds_raw(keyword)
    try:
        import json

        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    titles: list[str] = []
    for feed in data.get("feeds", [])[: limit * 2]:
        card = feed.get("noteCard") or {}
        title = (card.get("displayTitle") or card.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    try:
        print(check_login())
    except XhsError as exc:
        print(f"失败：{exc}")
