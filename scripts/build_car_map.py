"""构建/更新汽车之家车系映射表 data/car_series.json。

用法：python scripts/build_car_map.py [车型名 ...]
  不带参数时：扫描汽车之家静态车型表，匹配内置热门车型列表；
  带参数时：只查找指定车型名。

映射结构：{"小米SU7": {"autohome": 6962}}
"""

from __future__ import annotations

import json
import re
import string
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
MAP_FILE = BASE_DIR / "data" / "car_series.json"
LETTER_URL = "https://www.autohome.com.cn/grade/carhtml/{letter}.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.autohome.com.cn/",
}
ENTRY_PATTERN = re.compile(r'id="s(\d+)"[\s\S]*?<h4><a[^>]*>([^<]+)</a>', re.IGNORECASE)

POPULAR = [
    "小米SU7", "小米YU7", "特斯拉Model Y", "特斯拉Model 3", "比亚迪汉", "比亚迪秦PLUS",
    "比亚迪海豹", "理想L9", "理想L7", "理想MEGA", "问界M9", "问界M7", "智界R7", "享界S9",
    "极氪001", "极氪007", "蔚来ES6", "蔚来ET5", "小鹏G6", "小鹏MONA M03", "零跑C11",
    "五菱宏光MINIEV", "大众朗逸", "大众迈腾", "丰田凯美瑞", "本田雅阁", "日产轩逸",
    "奔驰C级", "奔驰E级", "宝马3系", "宝马5系", "奥迪A4L", "奥迪A6L", "吉利星越L",
    "长安CS75 PLUS", "坦克300", "哈弗H6", "奇瑞瑞虎8",
]


def fetch_all_series() -> dict[str, int]:
    """扫描 A-Z 静态车型表，返回 {车系名: 车系ID}。"""
    result: dict[str, int] = {}
    for letter in string.ascii_uppercase:
        url = LETTER_URL.format(letter=letter)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                continue
        except requests.RequestException as exc:
            print(f"  {letter} 请求失败：{exc}")
            continue
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = "utf-8"
        for series_id, name in ENTRY_PATTERN.findall(resp.text):
            clean = name.strip()
            if clean and clean not in result:
                result[clean] = int(series_id)
        print(f"  {letter}: 累计 {len(result)} 个车系")
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    targets = sys.argv[1:] or POPULAR
    print("扫描汽车之家车型表...")
    all_series = fetch_all_series()
    print(f"共获取 {len(all_series)} 个车系")

    mapping: dict[str, dict[str, int]] = {}
    if MAP_FILE.exists():
        try:
            mapping = json.loads(MAP_FILE.read_text(encoding="utf-8-sig"))
        except ValueError:
            mapping = {}

    matched = 0
    for target in targets:
        hit = None
        for name, series_id in all_series.items():
            if target == name or target in name or name in target:
                hit = (name, series_id)
                break
        if hit:
            key = target
            mapping[key] = {"autohome": hit[1], "name": hit[0]}
            matched += 1
            print(f"  {target} -> {hit[0]} ({hit[1]})")
        else:
            print(f"  {target} -> 未找到")

    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {MAP_FILE}（本次匹配 {matched}/{len(targets)}，累计 {len(mapping)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
