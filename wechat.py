"""微信公众号发布：调用 wechatsync CLI，将 Markdown 同步为公众号草稿。

wechatsync 依赖 Chrome 扩展在线（浏览器登录态），默认存草稿；
CLI 同步结果退出码恒为 0，成败需解析 stdout。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import requests

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CLI = "wechatsync"
DEFAULT_PLATFORMS = "weixin"
DEFAULT_TIMEOUT_MS = 40000
DEFAULT_RUN_TIMEOUT_SECONDS = 600
DEFAULT_AUTH_TIMEOUT_SECONDS = 45
DEFAULT_SYNC_WS_PORT = 9527
BRIDGE_HTTP_TIMEOUT = 2
# CLI 在主桥超时后会问「是否打开扩展安装页?」。stdin 关闭时该提问的回调永不触发，
# CLI 会死锁并一直占着 9527/9528；这里预先喂一个 n 让它直接退出。
CLI_STDIN_ANSWER = "n\n"
CLI_JS_RELATIVE = Path("node_modules") / "@wechatsync" / "cli" / "dist" / "index.js"
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
SUCCESS_PATTERN = re.compile(r"(\d+)\s*成功")
FAILURE_PATTERN = re.compile(r"(\d+)\s*失败")
IGNORED_URL_HOSTS = ("wechatsync.com", "github.com", "nodejs.org")
CREATE_NO_WINDOW = 0x08000000
SW_HIDE = 0
TRUTHY = ("1", "true", "yes", "on")


class WechatError(Exception):
    """微信公众号发布失败。"""


def _cli_prefix() -> list[str]:
    cli = os.getenv("WECHATSYNC_CLI", DEFAULT_CLI).strip() or DEFAULT_CLI
    resolved = shutil.which(cli) or cli
    if not shutil.which(cli) and not Path(resolved).exists():
        raise WechatError("未找到 wechatsync，请先执行：npm i -g @wechatsync/cli")
    # 优先直接调用 node + 入口 JS，避免 .cmd/cmd.exe 孙进程占着管道导致超时后收尾卡死
    if os.name == "nt":
        resolved_path = Path(resolved)
        js = resolved_path.parent / CLI_JS_RELATIVE
        node = resolved_path.parent / "node.exe"
        node_exe = str(node) if node.exists() else (shutil.which("node") or "")
        if node_exe and js.exists():
            return [node_exe, str(js)]
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


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in TRUTHY


def _process_flags():
    """返回 (creationflags, startupinfo)：默认隐藏子进程控制台窗口。"""
    if os.name != "nt":
        return 0, None
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if _truthy(os.getenv("WECHATSYNC_SHOW_CONSOLE"), default=False):
        return flags, None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE
    return flags | CREATE_NO_WINDOW, startupinfo


def _cleanup_stale_bridge() -> None:
    """启动前清理残留的 @wechatsync node 进程（它们占着 SYNC_WS_PORT 会让新实例空等）。"""
    if os.name != "nt":
        return
    flags, _startupinfo = _process_flags()
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        "Where-Object { $_.CommandLine -like '*@wechatsync*' } | "
        "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            creationflags=flags,
        )
    except Exception:  # noqa: BLE001 清理失败不阻断主流程
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时后杀掉整棵进程树，避免残留子进程占着管道/端口。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001 兜底再尝试直接 kill
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run(args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess:
    command = _cli_prefix() + args
    creationflags, startupinfo = _process_flags()
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            env=_build_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    except OSError as exc:
        raise WechatError(f"启动 wechatsync 失败：{exc}") from exc
    try:
        stdout, stderr = proc.communicate(input=CLI_STDIN_ANSWER, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        try:
            tail_out, tail_err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 收尾失败也不再阻塞
            _kill_tree(proc)
            tail_out, tail_err = "", ""
        partial = _strip_ansi(
            (tail_out or str(exc.stdout or "")) + (tail_err or str(exc.stderr or ""))
        ).strip()
        detail = f"：{partial[-300:]}" if partial else ""
        raise WechatError(
            f"wechatsync 执行超时（{timeout_seconds}s），已终止进程{detail}"
        ) from exc
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _bridge_ws_port() -> int:
    return int(os.getenv("SYNC_WS_PORT") or DEFAULT_SYNC_WS_PORT)


def _connected_from_status(text: str) -> bool | None:
    """解析桥接 /status 返回体：True/False 为扩展连接状态，无法解析返回 None。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "connected" not in data:
        return None
    return bool(data.get("connected"))


def bridge_status() -> bool | None:
    """探测本地同步桥接：True 扩展已连接 / False 有实例但扩展未连 / None 无实例或探测失败。"""
    ws_port = _bridge_ws_port()
    session = requests.Session()
    session.trust_env = False  # 本地回环不经过任何系统代理
    try:
        resp = session.get(f"http://127.0.0.1:{ws_port + 1}/status", timeout=BRIDGE_HTTP_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return _connected_from_status(resp.text)


def _ensure_bridge_connected() -> None:
    """已有桥接实例但扩展未连接时立即报错，避免白等 CLI 超时。"""
    if bridge_status() is not False:
        return
    _cleanup_stale_bridge()  # 顺手清掉占着端口的旧实例，否则后续每次调用都被它拖住
    raise WechatError(
        f"扩展未连接同步桥接（ws://localhost:{_bridge_ws_port()}）："
        "请在 Chrome 打开「文章同步助手」扩展 → 开启「MCP 连接」（或重新加载扩展）后重试；"
        "仍失败可重启 Chrome。"
    )


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
    _ensure_bridge_connected()
    _cleanup_stale_bridge()
    auth_seconds = int(os.getenv("WECHATSYNC_AUTH_TIMEOUT_SEC") or DEFAULT_AUTH_TIMEOUT_SECONDS)
    connect_ms = min(
        int(os.getenv("WECHATSYNC_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS),
        max(5000, (auth_seconds - 5) * 1000),
    )
    result = _run(["--timeout", str(connect_ms), "auth", target], timeout_seconds=auth_seconds)
    output = _strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
    if "invalid or missing token" in output.lower():
        raise WechatError(
            "Wechatsync Token 无效或缺失：请在浏览器扩展设置复制 Token 后填入「Wechatsync Token」并保存"
        )
    if result.returncode != 0:
        detail = output[-300:] or "子进程被中断或控制台窗口被关闭"
        raise WechatError(f"检查公众号登录状态失败（退出码 {result.returncode}）：{detail}")
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
    _ensure_bridge_connected()
    _cleanup_stale_bridge()
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
        detail = output[-500:] or "子进程被中断或控制台窗口被关闭"
        raise WechatError(f"wechatsync 同步失败（退出码 {result.returncode}）：{detail}")
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

    assert _connected_from_status('{"connected":true,"mode":"primary"}') is True
    assert _connected_from_status('{"connected":false,"mode":"primary"}') is False
    assert _connected_from_status('{"mode":"primary"}') is None
    assert _connected_from_status("<html>404</html>") is None
    assert _connected_from_status("") is None

    saved_port = os.environ.pop("SYNC_WS_PORT", None)
    try:
        assert _bridge_ws_port() == 9527
        os.environ["SYNC_WS_PORT"] = "9600"
        assert _bridge_ws_port() == 9600
    finally:
        os.environ.pop("SYNC_WS_PORT", None)
        if saved_port is not None:
            os.environ["SYNC_WS_PORT"] = saved_port
    print("wechat.py 自检通过")


if __name__ == "__main__":
    _selftest()
