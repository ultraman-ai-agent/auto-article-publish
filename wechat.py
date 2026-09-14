"""微信公众号发布：调用 wechatsync CLI，将 Markdown 同步为公众号草稿。

wechatsync 依赖 Chrome 扩展在线（浏览器登录态），默认存草稿；
CLI 同步结果退出码恒为 0，成败需解析 stdout。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CLI = "wechatsync"
DEFAULT_PLATFORMS = "weixin"
DEFAULT_TIMEOUT_MS = 120000
DEFAULT_RUN_TIMEOUT_SECONDS = 600
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
SUCCESS_PATTERN = re.compile(r"(\d+)\s*成功")
FAILURE_PATTERN = re.compile(r"(\d+)\s*失败")
IGNORED_URL_HOSTS = ("wechatsync.com", "github.com", "nodejs.org")


class WechatError(Exception):
    """微信公众号发布失败。"""


def _cli_prefix() -> list[str]:
    cli = os.getenv("WECHATSYNC_CLI", DEFAULT_CLI).strip() or DEFAULT_CLI
    resolved = shutil.which(cli) or cli
    if not shutil.which(cli) and not Path(resolved).exists():
        raise WechatError("未找到 wechatsync，请先执行：npm i -g @wechatsync/cli")
    # 直接调用解析出的 .cmd（Windows 下 CreateProcess 可执行），避免 cmd.exe /c 的引号陷阱
    return [resolved]


def _resolve_platform(platform: str | None) -> str:
    """取单个目标平台，空值一律回落默认（weixin）。"""
    raw = (platform or "").strip() or os.getenv("WECHATSYNC_PLATFORMS", "").strip()
    first = raw.split(",")[0].strip() if raw else ""
    return first or DEFAULT_PLATFORMS


def _resolve_platforms(platforms: str | None) -> str:
    """取目标平台列表，空值一律回落默认（weixin）。"""
    return (platforms or "").strip() or os.getenv("WECHATSYNC_PLATFORMS", "").strip() or DEFAULT_PLATFORMS


def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    token = os.getenv("WECHATSYNC_TOKEN", "").strip()
    if token:
        env["WECHATSYNC_TOKEN"] = token
    env["SYNC_WS_PORT"] = os.getenv("SYNC_WS_PORT", "9527").strip() or "9527"
    return env


def _run(args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _cli_prefix() + args,
            cwd=str(BASE_DIR),
            env=_build_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise WechatError(f"wechatsync 执行超时（{timeout_seconds}s）") from exc
    except OSError as exc:
        raise WechatError(f"启动 wechatsync 失败：{exc}") from exc


def _strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text or "")


def _extract_url(text: str) -> str:
    for url in URL_PATTERN.findall(text):
        if not any(host in url for host in IGNORED_URL_HOSTS):
            return url.rstrip("。,.，")
    return ""


def is_logged_in(text: str) -> bool:
    """根据 wechatsync auth 输出判断是否已登录。"""
    clean = _strip_ansi(text)
    return "已登录" in clean and "未登录" not in clean


def check_cli() -> str:
    """检查 wechatsync 是否可用，返回版本信息。"""
    result = _run(["--version"], timeout_seconds=60)
    output = _strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0 and not output:
        raise WechatError("wechatsync 不可用，请确认已安装 @wechatsync/cli")
    return output or "wechatsync 已安装"


def check_login(platform: str | None = None) -> str:
    """检查目标平台登录状态，返回 CLI 原始文本。"""
    target = _resolve_platform(platform)
    result = _run(["auth", target], timeout_seconds=120)
    output = _strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
    if "invalid or missing token" in output.lower():
        raise WechatError(
            "Wechatsync Token 无效或缺失：请在浏览器扩展设置复制 Token 后填入「Wechatsync Token」并保存"
        )
    if result.returncode != 0:
        raise WechatError(f"检查公众号登录状态失败（退出码 {result.returncode}）：{output[-300:]}")
    return output


def publish_file(
    md_path: Path,
    title: str,
    cover: str | Path | None = None,
    platforms: str | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict:
    """把 Markdown 文件推送到公众号草稿，返回 {success, url, output}。"""
    logger = log or (lambda _msg: None)
    md_path = Path(md_path)
    if not md_path.exists():
        raise WechatError(f"Markdown 文件不存在：{md_path}")
    platforms = _resolve_platforms(platforms)
    connection_timeout = int(os.getenv("WECHATSYNC_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS)
    args = [
        "--timeout",
        str(connection_timeout),
        "sync",
        str(md_path),
        "-p",
        platforms,
        "-t",
        title,
    ]
    if cover:
        args += ["--cover", str(cover)]
    if dry_run:
        args.append("--dry-run")

    run_timeout = int(os.getenv("WECHATSYNC_RUN_TIMEOUT_SEC") or DEFAULT_RUN_TIMEOUT_SECONDS)
    logger(f"调用 wechatsync 同步到 {platforms} ...")
    result = _run(args, timeout_seconds=run_timeout)
    output = _strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise WechatError(f"wechatsync 同步失败（退出码 {result.returncode}）：{output[-500:]}")
    if dry_run:
        return {"success": True, "url": "", "output": output}

    failure = FAILURE_PATTERN.search(output)
    success = SUCCESS_PATTERN.search(output)
    if failure and int(failure.group(1)) > 0:
        raise WechatError(f"wechatsync 同步存在失败平台：{output[-500:]}")
    if not success:
        raise WechatError(f"wechatsync 未返回同步结果，请检查扩展连接与登录态：{output[-500:]}")
    url = _extract_url(output)
    logger("wechatsync 同步完成" + (f"：{url}" if url else ""))
    return {"success": True, "url": url, "output": output}


def _selftest() -> None:
    assert is_logged_in("✓ weixin 已登录（我的公众号）")
    assert not is_logged_in("✗ weixin 未登录")
    assert not is_logged_in("")
    assert _extract_url("  同步完成: 1 成功, 0 失败\n  https://mp.weixin.qq.com/s/abc") == (
        "https://mp.weixin.qq.com/s/abc"
    )
    assert _extract_url("见 https://github.com/wechatsync/Wechatsync") == ""
    out = _strip_ansi("\x1b[32m同步完成: 2 成功, 1 失败\x1b[0m")
    assert SUCCESS_PATTERN.search(out).group(1) == "2"
    assert FAILURE_PATTERN.search(out).group(1) == "1"

    saved = os.environ.pop("WECHATSYNC_PLATFORMS", None)
    try:
        assert _resolve_platform("") == "weixin"
        assert _resolve_platform(None) == "weixin"
        assert _resolve_platform("weixin,zhihu") == "weixin"
        assert _resolve_platforms("") == "weixin"
        assert _resolve_platforms("zhihu,weixin") == "zhihu,weixin"
    finally:
        if saved is not None:
            os.environ["WECHATSYNC_PLATFORMS"] = saved
    print("wechat.py 自检通过")


if __name__ == "__main__":
    _selftest()
