"""配图模块：支持 Pexels/Unsplash 图库与汽车之家车系图，统一下载并压缩为 JPEG。

图片来源通过 IMAGE_SOURCE 配置（默认 auto）：
- auto：根据主题/关键词自动判断，汽车内容走汽车之家，其余走图库
- 也可显式指定优先级，如 autohome,pexels
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"
AUTOHOME_PIC_URL = "https://car.autohome.com.cn/pic/series/{series_id}-{category}.html"
AUTOHOME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://car.autohome.com.cn/",
}
STOCK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 60
MAX_IMAGE_BYTES = 500 * 1024
MAX_IMAGE_SIDE = 1440
DEFAULT_COUNT = 3
DEFAULT_GALLERY_DIR = BASE_DIR / "assets" / "gallery"
CAR_MAP_FILE = BASE_DIR / "data" / "car_series.json"
CACHE_DIR = BASE_DIR / "data" / "cache" / "images"
CACHE_TTL_SECONDS = 12 * 3600
AUTOHOME_CATEGORIES = {"外观": "1", "内饰": "10", "座椅": "3", "细节": "12"}
CAR_TOPIC_KEYWORDS = (
    "汽车", "车型", "试驾", "提车", "新车", "新能源", "电动车", "油车", "混动",
    "续航", "智驾", "车评", "车展", "车主", "suv", "mpv", "轿车", "特斯拉",
    "比亚迪", "蔚来", "理想", "小鹏", "问界", "极氪", "小米su7", "小米yu7",
)
AUTOHOME_IMG_PATTERN = re.compile(r"(?:https?:)?//car2?\.autoimg\.cn/[^\s\"'<>]+?\.(?:jpg|jpeg|png)", re.IGNORECASE)
AUTOHOME_SIZE_PATTERN = re.compile(r"/\d+x\d+_0_q95_c42_")


class ImageError(Exception):
    """配图获取失败。"""


@dataclass
class ImageRef:
    """一张待下载图片：地址 + 下载所需 Referer。"""

    url: str
    referer: str | None = None


def _safe_name(text: str) -> str:
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return ascii_text or "img"


def _compress_to_jpeg(data: bytes, dst_path: Path) -> None:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    quality = 80
    while True:
        image.save(dst_path, "JPEG", quality=quality, optimize=True)
        if dst_path.stat().st_size <= MAX_IMAGE_BYTES or quality <= 40:
            break
        quality -= 10


# ---------- 汽车车型映射 ----------

def load_car_map() -> dict[str, dict]:
    if not CAR_MAP_FILE.exists():
        return {}
    try:
        data = json.loads(CAR_MAP_FILE.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def find_car_series(query: str) -> tuple[str, int] | None:
    """在映射表中查找车系，返回 (匹配名, 汽车之家车系ID)。"""
    query = query.strip()
    if not query:
        return None
    car_map = load_car_map()
    normalized = re.sub(r"\s+", "", query).lower()
    for key, info in car_map.items():
        key_norm = re.sub(r"\s+", "", key).lower()
        if key_norm == normalized or normalized in key_norm or key_norm in normalized:
            series_id = info.get("autohome")
            if series_id:
                return key, int(series_id)
    return None


def is_car_context(topic: str | None, keywords_zh: list[str]) -> bool:
    for keyword in keywords_zh:
        if find_car_series(keyword):
            return True
    text = (topic or "").lower()
    return any(word in text for word in CAR_TOPIC_KEYWORDS)


# ---------- 缓存 ----------

def _cache_path(provider: str, query: str) -> Path:
    digest = hashlib.sha1(f"{provider}:{query}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _load_cache(provider: str, query: str) -> list[ImageRef] | None:
    path = _cache_path(provider, query)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if time.time() - data.get("ts", 0) > CACHE_TTL_SECONDS:
        return None
    return [ImageRef(item["url"], item.get("referer")) for item in data.get("refs", [])]


def _save_cache(provider: str, query: str, refs: list[ImageRef]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.time(), "refs": [{"url": r.url, "referer": r.referer} for r in refs]}
        _cache_path(provider, query).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---------- 图库 provider ----------

def _refs_pexels(keyword: str, per_page: int, api_key: str) -> list[ImageRef]:
    resp = requests.get(
        PEXELS_SEARCH_URL,
        params={"query": keyword, "per_page": per_page},
        headers={"Authorization": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        ImageRef(p["src"]["large"])
        for p in resp.json().get("photos", [])
        if p.get("src", {}).get("large")
    ]


def _refs_unsplash(keyword: str, per_page: int, access_key: str) -> list[ImageRef]:
    resp = requests.get(
        UNSPLASH_SEARCH_URL,
        params={"query": keyword, "per_page": per_page},
        headers={"Authorization": f"Client-ID {access_key}"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        ImageRef(r["urls"]["regular"])
        for r in resp.json().get("results", [])
        if r.get("urls", {}).get("regular")
    ]


def _stock_refs(provider: str, keyword: str, count: int) -> list[ImageRef]:
    if provider == "pexels":
        api_key = os.getenv("PEXELS_API_KEY", "").strip()
        if not api_key:
            return []
        return _refs_pexels(keyword, count, api_key)
    access_key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
    if not access_key:
        return []
    return _refs_unsplash(keyword, count, access_key)


# ---------- 汽车之家 provider ----------

def _autohome_page_urls(series_id: int, category: str) -> list[str]:
    url = AUTOHOME_PIC_URL.format(series_id=series_id, category=category)
    resp = requests.get(url, headers=AUTOHOME_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "gb2312"):
        resp.encoding = "utf-8"
    text = resp.text.replace("\\/", "/")
    urls: list[str] = []
    for raw in AUTOHOME_IMG_PATTERN.findall(text):
        full = "https:" + raw if raw.startswith("//") else raw
        full = AUTOHOME_SIZE_PATTERN.sub("/1000x0_0_q95_c42_", full, count=1)
        if full not in urls:
            urls.append(full)
    return urls


def _autohome_refs(query: str, count: int) -> list[ImageRef]:
    """按车系名从汽车之家图库取图。"""
    hit = find_car_series(query)
    if not hit:
        return []
    _name, series_id = hit
    categories = [c.strip() for c in os.getenv("CAR_IMAGE_CATEGORIES", "外观,内饰").split(",") if c.strip()]
    if not categories:
        categories = ["外观"]
    refs: list[ImageRef] = []
    seen: set[str] = set()
    index = 0
    while len(refs) < count and categories:
        category = categories[index % len(categories)]
        category_id = AUTOHOME_CATEGORIES.get(category, "1")
        for url in _autohome_page_urls(series_id, category_id):
            if url in seen:
                continue
            seen.add(url)
            refs.append(ImageRef(url, AUTOHOME_HEADERS["Referer"]))
            if len(refs) >= count:
                break
        index += 1
        if index > len(categories) * 2:
            break
    return refs


# ---------- 下载与编排 ----------

def _download_and_save(ref: ImageRef, dst_path: Path) -> None:
    headers = dict(AUTOHOME_HEADERS if ref.referer else STOCK_HEADERS)
    if ref.referer:
        headers["Referer"] = ref.referer
    resp = requests.get(ref.url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    _compress_to_jpeg(resp.content, dst_path)


def resolve_sources(topic: str | None, keywords: list[str], keywords_zh: list[str]) -> list[str]:
    raw = os.getenv("IMAGE_SOURCE", "auto").strip().lower()
    if raw and raw != "auto":
        return [item.strip() for item in raw.split(",") if item.strip()]
    if is_car_context(topic, keywords_zh):
        return ["autohome", "pexels", "unsplash"]
    return ["pexels", "unsplash"]


def _provider_refs(provider: str, keywords: list[str], keywords_zh: list[str], count: int) -> list[ImageRef]:
    cached = _load_cache(provider, "|".join(keywords_zh or keywords))
    if cached is not None:
        return cached
    if provider == "autohome":
        queries = keywords_zh or keywords
        refs: list[ImageRef] = []
        for query in queries:
            refs.extend(_autohome_refs(query, count))
            if len(refs) >= count:
                break
    else:
        refs = []
        for index in range(count):
            keyword = keywords[index % len(keywords)] if keywords else "nature"
            try:
                refs.extend(_stock_refs(provider, keyword, count))
            except requests.RequestException:
                continue
    if refs:
        _save_cache(provider, "|".join(keywords_zh or keywords), refs)
    return refs


def fetch_images(
    keywords: list[str],
    out_dir: Path,
    count: int = DEFAULT_COUNT,
    keywords_zh: list[str] | None = None,
    topic: str | None = None,
) -> list[str]:
    """按配置的来源获取图片，返回本地绝对路径列表；失败回退本地图库。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    keywords = [k.strip() for k in keywords if k and k.strip()] or ["nature"]
    keywords_zh = [k.strip() for k in (keywords_zh or []) if k and k.strip()]

    saved: list[str] = []
    for provider in resolve_sources(topic, keywords, keywords_zh):
        if len(saved) >= count:
            break
        try:
            refs = _provider_refs(provider, keywords, keywords_zh, count)
        except requests.RequestException:
            continue
        for ref in refs:
            if len(saved) >= count:
                break
            dst_path = out_dir / f"xhs_{provider}_{len(saved) + 1}.jpg"
            try:
                _download_and_save(ref, dst_path)
            except (requests.RequestException, OSError):
                continue
            saved.append(str(dst_path.resolve()))

    if not saved:
        saved = _fallback_gallery(out_dir)
    if not saved:
        raise ImageError("未能获取任何图片：请检查图库密钥/网络，或放置本地图库图片")
    return saved


def _fallback_gallery(out_dir: Path) -> list[str]:
    if not DEFAULT_GALLERY_DIR.exists():
        return []
    saved: list[str] = []
    for index, src in enumerate(sorted(DEFAULT_GALLERY_DIR.glob("*"))):
        if src.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        dst_path = out_dir / f"gallery_{index + 1}.jpg"
        _compress_to_jpeg(src.read_bytes(), dst_path)
        saved.append(str(dst_path.resolve()))
    return saved


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    query = args[0] if args else "nature"
    car_query = args[1] if len(args) > 1 else ""
    result = fetch_images(
        [query], Path("articles/_images_test"), count=4,
        keywords_zh=[car_query] if car_query else [], topic=car_query or query,
    )
    print(f"获取到 {len(result)} 张图片：")
    for path in result:
        print(f"  {path}  ({Path(path).stat().st_size / 1024:.0f} KB)")
