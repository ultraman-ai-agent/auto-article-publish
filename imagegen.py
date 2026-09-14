"""封面文生图：调用 MiniMax 图像生成接口，按标题生成公众号封面。

接口：POST {MINIMAX_BASE_URL}/image_generation
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Callable

import requests

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "image-01"
DEFAULT_ASPECT = "16:9"
DEFAULT_IMAGE_TIMEOUT = 60
DEFAULT_BODY_GEN_MAX = 2
PROMPT_TEMPLATE = "新闻配图：{title}。写实摄影风格，主体明确，构图简洁，画面干净，无文字、无水印。"
BODY_PROMPT_TEMPLATE = "新闻配图：{subject}。写实摄影风格，主体明确，构图简洁，画面干净，无文字、无水印。"


class ImageGenError(Exception):
    """封面文生图失败。"""


def generate_image(prompt: str, aspect_ratio: str | None = None) -> bytes:
    """调用 MiniMax 文生图，返回图片字节。"""
    base_url = os.getenv("MINIMAX_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    model = os.getenv("MINIMAX_IMAGE_MODEL", "").strip() or DEFAULT_MODEL
    if not api_key:
        raise ImageGenError("缺少 MINIMAX_API_KEY，无法生成封面")
    aspect = (aspect_ratio or os.getenv("WECHAT_COVER_ASPECT", DEFAULT_ASPECT)).strip() or DEFAULT_ASPECT
    timeout = int(os.getenv("MINIMAX_IMAGE_TIMEOUT") or DEFAULT_IMAGE_TIMEOUT)
    try:
        resp = requests.post(
            f"{base_url}/image_generation",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "prompt": prompt[:1500],
                "aspect_ratio": aspect,
                "response_format": "base64",
                "n": 1,
                "prompt_optimizer": True,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ImageGenError(f"调用 MiniMax 文生图失败：{exc}") from exc
    if resp.status_code != 200:
        raise ImageGenError(f"文生图接口返回 {resp.status_code}：{resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ImageGenError(f"文生图返回非 JSON：{resp.text[:300]}") from exc
    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code"):
        raise ImageGenError(f"文生图失败：{base_resp.get('status_msg')}")
    images = (data.get("data") or {}).get("image_base64") or []
    if not images:
        raise ImageGenError(f"文生图未返回图片：{str(data)[:300]}")
    try:
        return base64.b64decode(images[0])
    except (ValueError, TypeError) as exc:
        raise ImageGenError(f"文生图 base64 解码失败：{exc}") from exc


def generate_cover(title: str, out_dir: Path, log: Callable[[str], None] | None = None) -> str:
    """按标题生成封面，写入 out_dir/cover_ai.jpg，成功返回路径、失败返回空串。

    未配置 MINIMAX_IMAGE_MODEL 时视为关闭 AI 封面。
    """
    logger = log or (lambda _msg: None)
    title = (title or "").strip()
    if not title:
        return ""
    if not os.getenv("MINIMAX_IMAGE_MODEL", "").strip():
        return ""
    prompt = PROMPT_TEMPLATE.format(title=title)
    try:
        content = generate_image(prompt)
    except ImageGenError as exc:
        logger(f"封面文生图失败：{exc}")
        return ""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "cover_ai.jpg"
    try:
        path.write_bytes(content)
    except OSError as exc:
        logger(f"封面写入失败：{exc}")
        return ""
    logger(f"已用 MiniMax 生成封面：{path.name}")
    return str(path.resolve())


def generate_body_images(
    subjects: list[str], count: int, out_dir: Path, log: Callable[[str], None] | None = None
) -> list[str]:
    """正文无图且图库无命中时：按 count 逐张生成配图，返回成功路径列表。

    subjects 为待呈现的主体（公司/人名等）；未配置 MINIMAX_IMAGE_MODEL 时返回空列表；
    生成张数受 NEWS_AUTO_IMAGE_GEN_MAX 限制（0=关闭生成）。
    """
    logger = log or (lambda _msg: None)
    if not os.getenv("MINIMAX_IMAGE_MODEL", "").strip():
        return []
    cap = int(os.getenv("NEWS_AUTO_IMAGE_GEN_MAX") or DEFAULT_BODY_GEN_MAX)
    count = min(count, cap)
    if count <= 0:
        logger("正文配图生成已关闭（NEWS_AUTO_IMAGE_GEN_MAX=0）")
        return []
    subject = "、".join(item for item in (subjects or []) if item).strip() or "该新闻主题"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for index in range(count):
        logger(f"正在生成配图 {index + 1}/{count}（主体：{subject}）...")
        prompt = BODY_PROMPT_TEMPLATE.format(subject=subject)
        try:
            content = generate_image(prompt)
        except ImageGenError as exc:
            logger(f"生成失败 {index + 1}/{count}：{exc}")
            continue
        path = out_dir / f"auto_gen_{index + 1}.jpg"
        try:
            path.write_bytes(content)
        except OSError as exc:
            logger(f"配图写入失败：{exc}")
            continue
        paths.append(str(path.resolve()))
        logger(f"已生成 {len(paths)}/{count}")
    return paths


def _selftest() -> None:
    import tempfile

    original_post = requests.post

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"data": {"image_base64": ["aGVsbG8="]}, "base_resp": {"status_code": 0}}

    try:
        os.environ["MINIMAX_API_KEY"] = "x"
        os.environ["MINIMAX_IMAGE_MODEL"] = "image-01"
        os.environ["NEWS_AUTO_IMAGE_GEN_MAX"] = "2"
        calls = {"n": 0}

        def _fake_post(*args, **kwargs):
            calls["n"] += 1
            return _Resp()

        requests.post = _fake_post
        assert generate_image("测试") == b"hello"
        out = Path(tempfile.mkdtemp())
        path = generate_cover("某公司发布新品", out, log=lambda _msg: None)
        assert path.endswith("cover_ai.jpg") and Path(path).exists()
        assert Path(path).read_bytes() == b"hello"
        calls["n"] = 0
        body = generate_body_images(["SoftBank", "孙正义"], 5, out, log=lambda _msg: None)
        assert len(body) == 2, body
        assert calls["n"] == 2, calls
        os.environ["NEWS_AUTO_IMAGE_GEN_MAX"] = "0"
        assert generate_body_images(["x"], 3, out, log=lambda _msg: None) == []
    finally:
        requests.post = original_post
        os.environ.pop("MINIMAX_IMAGE_MODEL", None)
        os.environ.pop("NEWS_AUTO_IMAGE_GEN_MAX", None)
    print("imagegen.py 自检通过")


if __name__ == "__main__":
    _selftest()
