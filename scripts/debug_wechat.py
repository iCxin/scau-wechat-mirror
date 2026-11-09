#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时诊断脚本：只在 GitHub Actions 上运行，用于确认微信长链返回什么。

不要在本机运行，避免暴露本地公网 IP。
"""

from __future__ import annotations

import re
import sys

from bs4 import BeautifulSoup
from curl_cffi import requests

HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://mp.weixin.qq.com/",
}

BIZ = "MjM5OTE2MzUwNA=="
MID = "2652900708"
SN = "ef3cd8928fc2ab80f97d52204492819f"
ALBUM = "2195323138559836160"

VARIANTS = [
    ("long-raw", f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={MID}&idx=1&sn={SN}"),
    ("long-scene", f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={MID}&idx=1&sn={SN}&scene=38"),
    ("long-chksm", f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid={MID}&idx=1&sn={SN}&chksm=1&scene=27"),
    ("long-encoded", f"https://mp.weixin.qq.com/s?__biz=MjM5OTE2MzUwNA%3D%3D&mid={MID}&idx=1&sn={SN}"),
]

MARKERS = [
    "环境异常", "访问过于频繁", "wappoc_appmsgcaptcha", "请输入验证码", "完成验证",
    "该内容已被发布者删除", "此内容因违规无法查看", "请在微信客户端打开", "参数错误",
    "已被删除", "去微信", "验证",
]


def probe(session: requests.Session, label: str, url: str) -> None:
    try:
        response = session.get(url, headers=HEADERS, timeout=30, allow_redirects=True, impersonate="chrome")
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] request error: {type(exc).__name__}: {exc}", flush=True)
        return

    raw = response.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(raw, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    content = soup.find(id="js_content") or soup.select_one(".rich_media_content")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:400]
    markers = [marker for marker in MARKERS if marker in raw]

    print(f"\n=== {label} ===", flush=True)
    print(f"status={response.status_code} length={len(raw)} final={response.url}", flush=True)
    print(f"title={title!r} has_content={content is not None} markers={markers}", flush=True)
    print(f"text={text!r}", flush=True)


def main() -> int:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.44"})

    # 先访问合集页，让微信种下 cookie。
    try:
        warm = session.get(
            f"https://mp.weixin.qq.com/mp/appmsgalbum?__biz={BIZ}&album_id={ALBUM}&scene=173",
            headers=HEADERS,
            timeout=30,
            impersonate="chrome",
        )
        print(f"album warmup status={warm.status_code} cookies={list(session.cookies.keys())}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"album warmup error: {type(exc).__name__}: {exc}", flush=True)

    for label, url in VARIANTS:
        probe(session, label, url)

    # 全新会话再试一次，确认是否与 cookie 有关。
    fresh = requests.Session()
    fresh.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    probe(fresh, "fresh-session", VARIANTS[0][1])

    return 0


if __name__ == "__main__":
    sys.exit(main())
