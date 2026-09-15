"""翻译为简体：调用 MiniMax（OpenAI 兼容接口）把英文/繁体正文转成简体中文。

正文按块处理，图片块用 [[IMG0]] 占位符参与翻译，以保持图文顺序；
若模型未原样保留占位符，则回退为逐段翻译，图片块位置不变。
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable

import requests

from sites.base import ImageBlock, TextBlock

REQUEST_TIMEOUT = 120
DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "MiniMax-M2.1"
DEFAULT_CHUNK_CHARS = 1200
MAX_TOKENS = 8192
THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
THINK_TAG_PATTERN = re.compile(r"</?think>", re.IGNORECASE)
IMG_TOKEN_PATTERN = re.compile(r"\[\[\s*IMG\s*(\d+)\s*\]\]", re.IGNORECASE)
PARAGRAPH_SPLIT_PATTERN = re.compile(r"\n\s*\n")

SYSTEM_PROMPT = (
    "你是一个新闻正文的翻译与清洗工具。"
    "任务一：若原文为英文，逐段翻译成简体中文；若为繁体中文，转换为简体中文；已是简体则保持原样。"
    "任务二：删除与正文无关的段落，例如作者署名、订阅/关注/扫码/下载等推广引导、"
    "免责声明、与正文无关的编辑标注；不确定是否无关时一律保留。"
    "要求：事实、数字、标点、换行不得改写或增删；公司名、产品名、人名等专有名词保留原文"
    "（必要时可在首次出现处用括号附中文）。"
    "直接输出结果，不要任何解释、不要代码块。"
    "若文本中出现 [[IMGn]] 形式的标记，必须原样保留，不能改动、删除或移动。"
)
ENTITY_PROMPT = (
    "你是新闻信息抽取助手。从给定新闻标题与正文中提取用于图片检索的关键词，只输出 JSON："
    '{"companies": ["公司或机构名称"], "people": ["人物姓名"], "keywords": ["其他关键检索词"]}。'
    "要求：companies/people 必须逐一列出标题与正文中出现的**所有**公司/机构名与人物姓名"
    "（包括仅被提及、投资方、被投方、高管等主体，不要遗漏；没有则为空数组）；"
    "名称尽量给出常见英文写法（如 SoftBank/OpenAI/Masayoshi Son），也可附中文名；"
    "keywords 填与该新闻主题最贴切的具体英文名词（行业/产品/事件，如 semiconductor、stock chart）；"
    "每类最多 5 个；不要输出解释，不要代码块。"
)


class TranslateError(Exception):
    """繁转简失败。"""


def _strip_think(text: str) -> str:
    text = THINK_PATTERN.sub("", text)
    return THINK_TAG_PATTERN.sub("", text)


def _chat(system_prompt: str, user_text: str, max_tokens: int = MAX_TOKENS) -> str:
    base_url = os.getenv("MINIMAX_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    model = os.getenv("MINIMAX_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if not api_key:
        raise TranslateError("缺少 MINIMAX_API_KEY，请先在 .env 中配置")
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "temperature": 0.2,
                "max_tokens": max_tokens,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TranslateError(f"调用 MiniMax 失败：{exc}") from exc
    if resp.status_code != 200:
        raise TranslateError(f"MiniMax 接口返回 {resp.status_code}：{resp.text[:300]}")
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise TranslateError(f"MiniMax 返回格式异常：{resp.text[:300]}") from exc
    if not content:
        raise TranslateError("MiniMax 返回空内容")
    return _strip_think(str(content)).strip()


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in PARAGRAPH_SPLIT_PATTERN.split(text):
        length = len(paragraph)
        if current and size + length > limit:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += length
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def translate_text(text: str, log: Callable[[str], None] | None = None) -> str:
    """把一段文本转成简体中文（英文翻译 / 繁体转换，长文自动分段）。"""
    logger = log or (lambda _msg: None)
    limit = int(os.getenv("TRANSLATE_CHUNK_CHARS") or DEFAULT_CHUNK_CHARS)
    chunks = _chunk_text(text, limit)
    if len(chunks) > 1:
        logger(f"文本较长，分 {len(chunks)} 段翻译")
    return "\n\n".join(_chat(SYSTEM_PROMPT, chunk) for chunk in chunks)


def translate_title(title: str, log: Callable[[str], None] | None = None) -> str:
    """标题翻译为简体；失败则回退原文，不拖垮整篇。"""
    logger = log or (lambda _msg: None)
    title = (title or "").strip()
    if not title:
        return title
    try:
        return _chat(SYSTEM_PROMPT, title).strip() or title
    except TranslateError as exc:
        logger(f"标题翻译失败，保留原文：{exc}")
        return title


def _split_by_tokens(translated: str, images: list[ImageBlock]) -> list | None:
    """按 [[IMGn]] 占位符还原图文块；占位符不完整则返回 None。"""
    result: list = []
    position = 0
    found: list[int] = []
    for match in IMG_TOKEN_PATTERN.finditer(translated):
        segment = translated[position : match.start()]
        for paragraph in PARAGRAPH_SPLIT_PATTERN.split(segment):
            paragraph = paragraph.strip()
            if paragraph:
                result.append(TextBlock(paragraph))
        index = int(match.group(1))
        if index < 0 or index >= len(images):
            return None
        found.append(index)
        result.append(images[index])
        position = match.end()
    tail = translated[position:]
    for paragraph in PARAGRAPH_SPLIT_PATTERN.split(tail):
        paragraph = paragraph.strip()
        if paragraph:
            result.append(TextBlock(paragraph))
    if found != list(range(len(images))):
        return None
    return result


def translate_blocks(blocks: list, log: Callable[[str], None] | None = None) -> list:
    """整篇翻译，保持图文顺序；占位符异常时回退逐段翻译。"""
    logger = log or (lambda _msg: None)
    images = [block for block in blocks if isinstance(block, ImageBlock)]
    if not any(isinstance(block, TextBlock) for block in blocks):
        return blocks

    pieces: list[str] = []
    image_index = 0
    for block in blocks:
        if isinstance(block, TextBlock):
            pieces.append(block.text)
        else:
            pieces.append(f"[[IMG{image_index}]]")
            image_index += 1

    translated = _chat(SYSTEM_PROMPT, "\n\n".join(pieces))
    restored = _split_by_tokens(translated, images)
    if restored is not None:
        return restored

    logger("图片占位符丢失，回退逐段翻译")
    result: list = []
    cache: dict[str, str] = {}
    for block in blocks:
        if isinstance(block, ImageBlock):
            result.append(block)
            continue
        cached = cache.get(block.text)
        if cached is None:
            cached = _chat(SYSTEM_PROMPT, block.text)
            cache[block.text] = cached
        result.append(TextBlock(cached))
    return result


def _extract_json_object(text: str) -> dict | None:
    cleaned = _strip_think(text)
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(cleaned[index:])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _clean_terms(value) -> list[str]:
    items: list[str] = []
    if isinstance(value, list):
        for item in value:
            item = " ".join(str(item).split()).strip(" -•*·\t")
            if item and item not in items:
                items.append(item)
    return items[:5]


def entity_terms(
    text: str, log: Callable[[str], None] | None = None
) -> tuple[list[str], list[str], list[str]]:
    """从标题+正文提取（公司名, 人名, 其他检索词），失败返回三个空列表。"""
    logger = log or (lambda _msg: None)
    text = (text or "").strip()
    if not text:
        return [], [], []
    try:
        raw = _chat(ENTITY_PROMPT, text[:3000], max_tokens=1024)
    except TranslateError as exc:
        logger(f"提取配图实体失败：{exc}")
        return [], [], []
    data = _extract_json_object(raw)
    if not data:
        return [], [], []
    return (
        _clean_terms(data.get("companies")),
        _clean_terms(data.get("people")),
        _clean_terms(data.get("keywords")),
    )


def _selftest() -> None:
    images = [ImageBlock("http://img/1.jpg"), ImageBlock("http://img/2.jpg")]
    ok = _split_by_tokens("第一段。\n\n[[IMG0]]\n\n第二段。\n\n[[IMG1]]\n\n第三段。", images)
    assert ok is not None
    kinds = [type(block).__name__ for block in ok]
    assert kinds == ["TextBlock", "ImageBlock", "TextBlock", "ImageBlock", "TextBlock"], kinds
    assert ok[1].url == "http://img/1.jpg"

    assert _split_by_tokens("第一段。\n\n[[IMG0]]", images) is None
    assert _split_by_tokens("没有占位符", images) is None

    dropped = _split_by_tokens("第一段。\n\n[[IMG0]]\n\n[[IMG1]]\n\n第三段。", images)
    assert dropped is not None
    dropped_kinds = [type(block).__name__ for block in dropped]
    assert dropped_kinds == ["TextBlock", "ImageBlock", "ImageBlock", "TextBlock"], dropped_kinds

    chunks = _chunk_text("段落一。\n\n" + "很长的段落。" * 200, 50)
    assert len(chunks) > 1, chunks
    assert "".join(chunks).replace("\n\n", "").endswith("很长的段落。")

    original_chat = _chat
    try:
        def _boom(*_args, **_kwargs):
            raise TranslateError("stub")

        globals()["_chat"] = _boom
        assert translate_title("繁體標題") == "繁體標題"
        globals()["_chat"] = lambda *_args, **_kwargs: "简体标题"
        assert translate_title("繁體標題") == "简体标题"
        assert translate_title("   ") == ""
        globals()["_chat"] = lambda *_args, **_kwargs: (
            '前言 {"companies": ["SoftBank", "OpenAI"], "people": ["Masayoshi Son"],'
            ' "keywords": ["semiconductor", "stock chart"]} 结尾'
        )
        assert entity_terms("軟銀與OpenAI") == (
            ["SoftBank", "OpenAI"],
            ["Masayoshi Son"],
            ["semiconductor", "stock chart"],
        )
        globals()["_chat"] = lambda *_args, **_kwargs: "不是 JSON"
        assert entity_terms("軟銀") == ([], [], [])
        globals()["_chat"] = _boom
        assert entity_terms("軟銀") == ([], [], [])
        assert entity_terms("   ") == ([], [], [])
    finally:
        globals()["_chat"] = original_chat
    print("translator.py 自检通过")


if __name__ == "__main__":
    _selftest()
