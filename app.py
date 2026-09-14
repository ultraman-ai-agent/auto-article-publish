"""可视化界面：CustomTkinter GUI，管理配置、生成内容并发布到小红书。"""

from __future__ import annotations

import base64
import io
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import customtkinter as ctk
from dotenv import load_dotenv, dotenv_values

import auto as auto_module
import generate as generate_module
import images as images_module
import news_pipeline
import publish as publish_module
import sites
import wechat
import xhs
from generate import Article, GenerateError

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
MCP_EXE = BASE_DIR / "xiaohongshu-mcp-windows-amd64.exe"
LOGIN_EXE = BASE_DIR / "xiaohongshu-login-windows-amd64.exe"
ARTICLES_DIR = BASE_DIR / "articles"
PROMPT_PATH = BASE_DIR / "prompts" / "note_prompt.txt"
TOPICS_PATH = BASE_DIR / "topics.txt"

GEN_BACKENDS = ["默认", "openai", "anthropic"]
VISIBILITIES = ["公开可见", "仅自己可见", "仅互关好友可见"]
COUNT_DEFAULT_LABEL = "默认范围"
PORT_READY_TIMEOUT = 120
POLL_INTERVAL_MS = 100
STATUS_INTERVAL_MS = 5000

APPEARANCE_OPTIONS = {"深色": "dark", "浅色": "light", "跟随系统": "system"}
APPEARANCE_LABELS = {value: label for label, value in APPEARANCE_OPTIONS.items()}
PLATFORM_LABELS = {"xhs": "小红书", "wechat": "公众号"}
PLATFORM_TITLES = {"小红书": "小红书自动发布", "公众号": "公众号自动发布"}
TOAST_COLORS = {
    "info": "#3b82f6",
    "success": "#22c55e",
    "error": "#ef4444",
    "warn": "#f59e0b",
}
STATUS_COLORS = {
    "offline": "#9ca3af",
    "online": "#22c55e",
    "login": "#f59e0b",
}
PUBLISH_STEPS = ["生成文案", "配图", "留档", "发布"]
STEP_COLORS = {
    "pending": "#9ca3af",
    "active": "#f59e0b",
    "done": "#22c55e",
    "error": "#ef4444",
}

CONFIG_FIELDS = [
    ("GEN_BACKEND", "生成后端", False, ["openai", "anthropic"]),
    ("OPENAI_BASE_URL", "OpenAI 地址", False, None),
    ("OPENAI_API_KEY", "OpenAI 密钥", True, None),
    ("OPENAI_MODEL", "OpenAI 模型", False, None),
    ("ANTHROPIC_BASE_URL", "Anthropic 地址", False, None),
    ("ANTHROPIC_API_KEY", "Anthropic 密钥", True, None),
    ("ANTHROPIC_MODEL", "Anthropic 模型", False, None),
    ("PEXELS_API_KEY", "Pexels 密钥", True, None),
    ("UNSPLASH_ACCESS_KEY", "Unsplash 密钥", True, None),
    ("IMAGE_SOURCE", "图片来源", False, ["auto", "autohome", "pexels", "unsplash", "autohome,pexels"]),
    ("CAR_IMAGE_CATEGORIES", "汽车图分类", False, None),
    ("XHS_MCP_URL", "MCP 服务地址", False, None),
    ("MCP_HEADLESS", "静默启动浏览器", False, ["true", "false"]),
    ("XHS_VISIBILITY", "默认可见范围", False, VISIBILITIES),
    ("IMAGE_COUNT_MIN", "配图数量下限", False, None),
    ("IMAGE_COUNT_MAX", "配图数量上限", False, None),
    ("CONTENT_TARGET_MAX", "正文目标字数", False, None),
    ("TAGS_MAX", "标签数量上限", False, None),
    ("DEDUP_DAYS", "去重天数", False, None),
    ("DEDUP_THRESHOLD", "相似度阈值(0-1)", False, None),
    ("DEDUP_MAX_RETRY", "去重最大重试", False, None),
    ("XHS_REFERENCE_ENABLED", "实时热点参考", False, ["true", "false"]),
    ("XHS_REFERENCE_COUNT", "参考条数", False, None),
    ("AUTO_DAILY_LIMIT", "每日发布上限", False, None),
]

AUTO_TOPIC_SOURCES = ["pool_first", "ai", "pool_only"]
AUTO_FIELDS = [
    ("AUTO_TIMES", "发布时段", None),
    ("AUTO_VISIBILITY", "自动发布范围", VISIBILITIES),
    ("AUTO_TOPIC_SOURCE", "选题来源", AUTO_TOPIC_SOURCES),
    ("AUTO_DOMAIN", "领域关键词", None),
    ("AUTO_AI_TOPIC_COUNT", "AI 选题数量", None),
    ("AUTO_CHECK_INTERVAL_SEC", "检查间隔(秒)", None),
]

# 公众号（Wechatsync + MiniMax + 新闻抓取）独立配置，不参与小红书 CONFIG_FIELDS
WECHAT_FIELDS = [
    ("WECHATSYNC_CLI", "wechatsync 命令", False, None),
    ("WECHATSYNC_TOKEN", "Wechatsync Token", True, None),
    ("WECHATSYNC_PLATFORMS", "目标平台", False, None),
    ("MINIMAX_API_KEY", "MiniMax 密钥", True, None),
    ("MINIMAX_BASE_URL", "MiniMax 地址", False, None),
    ("MINIMAX_MODEL", "MiniMax 模型", False, None),
    ("NEWS_SITE", "新闻站点", False, None),
    ("NEWS_PICK_MIN", "抓取数量下限", False, None),
    ("NEWS_PICK_MAX", "抓取数量上限", False, None),
    ("NEWS_AI_FIRST", "AI 优先", False, ["true", "false"]),
    ("NEWS_MAX_CANDIDATES", "候选上限", False, None),
]


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    return {key: (value or "") for key, value in dotenv_values(ENV_PATH).items()}


