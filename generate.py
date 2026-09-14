"""内容生成模块：提示词可配置，支持 OpenAI 兼容接口与 Anthropic Messages 接口。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

import requests

import history

TITLE_MAX_LEN = 20
CONTENT_MAX_LEN = 1000
CONTENT_TARGET_MAX_DEFAULT = 450
IMAGE_KEYWORD_MIN = 3
IMAGE_KEYWORD_MAX = 6
IMAGE_KEYWORD_HARD_MAX = 9
TAGS_MIN = 3
TAGS_MAX_DEFAULT = 4
REQUEST_TIMEOUT = 120
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_MAX_TOKENS = 8192
ANTHROPIC_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.85
DEFAULT_PROMPT_FILE = "prompts/note_prompt.txt"
BASE_DIR = Path(__file__).resolve().parent

DEFAULT_PROMPT_TEMPLATE = """你是一个 30 岁左右的普通职场人，在小红书分享真实的职场经历和见闻。你不是营销号，也不是知识博主，就是一个爱吐槽、爱记录的上班族。

【身份与口吻】
- 全程第一人称，像跟朋友聊天：可以口语、碎碎念、带情绪。
- 必须有具体细节：时间、地点、人物关系、对话、数字、金额、当时的心理活动。
- 允许自嘲、吐槽、纠结、委屈，不要永远正能量。
- 不要写成公众号/教科书，不要“三点建议”式总结。

【本次主题】
{topic}

【内容要求】
- 不超过 {content_target} 字，段落短，每段 1-3 句。
- 像真发生过的事，有场景、有转折、有结尾的一句真实感受。
- 禁止 AI 腔：不要“在这个快节奏的时代”“让我们一起”“总之”“希望对你有帮助”这类套话。
- 禁止空泛大道理、排比堆砌、每个自然段对仗工整。
- emoji 总量 0-3 个，不要每行都加。
- 正文中不要出现 # 号、不要写话题标签（标签单独输出）。

【防重复（重要）】
- 最近 {dedup_days} 天已发过的内容如下，请换全新角度和故事，不要撞车：
{recent}
- 与上述内容的重合度不要超过 {dedup_threshold}。

【可参考的热点（只借鉴选题角度和标题写法，严禁照抄原文）】
{reference}

【标题（重要，要博眼球）】
- 不超过 {title_max} 个字。
- 要有冲突/悬念/反差/情绪，让人想点开：可用数字、反差、疑问、“我，30岁，……”句式。
- 参考风格：“我，30岁，被实习生反向管理了”“同事一句话，让我当场想辞职”。

【话题标签（重要，要蹭热度）】
- {tags_min}-{tags_max} 个，不带 # 号。
- 要情绪化、带流量，例如：打工人、职场、职场PUA、奇葩同事、辞职、35岁危机、职场生存。

【配图（重要）】
- 输出 {image_count} 个英文搜索关键词，按内容顺序排列。
- 第 1 个必须是封面：最有冲击力、最贴题、一眼吸引人。
- 关键词要具体可搜（场景/物品/氛围/人物动作），不要抽象概念。
- 再输出 {image_count} 个中文配图关键词（image_keywords_zh），按同样的内容顺序：
  - 若主题与汽车相关：必须是“具体车型 + 视角”，例如“小米SU7 外观”“理想L9 内饰”“特斯拉Model Y 侧面”，不要泛泛的“汽车”。
  - 若主题与汽车无关：给中文场景/物品词即可。

