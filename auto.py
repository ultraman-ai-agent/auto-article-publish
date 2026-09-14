"""全自动发布引擎：定时选题、生成、配图并发布到小红书。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv, dotenv_values

import generate
import history
import publish
import xhs

BASE_DIR = Path(__file__).resolve().parent
ARTICLES_DIR = BASE_DIR / "articles"
ENV_FILE = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"
STATE_FILE = LOGS_DIR / "auto_state.json"
TOPICS_FILE = BASE_DIR / "topics.txt"
MCP_EXE = BASE_DIR / "xiaohongshu-mcp-windows-amd64.exe"
DEFAULT_CHECK_INTERVAL = 60
SERVICE_WAIT_SECONDS = 120
TRUTHY = ("1", "true", "yes", "on")


class AutoError(Exception):
    """自动发布流程异常。"""


def _write_log_file(line: str) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / f"auto_{datetime.now():%Y%m%d}.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def make_logger(callback: Callable[[str], None] | None) -> Callable[[str], None]:
    """构造统一日志函数：加时间戳，输出到回调/控制台并写入日志文件。"""

    def logger(message: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {message}"
        if callback:
            callback(line)
        else:
            print(line)
        _write_log_file(line)

    return logger


def parse_times(raw: str) -> list[str]:
    """解析逗号分隔的 HH:MM 时段，返回升序合法列表。"""
    result: set[str] = set()
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        try:
            parsed = datetime.strptime(text, "%H:%M")
        except ValueError:
            continue
        result.add(parsed.strftime("%H:%M"))
    return sorted(result)


def next_run_time(times: list[str], fired: list[str], now: datetime | None = None) -> str | None:
    """返回下一次触发时间（HH:MM），今天没有则返回明天的首个时段。"""
    if not times:
        return None
    now = now or datetime.now()
    current = now.strftime("%H:%M")
    for slot in times:
        if slot not in fired and slot > current:
            return slot
    return times[0]


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _today_state() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    state = load_state()
    if state.get("date") != today:
        return {"date": today, "fired": []}
    state.setdefault("fired", [])
    return state


def articles_today_count() -> int:
    """统计今日“发布成功”条数（自动与手动都计入）。"""
    if not publish.PUBLISH_HISTORY_FILE.exists():
        return 0
    today = datetime.now().date().isoformat()
    count = 0
    try:
        with publish.PUBLISH_HISTORY_FILE.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if str(record.get("time", ""))[:10] == today:
                    count += 1
    except OSError:
        return 0
    return count


def cleanup_empty_archives(min_age_seconds: int = 600) -> None:
    """清理 articles 下无 article.json 且超过 min_age_seconds 的空目录。"""
    if not ARTICLES_DIR.exists():
        return
    cutoff = time.time() - min_age_seconds
    for path in ARTICLES_DIR.iterdir():
        if not path.is_dir():
            continue
        if (path / "article.json").exists():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _parse_host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.hostname or "localhost", parsed.port or 18060


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def ensure_service(log: Callable[[str], None]) -> None:
    """确保 MCP 服务在线，必要时拉起 exe。"""
    url = os.getenv("XHS_MCP_URL", "http://localhost:18060/mcp")
    host, port = _parse_host_port(url)
    if _port_open(host, port):
        return
    if not MCP_EXE.exists():
        raise AutoError(f"未找到 {MCP_EXE.name}，无法启动 MCP 服务")
    log("MCP 服务未启动，正在拉起...")
    command = [str(MCP_EXE)]
    if os.getenv("MCP_HEADLESS", "true").strip().lower() == "false":
        command.append("-headless=false")
        log("已启用非无头模式（MCP_HEADLESS=false）")
    subprocess.Popen(command, cwd=str(BASE_DIR), creationflags=subprocess.CREATE_NEW_CONSOLE)
    for _ in range(SERVICE_WAIT_SECONDS):
        if _port_open(host, port):
            log("MCP 服务已就绪")
            return
        threading.Event().wait(1)
    raise AutoError("MCP 服务启动超时")


def ensure_login(log: Callable[[str], None]) -> None:
    """检查登录态，未登录则终止本次自动发布。"""
    status = xhs.check_login()
    if not xhs.is_logged_in(status):
        raise AutoError(f"小红书未登录，请先在界面扫码登录。状态：{status}")


def read_pool() -> list[str]:
    if not TOPICS_FILE.exists():
        return []
    return [line.strip() for line in TOPICS_FILE.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def remove_pool_topic(topic: str) -> None:
    lines = read_pool()
    if topic in lines:
        lines.remove(topic)
        TOPICS_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def pick_topic(log: Callable[[str], None]) -> tuple[str, str | None]:
    """决定本期选题，返回 (选题, 选题池原文或 None)。"""
    source = os.getenv("AUTO_TOPIC_SOURCE", "pool_first").strip().lower()
    pool = read_pool()
    if pool and source in ("pool_first", "pool_only"):
        log(f"从选题池取用：{pool[0]}")
        return pool[0], pool[0]
    if source == "pool_only":
        raise AutoError("选题池为空，且配置为仅用选题池")

    domain = os.getenv("AUTO_DOMAIN", "30岁职场人的真实职场见闻")
    count = int(os.getenv("AUTO_AI_TOPIC_COUNT", "5"))
    days = int(os.getenv("DEDUP_DAYS", "7"))
    recent = history.recent_prompt_block(days)
    try:
        reference = "\n".join(f"- {t}" for t in xhs.search_feeds(domain, 5)) or "（无）"
    except xhs.XhsError:
        reference = "（无）"
    log("选题池为空，AI 正在生成选题...")
    topics = generate.suggest_topics(domain, count=count, recent_block=recent, reference_block=reference)
    return topics[0], None


def run_once(
    visibility: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    source: str = "auto",
    log: Callable[[str], None] | None = None,
) -> publish.PublishResult | None:
    """执行一次完整自动发布，返回结果（因上限/未登录跳过时返回 None）。"""
    logger = make_logger(log)
    limit = int(os.getenv("AUTO_DAILY_LIMIT", "2"))
    used = articles_today_count()
    if not dry_run and not force and used >= limit:
        logger(f"今日已发布 {used}/上限 {limit} 条，跳过")
        return None

    ensure_service(logger)
    ensure_login(logger)

    topic, pool_line = pick_topic(logger)
    target_visibility = visibility or os.getenv("AUTO_VISIBILITY", "公开可见")
    if force:
        logger(f"强制执行（忽略每日上限 {limit}），今日已发布 {used} 条")
    result = publish.run(
        topic=topic,
        visibility=target_visibility,
        dry_run=dry_run,
        log=logger,
        source=source,
    )
    if pool_line and not dry_run:
        remove_pool_topic(pool_line)
    logger(f"自动发布完成：{result.article.title}（今日已发布 {articles_today_count()}/{limit} 条）")
    return result


def reload_config() -> None:
    """从 .env 重新加载配置，使界面“保存设置”后常驻立即生效。"""
    if not ENV_FILE.exists():
        return
    for key, value in dotenv_values(ENV_FILE).items():
        if value is not None:
            os.environ[key] = value


def loop(stop_event: threading.Event, log: Callable[[str], None] | None = None) -> None:
    """常驻定时循环，到点触发一次发布（启动时不补发已过时段）。"""
    logger = make_logger(log)
    reload_config()
    times = parse_times(os.getenv("AUTO_TIMES", "09:30,15:00"))
    if not times:
        logger("未配置有效的 AUTO_TIMES，自动循环退出")
        return

    cleanup_empty_archives()
    state = _today_state()
    now = datetime.now().strftime("%H:%M")
    skipped = [slot for slot in times if slot not in state["fired"] and now >= slot]
    for slot in skipped:
        state["fired"].append(slot)
    save_state(state)
    if skipped:
        logger(f"已过时段不补发：{', '.join(skipped)}")
    logger(f"自动发布常驻启动，时段：{', '.join(times)}")

    while not stop_event.is_set():
        reload_config()
        times = parse_times(os.getenv("AUTO_TIMES", "09:30,15:00"))
        interval = int(os.getenv("AUTO_CHECK_INTERVAL_SEC", str(DEFAULT_CHECK_INTERVAL)))
        state = _today_state()
        now = datetime.now().strftime("%H:%M")
        for slot in times:
            if slot in state["fired"] or now < slot:
                continue
            try:
                run_once(log=log, source="auto")
            except (AutoError, generate.GenerateError, xhs.XhsError, publish.PublishError) as exc:
                logger(f"自动发布失败：{exc}")
            except Exception as exc:  # noqa: BLE001 保证循环不中断
                logger(f"自动发布异常：{exc}")
            state["fired"].append(slot)
            save_state(state)
        stop_event.wait(interval)
    logger("自动发布常驻已停止")


def _selftest() -> None:
    assert parse_times("09:30, 15:00,abc,23:59") == ["09:30", "15:00", "23:59"]
    now = datetime(2026, 9, 11, 10, 0)
    assert next_run_time(["09:30", "15:00"], ["09:30"], now) == "15:00"
    assert next_run_time(["09:30", "15:00"], ["09:30", "15:00"], now) == "09:30"
    assert next_run_time([], [], now) is None

    import tempfile

    original = publish.PUBLISH_HISTORY_FILE
    try:
        temp_file = Path(tempfile.mkdtemp()) / "publish_history.jsonl"
        publish.PUBLISH_HISTORY_FILE = temp_file
        today = datetime.now().date().isoformat()
        temp_file.write_text(
            json.dumps({"time": f"{today}T10:00:00", "title": "a"}, ensure_ascii=False)
            + "\n"
            + json.dumps({"time": "2000-01-01T00:00:00", "title": "b"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        assert articles_today_count() == 1, articles_today_count()
    finally:
        publish.PUBLISH_HISTORY_FILE = original
    print("auto.py 自检通过")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="小红书全自动发布")
    parser.add_argument("--once", action="store_true", help="立即执行一次")
    parser.add_argument("--loop", action="store_true", help="常驻定时运行")
    parser.add_argument("--force", action="store_true", help="忽略每日上限强制发布一次")
    parser.add_argument("--dry-run", action="store_true", help="只生成不发布")
    parser.add_argument("--visibility", help="覆盖发布可见范围")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args()

    load_dotenv()

    if args.selftest:
        _selftest()
        return 0
    try:
        if args.loop:
            stop = threading.Event()
            loop(stop)
        elif args.once:
            run_once(visibility=args.visibility, dry_run=args.dry_run, force=args.force)
        else:
            parser.error("请指定 --once 或 --loop")
            return 2
    except (AutoError, generate.GenerateError, xhs.XhsError, publish.PublishError) as exc:
        make_logger(None)(f"失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
