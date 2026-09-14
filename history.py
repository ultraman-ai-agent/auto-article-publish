"""历史文章与相似度：用于控制最近 N 天的内容重复率。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

ARTICLES_DIR = Path(__file__).resolve().parent / "articles"
_ARTICLE_FILE = "article.json"
_NORMALIZE_PATTERN = re.compile(r"[\s\W_]+", re.UNICODE)


def _load_records() -> list[tuple[datetime, dict]]:
    """读取 articles/*/article.json，返回 (时间, 记录) 列表。"""
    if not ARTICLES_DIR.exists():
        return []
    records: list[tuple[datetime, dict]] = []
    for path in ARTICLES_DIR.glob(f"*/{_ARTICLE_FILE}"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except (OSError, ValueError):
            continue
        records.append((mtime, data))
    records.sort(key=lambda item: item[0], reverse=True)
    return records


def recent_records(days: int) -> list[dict]:
    """返回最近 days 天内的文章记录，按时间倒序。"""
    cutoff = datetime.now() - timedelta(days=days)
    return [data for mtime, data in _load_records() if mtime >= cutoff]


def record_text(record: dict) -> str:
    """把一条记录拼成用于比对的文本。"""
    title = str(record.get("title", ""))
    content = str(record.get("content", ""))
    tags = " ".join(record.get("tags", []) or [])
    return f"{title}\n{tags}\n{content}"


def _normalize(text: str) -> str:
    return _NORMALIZE_PATTERN.sub("", text).lower()


def similarity(text_a: str, text_b: str) -> float:
    """归一化后的文本相似度，返回 0~1。"""
    norm_a, norm_b = _normalize(text_a), _normalize(text_b)
    if not norm_a or not norm_b:
        return 0.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()


def max_similarity(text: str, others: list[str]) -> float:
    """与一组文本的最大相似度。"""
    if not others:
        return 0.0
    return max(similarity(text, other) for other in others)


def recent_texts(days: int) -> list[str]:
    """最近 days 天文章的比对文本列表。"""
    return [record_text(record) for record in recent_records(days)]


def recent_prompt_block(days: int, limit: int = 10) -> str:
    """生成注入提示词的“近期已发内容”摘要。"""
    records = recent_records(days)[:limit]
    if not records:
        return "（最近没有发过内容）"
    lines: list[str] = []
    for index, record in enumerate(records, start=1):
        title = str(record.get("title", "")).strip()
        summary = str(record.get("content", "")).strip().replace("\n", " ")[:60]
        lines.append(f"{index}. {title}｜{summary}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 最小自检：相似度与防重复判定
    assert similarity("今天上班被同事甩锅了", "今天上班被同事甩锅了") == 1.0
    assert similarity("今天上班被同事甩锅了", "周末去露营看星星") < 0.3
    assert max_similarity("abc", []) == 0.0
    near = similarity("领导临时让我加班改方案我好崩溃", "领导临时让我加班改方案我真的很崩溃")
    assert near > 0.6, near
    print("history.py 自检通过")