def write_env(updates: dict[str, str]) -> None:
    """按 key 更新 .env，保留注释与未知项，缺失的追加到末尾。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    return parsed.hostname or "localhost", parsed.port or 18060


def _clip_text(text: str, limit: int = 40) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        load_dotenv(ENV_PATH, override=True)
        env = read_env()
        appearance = env.get("UI_APPEARANCE", "dark") or "dark"
        ctk.set_default_color_theme("blue")
        ctk.set_appearance_mode(appearance)

        self.title("小红书自动发布")
        self.geometry("940x820")
        self.minsize(840, 720)

        self._queue: queue.Queue = queue.Queue()
        self.current_article: Article | None = None
        self.current_out_dir: Path | None = None
        self._thumb_refs: list = []
        self._toast_label: ctk.CTkLabel | None = None
        self._qr_dialog: ctk.CTkToplevel | None = None
        self._qr_photo: ctk.CTkImage | None = None
        self._login_checked = False
        self._auto_thread: threading.Thread | None = None
        self._auto_stop = threading.Event()
        self._auto_elapsed = 0
        self._busy_count = 0
        self._btn_anim: dict = {}
        self._pulse_jobs: dict = {}
        self._service_waiting = False
        self._service_wait_start = 0.0
        self._active_step = -1
        self._wechat_items: list = []

        self.config_vars = {
            key: ctk.StringVar(value=env.get(key, "")) for key, _, _, _ in CONFIG_FIELDS
        }
        self.auto_vars = {
            key: ctk.StringVar(value=env.get(key, "")) for key, _, _ in AUTO_FIELDS
        }
        self.wechat_vars = {
            key: ctk.StringVar(value=env.get(key, "")) for key, _, _, _ in WECHAT_FIELDS
        }
        self.wechat_vars["WECHATSYNC_CLI"].set(env.get("WECHATSYNC_CLI", "") or "wechatsync")
        self.wechat_vars["WECHATSYNC_PLATFORMS"].set(
            env.get("WECHATSYNC_PLATFORMS", "") or "weixin"
        )
        self.wechat_vars["MINIMAX_BASE_URL"].set(
            env.get("MINIMAX_BASE_URL", "") or "https://api.minimaxi.com/v1"
        )
        self.wechat_vars["NEWS_PICK_MIN"].set(env.get("NEWS_PICK_MIN", "") or "3")
        self.wechat_vars["NEWS_PICK_MAX"].set(env.get("NEWS_PICK_MAX", "") or "5")
        self.wechat_vars["NEWS_MAX_CANDIDATES"].set(env.get("NEWS_MAX_CANDIDATES", "") or "60")
        self.wechat_vars["NEWS_AI_FIRST"].set(env.get("NEWS_AI_FIRST", "") or "true")
        self._build_header()
        self._build_tabs()

        self.after(POLL_INTERVAL_MS, self._poll_queue)
        self.after(400, self._refresh_service_status)
        self.after(STATUS_INTERVAL_MS, self._periodic_status)
        self.after(STATUS_INTERVAL_MS, self._refresh_auto_status)

    # ---------- 顶栏 ----------

    def _build_header(self) -> None:
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.pack(fill="x", padx=18, pady=(14, 4))
        self.header_title = ctk.CTkLabel(
            self._header, text="小红书自动发布", font=ctk.CTkFont(size=20, weight="bold")
        )
        self.header_title.pack(side="left")
        self.platform_switch = ctk.CTkSegmentedButton(
            self._header, values=list(PLATFORM_LABELS.values()), command=self._on_platform_change
        )
        self.platform_switch.set(PLATFORM_LABELS["xhs"])
        self.platform_switch.pack(side="left", padx=(16, 0))
        current = APPEARANCE_LABELS.get(ctk.get_appearance_mode().lower(), "深色")
        self.theme_switch = ctk.CTkSegmentedButton(
            self._header, values=list(APPEARANCE_OPTIONS.keys()), command=self._on_theme_change
        )
        self.theme_switch.set(current)
        self.theme_switch.pack(side="right")
        self.busy_bar = ctk.CTkProgressBar(self, height=3, mode="indeterminate")

    # ---------- 忙碌 / 动画 ----------

    def _show_busy(self) -> None:
        if not self.busy_bar.winfo_ismapped():
            self.busy_bar.pack(fill="x", padx=18, pady=(0, 4), after=self._header)
        self.busy_bar.start()

    def _hide_busy(self) -> None:
        self.busy_bar.stop()
        self.busy_bar.pack_forget()

    def _push_busy(self) -> None:
        self._busy_count += 1
        if self._busy_count == 1:
            self._show_busy()

    def _pop_busy(self) -> None:
        if self._busy_count > 0:
            self._busy_count -= 1
        if self._busy_count == 0:
            self._hide_busy()

    def _start_btn_anim(self, button: ctk.CTkButton, original: str) -> None:
        frames = ["处理中", "处理中·", "处理中··", "处理中···"]
        index = {"n": 0}

        def tick() -> None:
            if button not in self._btn_anim:
                return
            try:
                button.configure(text=frames[index["n"] % len(frames)])
            except Exception:  # noqa: BLE001 控件可能已销毁
                return
            index["n"] += 1
            self._btn_anim[button] = (original, self.after(400, tick))

        self._btn_anim[button] = (original, None)
        tick()

    def _stop_btn_anim(self, button: ctk.CTkButton) -> None:
        entry = self._btn_anim.pop(button, None)
        if not entry:
            return
        original, job = entry
        if job:
            try:
                self.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
        try:
            button.configure(text=original)
        except Exception:  # noqa: BLE001
            pass

    def _start_pulse(self, key: str, dot: ctk.CTkLabel, active: str, base: str) -> None:
        self._stop_pulse(key, dot, base)
        state = {"on": False}

        def tick() -> None:
            if key not in self._pulse_jobs:
                return
            state["on"] = not state["on"]
            try:
                dot.configure(text_color=active if state["on"] else base)
            except Exception:  # noqa: BLE001
                return
            self._pulse_jobs[key] = self.after(600, tick)

        tick()

    def _stop_pulse(self, key: str, dot: ctk.CTkLabel | None = None, base: str | None = None) -> None:
        job = self._pulse_jobs.pop(key, None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
        if dot is not None and base is not None:
            try:
                dot.configure(text_color=base)
            except Exception:  # noqa: BLE001
                pass

    def _on_theme_change(self, label: str) -> None:
        ctk.set_appearance_mode(APPEARANCE_OPTIONS[label])
        write_env({"UI_APPEARANCE": APPEARANCE_OPTIONS[label]})

    # ---------- 页签 ----------

    def _build_tabs(self) -> None:
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(fill="both", expand=True)

        self.xhs_tabview = ctk.CTkTabview(self.content)
        self.xhs_tabview.add("发布")
        self.xhs_tabview.add("自动")
        self.xhs_tabview.add("配置")
        self._build_publish_tab(self.xhs_tabview.tab("发布"))
        self._build_auto_tab(self.xhs_tabview.tab("自动"))
        self._build_config_tab(self.xhs_tabview.tab("配置"))

        self.wechat_panel = ctk.CTkFrame(self.content, fg_color="transparent")
        self._build_wechat_tab(self.wechat_panel)

        self.xhs_tabview.pack(fill="both", expand=True, padx=16, pady=8)

    def _on_platform_change(self, label: str) -> None:
        """顶层平台切换：小红书（发布/自动/配置）与公众号面板互斥显示。"""
        title = PLATFORM_TITLES.get(label, "自动发布")
        self.header_title.configure(text=title)
        self.title(title)
        if label == PLATFORM_LABELS["wechat"]:
            self.xhs_tabview.pack_forget()
            self.wechat_panel.pack(fill="both", expand=True, padx=16, pady=8)
        else:
            self.wechat_panel.pack_forget()
            self.xhs_tabview.pack(fill="both", expand=True, padx=16, pady=8)

    def _card(self, parent, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", pady=6)
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=14, pady=(10, 4)
        )
        return card

    # ---------- 发布页签 ----------

    def _build_publish_tab(self, parent) -> None:
        parent = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        parent.pack(fill="both", expand=True)
        service = self._card(parent, "服务状态")
        row = ctk.CTkFrame(service, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 12))
        self.status_dot = ctk.CTkLabel(row, text="●", font=ctk.CTkFont(size=16), text_color=STATUS_COLORS["offline"])
        self.status_dot.pack(side="left")
        self.service_var = ctk.StringVar(value="未检测")
        ctk.CTkLabel(row, textvariable=self.service_var).pack(side="left", padx=8)
        self.login_dot = ctk.CTkLabel(row, text="●", font=ctk.CTkFont(size=16), text_color=STATUS_COLORS["offline"])
        self.login_dot.pack(side="left", padx=(16, 0))
        self.login_var = ctk.StringVar(value="登录未检测")
        ctk.CTkLabel(row, textvariable=self.login_var, text_color="gray").pack(side="left", padx=6)
        self.login_btn = ctk.CTkButton(row, text="扫码登录", width=90, fg_color="transparent", border_width=1, command=self._login)
        self.login_btn.pack(side="right", padx=4)
        self.check_login_btn = ctk.CTkButton(row, text="检查登录", width=90, fg_color="transparent", border_width=1, command=self._check_login)
        self.check_login_btn.pack(side="right", padx=4)
        self.start_btn = ctk.CTkButton(row, text="启动服务", width=90, command=self._start_service)
        self.start_btn.pack(side="right", padx=4)

        stepper_card = self._card(parent, "流程")
        step_row = ctk.CTkFrame(stepper_card, fg_color="transparent")
        step_row.pack(fill="x", padx=14, pady=(0, 12))
        self.step_labels: list[ctk.CTkLabel] = []
        for index, name in enumerate(PUBLISH_STEPS):
            if index:
                ctk.CTkLabel(step_row, text="→", text_color=STEP_COLORS["pending"]).pack(side="left", padx=6)
            label = ctk.CTkLabel(step_row, text=f"○ {name}", text_color=STEP_COLORS["pending"])
            label.pack(side="left")
            self.step_labels.append(label)

        content = self._card(parent, "内容")
        grid = ctk.CTkFrame(content, fg_color="transparent")
        grid.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(grid, text="主题").grid(row=0, column=0, sticky="w")
        self.topic_entry = ctk.CTkEntry(grid, placeholder_text="例如：春天露营装备推荐")
        self.topic_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=8)
        ctk.CTkLabel(grid, text="生成后端").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.gen_combo = ctk.CTkComboBox(grid, values=GEN_BACKENDS, state="readonly", width=130)
        self.gen_combo.set("默认")
        self.gen_combo.grid(row=1, column=1, sticky="w", padx=8, pady=(10, 0))
        ctk.CTkLabel(grid, text="配图数量").grid(row=1, column=2, sticky="e", pady=(10, 0))
        self.count_menu = ctk.CTkOptionMenu(grid, values=[COUNT_DEFAULT_LABEL] + [str(i) for i in range(1, 10)], width=110)
        self.count_menu.set(COUNT_DEFAULT_LABEL)
        self.count_menu.grid(row=1, column=3, sticky="w", padx=8, pady=(10, 0))
        self.generate_btn = ctk.CTkButton(grid, text="生成并配图", command=self._generate)
        self.generate_btn.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        grid.columnconfigure(1, weight=1)

        preview = self._card(parent, "预览（可编辑）")
        pgrid = ctk.CTkFrame(preview, fg_color="transparent")
        pgrid.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(pgrid, text="标题").grid(row=0, column=0, sticky="w")
        self.title_entry = ctk.CTkEntry(pgrid)
        self.title_entry.grid(row=0, column=1, sticky="ew", padx=8)
        ctk.CTkLabel(pgrid, text="标签").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.tags_entry = ctk.CTkEntry(pgrid, placeholder_text="多个标签用逗号分隔")
        self.tags_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        ctk.CTkLabel(pgrid, text="正文").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        self.content_text = ctk.CTkTextbox(pgrid, height=180, wrap="word")
        self.content_text.grid(row=2, column=1, sticky="nsew", padx=8, pady=(8, 0))
        self.thumbs_frame = ctk.CTkFrame(pgrid, fg_color="transparent")
        self.thumbs_frame.grid(row=3, column=1, sticky="w", padx=8, pady=(10, 0))
        pgrid.columnconfigure(1, weight=1)

        options = self._card(parent, "发布选项")
        orow = ctk.CTkFrame(options, fg_color="transparent")
        orow.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(orow, text="可见范围").pack(side="left")
        self.visibility_combo = ctk.CTkComboBox(orow, values=VISIBILITIES, state="readonly", width=140)
        self.visibility_combo.set(self.config_vars["XHS_VISIBILITY"].get() or VISIBILITIES[0])
        self.visibility_combo.pack(side="left", padx=8)
        self.original_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(orow, text="声明原创", variable=self.original_var).pack(side="left", padx=12)
        self.publish_btn = ctk.CTkButton(orow, text="发布", width=120, command=self._publish)
        self.publish_btn.pack(side="right")

        log_card = self._card(parent, "日志")
        self.log_text = ctk.CTkTextbox(log_card, height=120, wrap="word")
        self.log_text.pack(fill="x", padx=14, pady=(0, 12))
        self.log_text.configure(state="disabled")

    # ---------- 自动页签 ----------

    def _build_auto_tab(self, parent) -> None:
        parent = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        parent.pack(fill="both", expand=True)

        status_card = self._card(parent, "运行状态")
        srow = ctk.CTkFrame(status_card, fg_color="transparent")
        srow.pack(fill="x", padx=14, pady=(0, 12))
        self.auto_dot = ctk.CTkLabel(srow, text="●", font=ctk.CTkFont(size=16), text_color=STATUS_COLORS["offline"])
        self.auto_dot.pack(side="left")
        self.auto_status_var = ctk.StringVar(value="未运行")
        ctk.CTkLabel(srow, textvariable=self.auto_status_var).pack(side="left", padx=8)
        self.auto_used_var = ctk.StringVar(value="今日已发布 0/0 条")
        ctk.CTkLabel(srow, textvariable=self.auto_used_var, text_color="gray").pack(side="left", padx=(10, 0))
        self.auto_stop_btn = ctk.CTkButton(srow, text="停止", width=90, fg_color="transparent", border_width=1, command=self._stop_auto)
        self.auto_stop_btn.pack(side="right", padx=4)
        self.auto_start_btn = ctk.CTkButton(srow, text="启动常驻", width=100, command=self._start_auto)
        self.auto_start_btn.pack(side="right", padx=4)
        self.auto_force_btn = ctk.CTkButton(srow, text="强制执行一次", width=110, fg_color="transparent", border_width=1, command=self._auto_force)
        self.auto_force_btn.pack(side="right", padx=4)
        self.auto_once_btn = ctk.CTkButton(srow, text="立即执行一次", width=110, fg_color="transparent", border_width=1, command=self._auto_once)
        self.auto_once_btn.pack(side="right", padx=4)

        settings = self._card(parent, "自动设置")
        grid = ctk.CTkFrame(settings, fg_color="transparent")
        grid.pack(fill="x", padx=14, pady=(0, 12))
        for row, (key, label, choices) in enumerate(AUTO_FIELDS):
            ctk.CTkLabel(grid, text=label).grid(row=row, column=0, sticky="w", pady=5)
            if choices:
                widget = ctk.CTkComboBox(grid, values=choices, state="readonly", variable=self.auto_vars[key])
            else:
                widget = ctk.CTkEntry(grid, textvariable=self.auto_vars[key])
            widget.grid(row=row, column=1, sticky="ew", padx=8, pady=5)
        grid.columnconfigure(1, weight=1)
        ctk.CTkLabel(
            grid, text="时段格式：HH:MM，多个用英文逗号，如 09:30,15:00；选题来源：pool_first 池优先 / ai 纯AI / pool_only 仅池",
            text_color="gray", wraplength=520, justify="left",
        ).grid(row=len(AUTO_FIELDS), column=0, columnspan=2, sticky="w", pady=(6, 0))
        ctk.CTkButton(settings, text="保存自动设置", command=self._save_config).pack(anchor="e", padx=14, pady=(0, 12))

        pool = self._card(parent, "选题池（每行一个，发布成功后自动移除）")
        self.topics_text = ctk.CTkTextbox(pool, height=140, wrap="word")
        self.topics_text.pack(fill="x", padx=14, pady=(0, 8))
        self.topics_text.insert("1.0", self._read_topics())
        ctk.CTkButton(pool, text="保存选题池", command=self._save_topics).pack(anchor="e", padx=14, pady=(0, 12))

        log_card = self._card(parent, "自动日志")
        self.auto_log_text = ctk.CTkTextbox(log_card, height=160, wrap="word")
        self.auto_log_text.pack(fill="x", padx=14, pady=(0, 12))
        self.auto_log_text.configure(state="disabled")

    def _read_topics(self) -> str:
        if TOPICS_PATH.exists():
            return TOPICS_PATH.read_text(encoding="utf-8-sig")
        return ""

    def _save_topics(self) -> None:
        TOPICS_PATH.write_text(self.topics_text.get("1.0", "end").strip() + "\n", encoding="utf-8")
        self._toast("选题池已保存", "success")

    def _auto_log(self, message: str) -> None:
        self._queue.put(lambda m=message: self._append_auto_log(m))

    def _append_auto_log(self, message: str) -> None:
        self.auto_log_text.configure(state="normal")
        self.auto_log_text.insert("end", message + "\n")
        self.auto_log_text.see("end")
        self.auto_log_text.configure(state="disabled")

    def _auto_once(self) -> None:
        self._apply_form_to_environ()
        self._run_task(
            self.auto_once_btn,
            work=lambda: auto_module.run_once(log=self._auto_log, source="auto"),
            on_success=lambda result: self._toast("执行完成" if result else "已跳过（未登录或已达上限）", "success"),
            on_error=lambda exc: (self._auto_log(f"执行失败：{exc}"), self._toast(str(exc), "error")),
        )

    def _auto_force(self) -> None:
        self._apply_form_to_environ()
        self._run_task(
            self.auto_force_btn,
            work=lambda: auto_module.run_once(log=self._auto_log, force=True, source="auto"),
            on_success=lambda result: self._toast("已强制执行发布" if result else "已跳过（未登录等）", "success"),
            on_error=lambda exc: (self._auto_log(f"执行失败：{exc}"), self._toast(str(exc), "error")),
        )

    def _start_auto(self) -> None:
        if self._auto_thread and self._auto_thread.is_alive():
            self._toast("自动常驻已在运行", "info")
            return
        self._apply_form_to_environ()
        self._auto_stop = threading.Event()
        self._auto_thread = threading.Thread(
            target=auto_module.loop, args=(self._auto_stop, self._auto_log), daemon=True
        )
        self._auto_thread.start()
        self._toast("自动常驻已启动", "success")
        self._refresh_auto_status()

    def _stop_auto(self) -> None:
        self._auto_stop.set()
        self._toast("正在停止自动常驻...", "info")
        self._refresh_auto_status()

    def _refresh_auto_status(self) -> None:
        running = bool(self._auto_thread and self._auto_thread.is_alive())
        limit = self.config_vars["AUTO_DAILY_LIMIT"].get().strip() or "?"
        used = auto_module.articles_today_count()
        self.auto_used_var.set(f"今日已发布 {used}/{limit} 条")
        if running:
            state = auto_module._today_state()
            upcoming = auto_module.next_run_time(
                auto_module.parse_times(self.auto_vars["AUTO_TIMES"].get()), state.get("fired", [])
            )
            self.auto_status_var.set(f"运行中，下次触发：{upcoming or '—'}")
            if "auto" not in self._pulse_jobs:
                self._start_pulse("auto", self.auto_dot, STATUS_COLORS["online"], "#166534")
        else:
            self._stop_pulse("auto", self.auto_dot, STATUS_COLORS["offline"])
            self.auto_status_var.set("未运行")
        self.after(STATUS_INTERVAL_MS, self._refresh_auto_status)

    # ---------- 配置页签 ----------

    def _build_config_tab(self, parent) -> None:
        form = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        form.pack(fill="both", expand=True)
        self.secret_entries: list[ctk.CTkEntry] = []

        for row, (key, label, is_secret, choices) in enumerate(CONFIG_FIELDS):
            ctk.CTkLabel(form, text=label).grid(row=row, column=0, sticky="w", pady=6)
            if choices:
                widget = ctk.CTkComboBox(form, values=choices, state="readonly", variable=self.config_vars[key])
            else:
                widget = ctk.CTkEntry(form, textvariable=self.config_vars[key], show="*" if is_secret else "")
            widget.grid(row=row, column=1, sticky="ew", padx=10, pady=6)
            if is_secret:
                self.secret_entries.append(widget)
        form.columnconfigure(1, weight=1)

        self.show_secret = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            form, text="显示密钥", variable=self.show_secret, command=self._toggle_secrets
        ).grid(row=len(CONFIG_FIELDS), column=0, sticky="w", pady=12)

        buttons = ctk.CTkFrame(form, fg_color="transparent")
        buttons.grid(row=len(CONFIG_FIELDS) + 1, column=0, columnspan=2, sticky="ew", pady=8)
        self.test_gallery_btn = ctk.CTkButton(buttons, text="测试图库", width=100, fg_color="transparent", border_width=1, command=self._test_gallery)
        self.test_gallery_btn.pack(side="left", padx=4)
        self.test_generate_btn = ctk.CTkButton(buttons, text="测试生成", width=100, fg_color="transparent", border_width=1, command=self._test_generate)
        self.test_generate_btn.pack(side="left", padx=4)
        ctk.CTkButton(buttons, text="保存配置", width=120, command=self._save_config).pack(side="right", padx=4)

        ctk.CTkLabel(
            form, text="提示：密钥仅保存在本地 .env，不会提交到 git。", text_color="gray"
        ).grid(row=len(CONFIG_FIELDS) + 2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        prompt_row = len(CONFIG_FIELDS) + 3
        header = ctk.CTkFrame(form, fg_color="transparent")
        header.grid(row=prompt_row, column=0, columnspan=2, sticky="ew", pady=(14, 4))
        ctk.CTkLabel(header, text="提示词", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="恢复默认", width=90, fg_color="transparent", border_width=1, command=self._restore_prompt).pack(side="right", padx=4)
        ctk.CTkButton(header, text="保存提示词", width=100, command=self._save_prompt).pack(side="right", padx=4)
        self.prompt_text = ctk.CTkTextbox(form, height=260, wrap="word")
        self.prompt_text.grid(row=prompt_row + 1, column=0, columnspan=2, sticky="nsew")
        self.prompt_text.insert("1.0", generate_module.load_prompt_template())
        ctk.CTkLabel(
            form, text="可用占位符：{topic} {recent} {reference} {image_count} {title_max} {content_target} {content_max} {tags_min} {tags_max} {dedup_days} {dedup_threshold}",
            text_color="gray",
        ).grid(row=prompt_row + 2, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _save_prompt(self) -> None:
        text = self.prompt_text.get("1.0", "end").strip()
        if not text:
            self._toast("提示词不能为空", "warn")
            return
        PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROMPT_PATH.write_text(text + "\n", encoding="utf-8")
        self._log(f"提示词已保存到 {PROMPT_PATH}")
        self._toast("提示词已保存", "success")

    def _restore_prompt(self) -> None:
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", generate_module.DEFAULT_PROMPT_TEMPLATE)
        self._toast("已恢复默认提示词，记得保存", "info")

    def _toggle_secrets(self) -> None:
        show = "" if self.show_secret.get() else "*"
        for entry in self.secret_entries:
            entry.configure(show=show)

    # ---------- 公众号页签 ----------

    def _build_wechat_tab(self, parent) -> None:
        parent = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        parent.pack(fill="both", expand=True)

        status = self._card(parent, "环境状态")
        srow = ctk.CTkFrame(status, fg_color="transparent")
        srow.pack(fill="x", padx=14, pady=(0, 12))
        self.wechat_dot = ctk.CTkLabel(
            srow, text="●", font=ctk.CTkFont(size=16), text_color=STATUS_COLORS["offline"]
        )
        self.wechat_dot.pack(side="left")
        self.wechat_env_var = ctk.StringVar(value="wechatsync 未检测")
        ctk.CTkLabel(srow, textvariable=self.wechat_env_var).pack(side="left", padx=8)
        self.wechat_login_var = ctk.StringVar(value="登录未检测")
        ctk.CTkLabel(srow, textvariable=self.wechat_login_var, text_color="gray").pack(
            side="left", padx=(12, 0)
        )
        self.wechat_check_btn = ctk.CTkButton(
            srow, text="检测环境", width=90, fg_color="transparent", border_width=1,
            command=self._wechat_check_env,
        )
        self.wechat_check_btn.pack(side="right", padx=4)
        self.wechat_login_btn = ctk.CTkButton(
            srow, text="检查登录", width=90, fg_color="transparent", border_width=1,
            command=self._wechat_check_login,
        )
        self.wechat_login_btn.pack(side="right", padx=4)

        settings = self._card(parent, "公众号与抓取配置")
        grid = ctk.CTkFrame(settings, fg_color="transparent")
        grid.pack(fill="x", padx=14, pady=(0, 8))
        self.wechat_secret_entries = []
        site_keys = list(sites.available_sites().keys())
        for row, (key, label, is_secret, choices) in enumerate(WECHAT_FIELDS):
            ctk.CTkLabel(grid, text=label).grid(row=row, column=0, sticky="w", pady=5)
            if key == "NEWS_SITE":
                widget = ctk.CTkComboBox(
                    grid, values=site_keys or ["yahoo_hk_finance"], state="readonly",
                    variable=self.wechat_vars[key],
                )
                if not self.wechat_vars[key].get():
                    widget.set(site_keys[0] if site_keys else "yahoo_hk_finance")
            elif choices:
                widget = ctk.CTkComboBox(
                    grid, values=choices, state="readonly", variable=self.wechat_vars[key]
                )
            else:
                widget = ctk.CTkEntry(
                    grid, textvariable=self.wechat_vars[key], show="*" if is_secret else ""
                )
            widget.grid(row=row, column=1, sticky="ew", padx=8, pady=5)
            if is_secret:
                self.wechat_secret_entries.append(widget)
        grid.columnconfigure(1, weight=1)
        self.wechat_show_secret = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            grid, text="显示密钥", variable=self.wechat_show_secret,
            command=self._toggle_wechat_secrets,
        ).grid(row=len(WECHAT_FIELDS), column=0, sticky="w", pady=(8, 0))
        ctk.CTkLabel(
            grid,
            text="提示：Wechatsync 需 Chrome 扩展在线并登录公众号；数量范围默认 3~5；保存后写入 .env。",
            text_color="gray", wraplength=520, justify="left",
        ).grid(row=len(WECHAT_FIELDS) + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ctk.CTkButton(settings, text="保存配置", command=self._save_wechat_config).pack(
            anchor="e", padx=14, pady=(0, 12)
        )

        actions = self._card(parent, "操作")
        ctk.CTkLabel(actions, text="候选新闻", text_color="gray", font=ctk.CTkFont(size=12)).pack(
            anchor="w", padx=14
        )
        self.wechat_candidate_combo = ctk.CTkComboBox(
            actions, values=[""], state="readonly", height=32, font=ctk.CTkFont(size=13)
        )
        self.wechat_candidate_combo.set("")
        self.wechat_candidate_combo.pack(fill="x", padx=14, pady=(2, 0))

        sel_row = ctk.CTkFrame(actions, fg_color="transparent")
        sel_row.pack(fill="x", padx=14, pady=(8, 10))
        self.wechat_refresh_btn = ctk.CTkButton(
            sel_row, text="刷新候选", width=100, height=32, corner_radius=8,
            fg_color="transparent", border_width=1, font=ctk.CTkFont(size=13),
            command=self._wechat_refresh_candidates,
        )
        self.wechat_refresh_btn.pack(side="left")
        self.wechat_publish_sel_btn = ctk.CTkButton(
            sel_row, text="发布选中", width=100, height=32, corner_radius=8,
            font=ctk.CTkFont(size=13), command=self._wechat_publish_selected,
        )
        self.wechat_publish_sel_btn.pack(side="left", padx=(8, 0))
        self.wechat_open_btn = ctk.CTkButton(
            sel_row, text="打开原文", width=100, height=32, corner_radius=8,
            fg_color="transparent", border_width=1, font=ctk.CTkFont(size=13),
            command=self._wechat_open_source,
        )
        self.wechat_open_btn.pack(side="left", padx=(8, 0))
        self.wechat_candidate_var = ctk.StringVar(value="候选未加载")
        ctk.CTkLabel(
            sel_row, textvariable=self.wechat_candidate_var, text_color="gray",
            font=ctk.CTkFont(size=12),
        ).pack(side="right")

        ctk.CTkFrame(actions, height=1, fg_color=("gray75", "gray30")).pack(
            fill="x", padx=14, pady=(0, 2)
        )

        ctk.CTkLabel(
            actions, text="批量随机（3~5 篇）", text_color="gray", font=ctk.CTkFont(size=12)
        ).pack(anchor="w", padx=14, pady=(10, 4))
        batch = ctk.CTkFrame(actions, fg_color="transparent")
        batch.pack(fill="x", padx=14, pady=(0, 12))
        for column in range(3):
            batch.columnconfigure(column, weight=1, uniform="batch")
        self.wechat_preview_btn = ctk.CTkButton(
            batch, text="预览候选", height=32, corner_radius=8, font=ctk.CTkFont(size=13),
            fg_color="transparent", border_width=1, command=self._wechat_preview,
        )
        self.wechat_preview_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.wechat_dryrun_btn = ctk.CTkButton(
            batch, text="干跑（不推送）", height=32, corner_radius=8, font=ctk.CTkFont(size=13),
            fg_color="transparent", border_width=1, command=lambda: self._wechat_run(dry_run=True),
        )
        self.wechat_dryrun_btn.grid(row=0, column=1, sticky="ew", padx=6)
        self.wechat_run_btn = ctk.CTkButton(
            batch, text="抓取并发布", height=32, corner_radius=8, font=ctk.CTkFont(size=13),
            command=lambda: self._wechat_run(dry_run=False),
        )
        self.wechat_run_btn.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        log_card = self._card(parent, "公众号日志")
        self.wechat_log_text = ctk.CTkTextbox(log_card, height=200, wrap="word")
        self.wechat_log_text.pack(fill="x", padx=14, pady=(0, 12))
        self.wechat_log_text.configure(state="disabled")

    def _toggle_wechat_secrets(self) -> None:
        show = "" if self.wechat_show_secret.get() else "*"
        for entry in self.wechat_secret_entries:
            entry.configure(show=show)

    def _wechat_log(self, message: str) -> None:
        self._queue.put(lambda m=message: self._append_wechat_log(m))

    def _append_wechat_log(self, message: str) -> None:
        self.wechat_log_text.configure(state="normal")
        self.wechat_log_text.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        self.wechat_log_text.see("end")
        self.wechat_log_text.configure(state="disabled")

    def _wechat_apply_environ(self) -> None:
        for key, _label, _secret, _choices in WECHAT_FIELDS:
            value = self.wechat_vars[key].get().strip()
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        if not os.environ.get("WECHATSYNC_PLATFORMS", "").strip():
            os.environ["WECHATSYNC_PLATFORMS"] = "weixin"

    def _save_wechat_config(self) -> None:
        updates = {key: self.wechat_vars[key].get().strip() for key, _, _, _ in WECHAT_FIELDS}
        write_env(updates)
        self._wechat_apply_environ()
        self._wechat_log(f"公众号配置已保存到 {ENV_PATH}")
        self._toast("公众号配置已保存", "success")

    def _wechat_set_env_indicator(self, text: str, ok: bool) -> None:
        self.wechat_env_var.set(text)
        self.wechat_dot.configure(text_color=STATUS_COLORS["online" if ok else "offline"])

    def _wechat_check_env(self) -> None:
        self._wechat_apply_environ()
        self._run_task(
            self.wechat_check_btn,
            work=wechat.check_cli,
            on_success=lambda version: (
                self._wechat_set_env_indicator(f"wechatsync：{version}", True),
                self._wechat_log(f"wechatsync 可用：{version}"),
            ),
            on_error=lambda exc: (
                self._wechat_set_env_indicator(f"wechatsync 不可用", False),
                self._wechat_log(f"环境检测失败：{exc}"),
                self._toast(str(exc), "error"),
            ),
        )

    def _wechat_check_login(self) -> None:
        self._wechat_apply_environ()
        platform = self.wechat_vars["WECHATSYNC_PLATFORMS"].get().strip() or None

        def on_error(exc: Exception) -> None:
            self.wechat_login_var.set("检查失败")
            self._wechat_log(f"检查登录失败：{exc}")
            self._toast(str(exc), "error")

        self._run_task(
            self.wechat_login_btn,
            work=lambda: wechat.check_login(platform),
            on_success=self._on_wechat_login_status,
            on_error=on_error,
        )

    def _on_wechat_login_status(self, text: str) -> None:
        logged_in = wechat.is_logged_in(text)
        self.wechat_login_var.set("已登录" if logged_in else "未登录")
        self._wechat_log(f"登录状态：{text}")
        self._toast("公众号已登录" if logged_in else "公众号未登录", "success" if logged_in else "warn")

    def _wechat_preview(self) -> None:
        self._wechat_apply_environ()
        site_key = self.wechat_vars["NEWS_SITE"].get().strip() or None

        def work() -> list[str]:
            site = sites.get_site(site_key or os.getenv("NEWS_SITE", "yahoo_hk_finance"))
            return [f"{item.title}（{item.source} {item.published}）" for item in site.list_items()]

        def on_success(titles: list[str]) -> None:
            self._wechat_log(f"共 {len(titles)} 条候选：")
            for index, title in enumerate(titles, start=1):
                self._wechat_log(f"  {index}. {title}")
            self._toast(f"共 {len(titles)} 条候选", "success")

        self._run_task(
            self.wechat_preview_btn,
            work=work,
            on_success=on_success,
            on_error=lambda exc: (self._wechat_log(f"获取候选失败：{exc}"), self._toast(str(exc), "error")),
        )

    def _wechat_run(self, dry_run: bool) -> None:
        self._wechat_apply_environ()
        site_key = self.wechat_vars["NEWS_SITE"].get().strip() or None
        self._run_task(
            self.wechat_dryrun_btn if dry_run else self.wechat_run_btn,
            work=lambda: news_pipeline.run(
                site_key=site_key, dry_run=dry_run, source="gui", log=self._wechat_log
            ),
            on_success=self._on_wechat_finished,
            on_error=lambda exc: (self._wechat_log(f"执行失败：{exc}"), self._toast(str(exc), "error")),
        )

    def _on_wechat_finished(self, results: list) -> None:
        published = sum(1 for item in results if item.get("published"))
        failed = sum(1 for item in results if item.get("error"))
        self._wechat_log(f"完成：成功 {published} 篇，失败 {failed} 篇（共 {len(results)}）")
        self._toast(f"完成：成功 {published}，失败 {failed}", "success" if not failed else "warn")

    def _wechat_refresh_candidates(self) -> None:
        self._wechat_apply_environ()
        site_key = self.wechat_vars["NEWS_SITE"].get().strip() or None

        def work() -> tuple[list, int]:
            items = news_pipeline.list_candidates(site_key)
            published = news_pipeline.published_source_urls()
            unpublished = [item for item in items if item.url not in published]
            return unpublished, len(items) - len(unpublished)

        def on_success(payload: tuple[list, int]) -> None:
            unpublished, excluded = payload
            self._wechat_items = unpublished
            labels = [
                f"#{index} {_clip_text(item.title)}（{item.source}）"
                for index, item in enumerate(unpublished, start=1)
            ]
            self.wechat_candidate_combo.configure(values=labels or [""])
            self.wechat_candidate_combo.set(labels[0] if labels else "")
            self.wechat_candidate_var.set(f"可用 {len(unpublished)} 条 / 已排除已发布 {excluded} 条")
            self._wechat_log(f"候选已刷新：可用 {len(unpublished)} 条，已排除已发布 {excluded} 条")
            self._toast(f"候选 {len(unpublished)} 条", "success" if unpublished else "warn")

        self._run_task(
            self.wechat_refresh_btn,
            work=work,
            on_success=on_success,
            on_error=lambda exc: (
                self._wechat_log(f"刷新候选失败：{exc}"),
                self._toast(str(exc), "error"),
            ),
        )

    def _wechat_selected_item(self):
        label = self.wechat_candidate_combo.get().strip()
        for index, candidate in enumerate(self._wechat_items, start=1):
            if label.startswith(f"#{index} "):
                return candidate
        return None

    def _wechat_open_source(self) -> None:
        item = self._wechat_selected_item()
        if item is None:
            self._toast("请先点「刷新候选」并选择一条", "warn")
            return
        webbrowser.open(item.url)
        self._wechat_log(f"已打开原文：{item.url}")
        self._toast("已在浏览器打开原文", "info")

    def _wechat_publish_selected(self) -> None:
        item = self._wechat_selected_item()
        if item is None:
            self._toast("请先点「刷新候选」并选择一条", "warn")
            return
        self._wechat_apply_environ()
        site_key = self.wechat_vars["NEWS_SITE"].get().strip() or None

        def work() -> list:
            return news_pipeline.run(
                site_key=site_key, source="gui", log=self._wechat_log, selected=[item]
            )

        def on_success(results: list) -> None:
            self._on_wechat_finished(results)
            self._wechat_refresh_candidates()

        self._run_task(
            self.wechat_publish_sel_btn,
            work=work,
            on_success=on_success,
            on_error=lambda exc: (
                self._wechat_log(f"发布失败：{exc}"),
                self._toast(str(exc), "error"),
            ),
        )

    # ---------- 进度 / 线程 ----------

    def _poll_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(POLL_INTERVAL_MS, self._poll_queue)

    def _run_task(self, button: ctk.CTkButton, work, on_success, on_error) -> None:
        original = button.cget("text")
        button.configure(state="disabled")
        self._start_btn_anim(button, original)
        self._push_busy()
        self._spawn(
            work,
            lambda r: self._finish(button, on_success, r),
            lambda e: self._finish(button, on_error, e),
        )

    def _spawn(self, work, on_success, on_error) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 统一回主线程展示
                self._queue.put(lambda e=exc: on_error(e))
            else:
                self._queue.put(lambda r=result: on_success(r))

        threading.Thread(target=runner, daemon=True).start()

    def _finish(self, button: ctk.CTkButton, callback, payload) -> None:
        self._pop_busy()
        self._stop_btn_anim(button)
        button.configure(state="normal")
        callback(payload)

    def _toast(self, message: str, kind: str = "info") -> None:
        if self._toast_label is not None and self._toast_label.winfo_exists():
            self._toast_label.destroy()
        label = ctk.CTkLabel(
            self,
            text=message,
            fg_color=TOAST_COLORS.get(kind, TOAST_COLORS["info"]),
            text_color="white",
            corner_radius=10,
            padx=18,
            pady=8,
        )
        label.place(relx=0.5, y=12, anchor="n")
        self._toast_label = label
        self.after(2600, lambda: label.destroy() if label.winfo_exists() else None)

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------- 服务管理 ----------

    def _mcp_url(self) -> str:
        return self.config_vars["XHS_MCP_URL"].get().strip() or "http://localhost:18060/mcp"

    def _refresh_service_status(self) -> None:
        host, port = parse_host_port(self._mcp_url())
        if port_open(host, port):
            self.service_var.set(f"服务在线（{host}:{port}）")
            self.status_dot.configure(text_color=STATUS_COLORS["online"])
            if not self._login_checked:
                self._login_checked = True
                self._refresh_login_status()
        else:
            self.service_var.set("服务未启动")
            self.status_dot.configure(text_color=STATUS_COLORS["offline"])
            self._login_checked = False
            self._set_login_indicator(None)

    def _set_login_indicator(self, logged_in: bool | None) -> None:
        """更新登录状态指示：True 已登录 / False 未登录 / None 未检测。"""
        self._stop_pulse("login")
        if logged_in is None:
            self.login_dot.configure(text_color=STATUS_COLORS["offline"])
            self.login_var.set("登录未检测")
        elif logged_in:
            self.login_dot.configure(text_color=STATUS_COLORS["online"])
            self.login_var.set("已登录")
        else:
            self.login_dot.configure(text_color=STATUS_COLORS["login"])
            self.login_var.set("未登录")

    def _refresh_login_status(self) -> None:
        """后台调用 check_login_status，仅更新指示，不打扰用户。"""
        self._start_pulse("login", self.login_dot, STATUS_COLORS["login"], STATUS_COLORS["offline"])

        def on_error(exc: Exception) -> None:
            self._login_checked = False
            self._set_login_indicator(None)

        self._spawn(xhs.check_login, on_success=self._on_login_status, on_error=on_error)

    def _periodic_status(self) -> None:
        self._refresh_service_status()
        self.after(STATUS_INTERVAL_MS, self._periodic_status)

    def _start_service(self) -> None:
        if not MCP_EXE.exists():
            self._toast(f"未找到 {MCP_EXE.name}", "error")
            return
        host, port = parse_host_port(self._mcp_url())
        if port_open(host, port):
            self._toast("服务已在运行", "info")
            self._refresh_service_status()
            return

        self._log("正在启动 MCP 服务（首次会下载无头浏览器，请耐心等待）...")
        self._toast("正在启动服务...", "info")
        command = [str(MCP_EXE)]
        if self.config_vars["MCP_HEADLESS"].get().strip().lower() == "false":
            command.append("-headless=false")
            self._log("已启用非无头模式，将显示浏览器窗口（排查用）")
        subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        self._service_waiting = True
        self._service_wait_start = time.time()
        self._push_busy()
        self._tick_service_wait()
        self.after(1000, lambda: self._wait_port(host, port, PORT_READY_TIMEOUT))

    def _tick_service_wait(self) -> None:
        if not self._service_waiting:
            return
        elapsed = int(time.time() - self._service_wait_start)
        self.service_var.set(f"正在启动服务…已等待 {elapsed}s")
        self.status_dot.configure(text_color=STATUS_COLORS["login"])
        self.after(1000, self._tick_service_wait)

    def _end_service_wait(self) -> None:
        if self._service_waiting:
            self._service_waiting = False
            self._pop_busy()

    def _wait_port(self, host: str, port: int, timeout: int) -> None:
        if timeout <= 0:
            self._log("服务启动超时，请查看 MCP 窗口日志")
            self._end_service_wait()
            self._refresh_service_status()
            return
        if port_open(host, port):
            self._log(f"服务已就绪：{host}:{port}")
            self._end_service_wait()
            self._refresh_service_status()
            self._toast("服务已就绪", "success")
            return
        self.after(1000, lambda: self._wait_port(host, port, timeout - 1))

    def _check_login(self) -> None:
        self._start_pulse("login", self.login_dot, STATUS_COLORS["login"], STATUS_COLORS["offline"])
        self._run_task(
            self.check_login_btn,
            work=xhs.check_login,
            on_success=self._on_login_status,
            on_error=self._on_login_error,
        )

    def _on_login_error(self, exc: Exception) -> None:
        self._stop_pulse("login", self.login_dot, STATUS_COLORS["offline"])
        self._log(f"检查登录失败：{exc}")
        self._toast(str(exc), "error")

    def _on_login_status(self, result: str) -> None:
        self._log(f"登录状态：{result}")
        self._login_checked = True
        self._set_login_indicator(xhs.is_logged_in(result))

    def _login(self) -> None:
        host, port = parse_host_port(self._mcp_url())
        if not port_open(host, port):
            self._fallback_login("MCP 服务未在线")
            return
        self._run_task(
            self.login_btn,
            work=xhs.get_login_qrcode,
            on_success=self._show_qrcode,
            on_error=lambda exc: self._fallback_login(str(exc)),
        )

    def _show_qrcode(self, qrcode_base64: str) -> None:
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(base64.b64decode(qrcode_base64)))
        except Exception as exc:  # noqa: BLE001 解码失败则回退登录 exe
            self._fallback_login(f"二维码解析失败：{exc}")
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("扫码登录")
        dialog.geometry("320x400")
        dialog.transient(self)
        ctk.CTkLabel(dialog, text="请用小红书 App 扫码登录", font=ctk.CTkFont(size=14)).pack(pady=(16, 8))
        photo = ctk.CTkImage(light_image=image, dark_image=image, size=(260, 260))
        qr_label = ctk.CTkLabel(dialog, image=photo, text="")
        qr_label.image = photo
        qr_label.pack(pady=8)
        ctk.CTkLabel(dialog, text="登录成功后窗口会自动关闭", text_color="gray").pack(pady=(0, 12))
        self._qr_dialog = dialog
        self._qr_photo = photo
        self._log("已获取登录二维码，请扫码")
        self._toast("请扫码登录", "info")
        self._poll_login(dialog)

    def _poll_login(self, dialog: ctk.CTkToplevel) -> None:
        if not dialog.winfo_exists():
            return

        def on_status(result: str) -> None:
            if not dialog.winfo_exists():
                return
            if xhs.is_logged_in(result):
                dialog.destroy()
                self._login_checked = True
                self._set_login_indicator(True)
                self._log("登录成功")
                self._toast("登录成功", "success")
            else:
                self.after(2000, lambda: self._poll_login(dialog))

        def on_error(exc: Exception) -> None:
            if dialog.winfo_exists():
                self._log(f"登录状态轮询失败：{exc}")
                self.after(3000, lambda: self._poll_login(dialog))

        self._spawn(xhs.check_login, on_status, on_error)

    def _fallback_login(self, reason: str) -> None:
        self._log(f"{reason}，改用本地登录工具")
        if not LOGIN_EXE.exists():
            self._toast(f"未找到 {LOGIN_EXE.name}", "error")
            return
        subprocess.Popen([str(LOGIN_EXE)], cwd=str(BASE_DIR), creationflags=subprocess.CREATE_NEW_CONSOLE)
        self._toast("已打开登录工具，请扫码", "info")

    # ---------- 生成 / 发布 ----------

    def _reset_steps(self) -> None:
        self._active_step = -1
        for index in range(len(PUBLISH_STEPS)):
            self._apply_step_label(index, "pending")

    def _apply_step_label(self, index: int, state: str) -> None:
        if index >= len(self.step_labels):
            return
        mark = {"pending": "○", "active": "●", "done": "✓", "error": "✕"}[state]
        self.step_labels[index].configure(
            text=f"{mark} {PUBLISH_STEPS[index]}", text_color=STEP_COLORS[state]
        )

    def _on_step(self, stage: str) -> None:
        self._queue.put(lambda s=stage: self._advance_step(s))

    def _advance_step(self, stage: str) -> None:
        if stage not in PUBLISH_STEPS:
            return
        index = PUBLISH_STEPS.index(stage)
        self._active_step = index
        for i in range(len(PUBLISH_STEPS)):
            if i < index:
                self._apply_step_label(i, "done")
            elif i == index:
                self._apply_step_label(i, "active")
            else:
                self._apply_step_label(i, "pending")

    def _complete_step(self, stage: str) -> None:
        if stage not in PUBLISH_STEPS:
            return
        for i in range(PUBLISH_STEPS.index(stage) + 1):
            self._apply_step_label(i, "done")
        self._active_step = -1

    def _mark_steps_done(self) -> None:
        for i in range(len(PUBLISH_STEPS)):
            self._apply_step_label(i, "done")
        self._active_step = -1

    def _mark_steps_error(self) -> None:
        if 0 <= self._active_step < len(PUBLISH_STEPS):
            self._apply_step_label(self._active_step, "error")

    def _generate(self) -> None:
        topic = self.topic_entry.get().strip()
        if not topic:
            self._toast("请输入主题", "warn")
            return
        backend = self.gen_combo.get()
        backend = None if backend == "默认" else backend
        selected = self.count_menu.get()
        count = None if selected == COUNT_DEFAULT_LABEL else int(selected)
        self._apply_form_to_environ()
        self._reset_steps()

        def work() -> Article:
            article, out_dir = publish_module.build_article(
                topic, gen=backend, count=count, log=self._log, on_step=self._on_step
            )
            self.current_out_dir = out_dir
            return article

        self._run_task(self.generate_btn, work, self._on_generated, self._on_error)

    def _on_generated(self, article: Article) -> None:
        self.current_article = article
        self._complete_step("配图")
        self.title_entry.delete(0, "end")
        self.title_entry.insert(0, article.title)
        self.tags_entry.delete(0, "end")
        self.tags_entry.insert(0, "，".join(article.tags))
        self.content_text.delete("1.0", "end")
        self.content_text.insert("1.0", article.content)
        self._show_thumbs(article.images)
        self._log(f"生成完成：标题 {article.title}，配图 {len(article.images)} 张")
        self._toast("生成完成，可编辑后发布", "success")

    def _show_thumbs(self, paths: list[str]) -> None:
        for child in self.thumbs_frame.winfo_children():
            child.destroy()
        self._thumb_refs.clear()
        try:
            from PIL import Image
        except ImportError:
            return
        for path in paths[:9]:
            try:
                image = Image.open(path)
            except OSError:
                continue
            photo = ctk.CTkImage(light_image=image, dark_image=image, size=(100, 100))
            self._thumb_refs.append(photo)
            ctk.CTkLabel(self.thumbs_frame, image=photo, text="").pack(side="left", padx=3)

    def _publish(self) -> None:
        if self.current_article is None:
            self._toast("请先点击“生成并配图”", "warn")
            return
        article = self._article_from_form()
        self._apply_form_to_environ()
        visibility = self.visibility_combo.get()
        original = self.original_var.get()
        out_dir = self.current_out_dir
        self._reset_steps()

        def work() -> str:
            result = publish_module.run(
                article=article,
                out_dir=out_dir,
                visibility=visibility,
                original=original,
                log=self._log,
                on_step=self._on_step,
            )
            return result.publish_result or "已完成"

        self._run_task(self.publish_btn, work, self._on_published, self._on_error)

    def _article_from_form(self) -> Article:
        assert self.current_article is not None
        tags = [t.strip() for t in self.tags_entry.get().replace("，", ",").split(",") if t.strip()]
        return Article(
            title=self.title_entry.get().strip(),
            content=self.content_text.get("1.0", "end").strip(),
            tags=tags,
            image_keywords=self.current_article.image_keywords,
            image_keywords_zh=self.current_article.image_keywords_zh,
            images=self.current_article.images,
        )

    def _on_published(self, result: str) -> None:
        self._mark_steps_done()
        self._log(f"发布完成：{result}")
        self._toast("发布完成", "success")

    def _on_error(self, exc: Exception) -> None:
        self._mark_steps_error()
        self._log(f"失败：{exc}")
        self._toast(str(exc), "error")

    @staticmethod
    def _slug(text: str) -> str:
        ascii_text = "".join(c if c.isascii() and c.isalnum() else "_" for c in text).strip("_")
        return ascii_text[:30] or "note"

    # ---------- 配置保存与测试 ----------

    def _apply_form_to_environ(self) -> None:
        for key, _label, _secret, _choices in CONFIG_FIELDS:
            os.environ[key] = self.config_vars[key].get().strip()
        for key, _label, _choices in AUTO_FIELDS:
            os.environ[key] = self.auto_vars[key].get().strip()

    def _save_config(self) -> None:
        updates = {key: self.config_vars[key].get().strip() for key, _, _, _ in CONFIG_FIELDS}
        updates.update({key: self.auto_vars[key].get().strip() for key, _, _ in AUTO_FIELDS})
        write_env(updates)
        self._apply_form_to_environ()
        self.visibility_combo.set(updates.get("XHS_VISIBILITY") or VISIBILITIES[0])
        self._log(f"配置已保存到 {ENV_PATH}")
        self._toast("配置已保存", "success")

    def _test_gallery(self) -> None:
        self._apply_form_to_environ()
        self._run_task(
            self.test_gallery_btn,
            work=lambda: images_module.fetch_images(["nature"], ARTICLES_DIR / "_gallery_test", count=1),
            on_success=lambda paths: (self._log(f"图库可用，已下载：{paths[0]}"), self._toast("图库可用", "success")),
            on_error=lambda exc: (self._log(f"图库测试失败：{exc}"), self._toast(str(exc), "error")),
        )

    def _test_generate(self) -> None:
        self._apply_form_to_environ()
        backend = self.gen_combo.get()
        backend = None if backend == "默认" else backend
        self._run_task(
            self.test_generate_btn,
            work=lambda: generate_module.generate("用一句话介绍春天", backend=backend),
            on_success=lambda article: (self._log(f"生成可用，返回标题：{article.title}"), self._toast("生成可用", "success")),
            on_error=lambda exc: (self._log(f"生成测试失败：{exc}"), self._toast(str(exc), "error")),
        )


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    try:
        main()
    except (GenerateError, xhs.XhsError, images_module.ImageError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        sys.exit(1)