只返回如下 JSON，不要任何解释、不要代码块标记：
{{"title": "...", "content": "...", "tags": ["...", "..."], "image_keywords": ["...", "..."], "image_keywords_zh": ["...", "..."]}}
"""


class GenerateError(Exception):
    """内容生成失败。"""


@dataclass
class Article:
    """一篇待发布笔记的统一结构。"""

    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    image_keywords: list[str] = field(default_factory=list)
    image_keywords_zh: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Article":
        return cls(
            title=data["title"],
            content=data["content"],
            tags=list(data.get("tags", [])),
            image_keywords=list(data.get("image_keywords", [])),
            image_keywords_zh=list(data.get("image_keywords_zh", [])),
            images=list(data.get("images", [])),
        )


def load_prompt_template() -> str:
    """读取用户自定义提示词，缺省用内置模板。"""
    path = Path(os.getenv("PROMPT_FILE", DEFAULT_PROMPT_FILE))
    if not path.is_absolute():
        path = BASE_DIR / path
    if path.exists():
        text = path.read_text(encoding="utf-8-sig").strip()
        if text:
            return text
    return DEFAULT_PROMPT_TEMPLATE


def build_prompt(
    topic: str,
    image_count: int,
    recent_block: str = "（最近没有发过内容）",
    reference_block: str = "（无）",
    dedup_days: int = 7,
    dedup_threshold: float = 0.3,
) -> str:
    content_target = int(os.getenv("CONTENT_TARGET_MAX", str(CONTENT_TARGET_MAX_DEFAULT)))
    tags_max = int(os.getenv("TAGS_MAX", str(TAGS_MAX_DEFAULT)))
    return load_prompt_template().format(
        topic=topic,
        title_max=TITLE_MAX_LEN,
        content_target=content_target,
        content_max=CONTENT_MAX_LEN,
        tags_min=TAGS_MIN,
        tags_max=tags_max,
        image_count=image_count,
        recent=recent_block,
        reference=reference_block,
        dedup_days=dedup_days,
        dedup_threshold=f"{dedup_threshold:.0%}",
    )


def _extract_json(text: str, required_key: str = "title") -> dict[str, Any]:
    """从模型输出中提取 JSON，兼容推理块、代码块与前后缀文本。"""
    cleaned = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"</?think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and required_key in data:
            return data
    except json.JSONDecodeError:
        pass

    # 扫描文本中所有 { 起始位置，取第一个能解析且含目标字段的 JSON 对象
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and required_key in data:
            return data

    raise GenerateError(f"模型未返回合法 JSON：{text[:200]}")


def _call_openai(prompt: str, model: str) -> str:
    base_url = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not base_url or not api_key:
        raise GenerateError("缺少 OPENAI_BASE_URL 或 OPENAI_API_KEY，请先在 .env 中配置")
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": OPENAI_MAX_TOKENS,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise GenerateError(f"OpenAI 接口返回 {resp.status_code}：{resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def _call_anthropic(prompt: str, model: str) -> str:
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").rstrip("/")
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not base_url or not api_key:
        raise GenerateError("缺少 ANTHROPIC_BASE_URL 或 ANTHROPIC_API_KEY，请先在 .env 中配置")
    resp = requests.post(
        f"{base_url}/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise GenerateError(f"Anthropic 接口返回 {resp.status_code}：{resp.text[:300]}")
    return resp.json()["content"][0]["text"]


def _call_model(prompt: str, backend: str | None = None, model: str | None = None) -> str:
    """按后端调用模型，返回原始文本。"""
    backend = (backend or os.getenv("GEN_BACKEND", "openai")).strip().lower()
    if backend == "openai":
        model = model or os.getenv("OPENAI_MODEL", "")
        if not model:
            raise GenerateError("未配置 OPENAI_MODEL，请先在 .env 中设置")
        return _call_openai(prompt, model)
    if backend == "anthropic":
        model = model or os.getenv("ANTHROPIC_MODEL", "")
        if not model:
            raise GenerateError("未配置 ANTHROPIC_MODEL，请先在 .env 中设置")
        return _call_anthropic(prompt, model)
    raise GenerateError(f"未知生成后端：{backend}（仅支持 openai / anthropic）")


def _validate(article: Article) -> None:
    """校验小红书硬性限制，超限直接报错，不截断。"""
    if not article.title:
        raise GenerateError("标题为空")
    if len(article.title) > TITLE_MAX_LEN:
        raise GenerateError(f"标题超过 {TITLE_MAX_LEN} 字（当前 {len(article.title)} 字）：{article.title}")
    if not article.content:
        raise GenerateError("正文为空")
    if len(article.content) > CONTENT_MAX_LEN:
        raise GenerateError(f"正文超过 {CONTENT_MAX_LEN} 字（当前 {len(article.content)} 字）")
    if not article.image_keywords:
        raise GenerateError("模型未返回配图关键词")


def generate(
    topic: str,
    backend: str | None = None,
    model: str | None = None,
    image_count: int | None = None,
    recent_block: str = "（最近没有发过内容）",
    reference_block: str = "（无）",
    dedup_days: int = 7,
    dedup_threshold: float = 0.3,
) -> Article:
    """根据主题生成一篇小红书笔记。"""
    backend = (backend or os.getenv("GEN_BACKEND", "openai")).strip().lower()
    count = image_count or IMAGE_KEYWORD_MIN
    prompt = build_prompt(topic, count, recent_block, reference_block, dedup_days, dedup_threshold)
    raw = _call_model(prompt, backend, model)
    article = Article.from_dict(_extract_json(raw))
    article.image_keywords = article.image_keywords[:IMAGE_KEYWORD_HARD_MAX]
    _validate(article)
    return article


TOPIC_PROMPT = """你是小红书职场内容策划。围绕领域「{domain}」，给出 {count} 个具体、有冲突感、适合第一人称真实分享的选题。

要求：
- 每个选题不超过 15 个字。
- 像真实经历：有场景、有冲突或反差，不要空泛鸡汤。
- 不要与这些近期内容撞题：
{recent}
- 可参考的热点（只借鉴角度，不要照抄）：
{reference}

只返回如下 JSON，不要任何解释：
{{"topics": ["...", "..."]}}
"""


def suggest_topics(
    domain: str,
    count: int = 5,
    recent_block: str = "（无）",
    reference_block: str = "（无）",
    backend: str | None = None,
    model: str | None = None,
) -> list[str]:
    """AI 自动生成候选选题，返回选题列表。"""
    prompt = TOPIC_PROMPT.format(domain=domain, count=count, recent=recent_block, reference=reference_block)
    raw = _call_model(prompt, backend, model)
    data = _extract_json(raw, required_key="topics")
    topics = [str(item).strip() for item in data.get("topics", []) if str(item).strip()]
    if not topics:
        raise GenerateError("模型未返回可用选题")
    return topics[:count]


def generate_unique(
    topic: str,
    backend: str | None = None,
    model: str | None = None,
    image_count: int | None = None,
    days: int | None = None,
    threshold: float | None = None,
    max_retry: int | None = None,
    reference_block: str = "（无）",
    log: Callable[[str], None] | None = None,
) -> tuple[Article, float]:
    """生成一篇与最近 N 天内容相似度不超过阈值的内容，返回 (文章, 相似度)。"""
    logger = log or (lambda _msg: None)
    days = days if days is not None else int(os.getenv("DEDUP_DAYS", "7"))
    threshold = threshold if threshold is not None else float(os.getenv("DEDUP_THRESHOLD", "0.3"))
    max_retry = max_retry if max_retry is not None else int(os.getenv("DEDUP_MAX_RETRY", "3"))

    others = history.recent_texts(days)
    recent_block = history.recent_prompt_block(days)

    best: Article | None = None
    best_sim = 1.0
    last_error: Exception | None = None
    for attempt in range(1, max_retry + 1):
        try:
            article = generate(
                topic,
                backend=backend,
                model=model,
                image_count=image_count,
                recent_block=recent_block,
                reference_block=reference_block,
                dedup_days=days,
                dedup_threshold=threshold,
            )
        except GenerateError as exc:
            last_error = exc
            logger(f"第 {attempt} 次生成失败：{exc}")
            continue
        sim = history.max_similarity(history.record_text(article.to_dict()), others)
        logger(f"第 {attempt} 次生成，与近 {days} 天最大相似度 {sim:.0%}")
        if sim < best_sim:
            best, best_sim = article, sim
        if sim <= threshold:
            return article, sim

    if best is None:
        raise GenerateError(f"连续 {max_retry} 次生成失败：{last_error}")
    logger(f"警告：{max_retry} 次生成相似度均超过 {threshold:.0%}，采用相似度最低的一篇（{best_sim:.0%}）")
    return best, best_sim


def _selftest() -> None:
    """最小自检：验证 JSON 解析、提示词渲染与长度校验，无需网络与密钥。"""
    parsed = _extract_json('前言```json\n{"title":"测试","content":"正文","tags":["a"],"image_keywords":["cat"]}\n```')
    assert parsed["title"] == "测试", parsed
    article = Article.from_dict(parsed)
    _validate(article)
    prompt = build_prompt("被同事甩锅", 4)
    assert "被同事甩锅" in prompt and "4 个英文搜索关键词" in prompt
    topics = _extract_json('```json\n{"topics":["被同事甩锅","加班到凌晨"]}\n```', required_key="topics")
    assert topics["topics"][0] == "被同事甩锅"
    try:
        _validate(Article(title="a" * (TITLE_MAX_LEN + 1), content="ok", image_keywords=["x"]))
    except GenerateError:
        pass
    else:
        raise AssertionError("超长标题未被拦截")
    print("generate.py 自检通过")


if __name__ == "__main__":
    _selftest()
