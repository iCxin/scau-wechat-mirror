#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华南农业大学官方微信公众号镜像同步脚本。

数据链路
--------
1. 通过华南农业大学官方澎湃号（认证政务号 6903558）的分页接口发现推文；
2. 读取每篇澎湃号文章的 __NEXT_DATA__，取得正文、封面、发布时间与“阅读原文”微信链接；
3. 尝试抓取微信原文，成功则使用微信全文，失败则回退到官方澎湃号正文；
4. 清洗 HTML 后写入 web/data，并生成 RSS。

本脚本的设计运行环境是 GitHub Actions。请勿在本机运行抓取流程，以免向
微信公众号 / 澎湃新闻暴露你的本地公网 IP。站点是纯静态的，GitHub Pages
直接读取 web/ 目录。
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup
from curl_cffi import requests


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
DATA_DIR = WEB_DIR / "data"
ARTICLES_DIR = DATA_DIR / "articles"
INDEX_PATH = DATA_DIR / "index.json"
FEED_PATH = WEB_DIR / "feed.xml"

ACCOUNT_NAME = os.getenv("SCAU_ACCOUNT_NAME", "华南农业大学")
WECHAT_ID = os.getenv("SCAU_WECHAT_ID", "scauxmt")
PAPER_ID = os.getenv("SCAU_PAPER_ID", "6903558")
SITE_URL = os.getenv("SITE_URL", "https://icxin.github.io/scau-wechat-mirror").rstrip("/")
REPO_URL = os.getenv("REPO_URL", "https://github.com/iCxin/scau-wechat-mirror")

WECHAT_FETCH = os.getenv("WECHAT_FETCH", "1").lower() not in {"0", "false", "no", "off"}
WECHAT_MAX = max(0, int(os.getenv("WECHAT_MAX", "20") or 20))
MAX_ARTICLES = max(100, int(os.getenv("MAX_ARTICLES", "3000") or 3000))

# 微信合集（专辑）用于回填历史文章。合集是公开页面，不需要公众号后台登录。
SCAU_BIZ = os.getenv("SCAU_BIZ", "MjM5OTE2MzUwNA==")
SCAU_ALBUM_ID = os.getenv("SCAU_ALBUM_ID", "2195323138559836160")
USE_ALBUM = os.getenv("USE_ALBUM", "1").lower() not in {"0", "false", "no", "off"}
ALBUM_MAX_PAGES = max(1, int(os.getenv("ALBUM_MAX_PAGES", "30") or 30))

PAPER_API = "https://api.thepaper.cn/contentapi/cont/pph/gov"
PAPER_BASE = "https://m.thepaper.cn"
ALBUM_API = "https://mp.weixin.qq.com/mp/appmsgalbum"
ALBUM_CACHE_PATH = DATA_DIR / "album.json"

# 仅用于诊断：把列表接口最后一次的状态写进 index.json 的 sync 字段。
LAST_LIST_META: dict = {}

CST = timezone(timedelta(hours=8))
NOW = lambda: datetime.now(CST)


def log(message: str) -> None:
    print(f"[sync] {message}", flush=True)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def clean_text(value) -> str:
    if value is None:
        return ""
    text = html_lib.unescape(str(value))
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_dt(value) -> datetime | None:
    """把接口里五花八门的时间格式统一成北京时间。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:  # 毫秒
            ts /= 1000
        try:
            return datetime.fromtimestamp(ts, CST)
        except (OverflowError, OSError, ValueError):
            return None

    text = clean_text(value)
    if not text:
        return None

    # 纯数字时间戳（接口可能以字符串形式返回毫秒）
    if re.fullmatch(r"\d{10,14}", text):
        try:
            ts = float(text)
            if ts > 1_000_000_000_000:
                ts /= 1000
            return datetime.fromtimestamp(ts, CST)
        except (OverflowError, OSError, ValueError):
            return None

    # ISO / 常见日期格式
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except ValueError:
        pass

    m = re.match(
        r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
        r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        text,
    )
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
                tzinfo=CST,
            )
        except ValueError:
            return None

    m = re.match(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日"
        r"(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        text,
    )
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
                tzinfo=CST,
            )
        except ValueError:
            return None

    # 相对时间（澎湃列表偶尔会返回“2天前”）
    now = NOW()
    if "刚刚" in text or "分钟前" in text:
        m2 = re.match(r"(\d+)\s*分钟前", text)
        return now - timedelta(minutes=int(m2.group(1)) if m2 else 0)
    if "今天" in text:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "昨天" in text:
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    m2 = re.match(r"(\d+)\s*天前", text)
    if m2:
        return now - timedelta(days=int(m2.group(1)))
    m2 = re.match(r"(\d+)\s*小时前", text)
    if m2:
        return now - timedelta(hours=int(m2.group(1)))
    return None


def iso_dt(value) -> str | None:
    dt = parse_dt(value)
    return dt.isoformat(timespec="seconds") if dt else None


def normalize_url(value, *, force_https: bool = False) -> str | None:
    if not value or not isinstance(value, str):
        return None
    url = html_lib.unescape(value).strip().replace("\\/", "/")
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://") and force_https:
        url = "https://" + url[7:]
    if not url.startswith(("http://", "https://")):
        return None
    if url.lower().startswith(("javascript:", "data:", "blob:")):
        return None
    return url


def clean_wechat_url(value) -> str | None:
    """只保留指向微信公众号的稳定链接，并去掉跟踪参数。"""
    url = normalize_url(value)
    if not url:
        return None
    parsed = urlparse(url)
    if "mp.weixin.qq.com" not in parsed.netloc:
        return None

    query = parse_qs(parsed.query)
    keep = {}
    for key in ("__biz", "mid", "idx", "sn"):
        if key in query and query[key]:
            keep[key] = query[key][0]
    if keep:
        return urlunparse(("https", "mp.weixin.qq.com", parsed.path, "", urlencode(keep), ""))

    m = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
    if m:
        return f"https://mp.weixin.qq.com/s/{m.group(1)}"
    return None


def wechat_key(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "sn" in query and query["sn"]:
        return "sn:" + query["sn"][0]
    if "mid" in query and "idx" in query:
        return f"mid:{query['mid'][0]}:{query['idx'][0]}"
    m = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
    if m:
        return "short:" + m.group(1)
    return url


def write_json(path: Path, payload, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    })
    return session


def request(session: requests.Session, method: str, url: str, *, retries: int = 3, **kwargs):
    kwargs.setdefault("timeout", 35)
    kwargs.setdefault("allow_redirects", True)
    kwargs.setdefault("impersonate", "chrome")
    for attempt in range(1, retries + 1):
        try:
            response = session.request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
            log(f"请求失败 {type(exc).__name__} {url} ({attempt}/{retries})")
        else:
            if response.status_code == 200:
                return response
            log(f"HTTP {response.status_code} {url} ({attempt}/{retries})")
            if response.status_code in {401, 404, 410}:
                return None
        if attempt < retries:
            time.sleep(1.8 * attempt)
    return None


# ---------------------------------------------------------------------------
# 澎湃号：发现列表
# ---------------------------------------------------------------------------

def fetch_paper_list(session: requests.Session, page_num: int, page_size: int = 20) -> tuple[list, bool]:
    if page_num == 1:
        LAST_LIST_META.clear()

    response = request(
        session,
        "POST",
        PAPER_API,
        json={"pageNum": page_num, "pageSize": page_size, "pphId": PAPER_ID},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://m.thepaper.cn",
            "Referer": "https://m.thepaper.cn/",
        },
    )
    if response is None:
        LAST_LIST_META.update({"status": None, "message": "请求失败"})
        return [], False
    LAST_LIST_META.update({"status": response.status_code})

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        LAST_LIST_META.update({"message": "响应不是 JSON"})
        log(f"列表接口返回非 JSON（page={page_num}）")
        return [], False
    if not isinstance(payload, dict):
        LAST_LIST_META.update({"message": "响应不是 JSON 对象"})
        return [], False
    LAST_LIST_META.update({"code": payload.get("code")})

    data = payload.get("data")
    if isinstance(data, dict):
        items = data.get("list") or data.get("records") or []
        has_next = data.get("hasNext")
    elif isinstance(data, list):
        items = data
        has_next = len(items) >= page_size
    else:
        items = payload.get("list") or []
        has_next = len(items) >= page_size

    if not isinstance(items, list):
        items = []
    if has_next is None:
        # 部分接口不返回 hasNext；只要本页还有数据就继续翻页，遇到空页自然停止。
        has_next = bool(items)
    if isinstance(has_next, str):
        has_next = has_next.strip().lower() in {"1", "true", "yes", "y"}
    LAST_LIST_META.update({"count": len(items)})
    return items, bool(has_next)


def parse_list_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    cont_id = clean_text(item.get("contId") or item.get("contentId") or "")
    title = clean_text(item.get("name") or item.get("title") or "")
    if not cont_id or not title:
        return None
    if item.get("cornerLabelDesc") == "短剧":
        return None

    published = parse_dt(item.get("pubTimeLong")) or parse_dt(item.get("publishTime")) or parse_dt(item.get("pubTime"))
    author_info = item.get("authorInfo") if isinstance(item.get("authorInfo"), dict) else {}
    cover = normalize_url(
        item.get("pic") or item.get("bigPic") or item.get("sharePic") or item.get("sharePicUrl"),
        force_https=True,
    )
    external = normalize_url(item.get("link")) if item.get("link") else None

    return {
        "id": cont_id,
        "title": title,
        "published_at": published.isoformat(timespec="seconds") if published else None,
        "cover": cover,
        "summary": clean_text(item.get("summary") or item.get("digest") or ""),
        "author": clean_text(item.get("author") or author_info.get("sname") or ""),
        "paper_url": f"{PAPER_BASE}/newsDetail_forward_{cont_id}",
        "external_url": external,
        "raw_tags": [
            clean_text(tag.get("tag"))
            for tag in (item.get("tagList") or [])
            if isinstance(tag, dict) and clean_text(tag.get("tag"))
        ],
    }


# ---------------------------------------------------------------------------
# 澎湃号：正文页
# ---------------------------------------------------------------------------

def extract_dom_content(soup: BeautifulSoup) -> str:
    """__NEXT_DATA__ 缺失时，用正文段落密度兜底。"""
    best = None
    best_score = 0
    for div in soup.find_all("div"):
        paragraphs = div.find_all("p", recursive=False)
        if len(paragraphs) < 2:
            continue
        text_len = len(div.get_text(" ", strip=True))
        if text_len < 200:
            continue
        score = len(paragraphs) * 60 + min(text_len, 4000)
        if score > best_score and len(div.find_all("div")) < 80:
            best = div
            best_score = score
    if best is not None:
        return "".join(str(child) for child in best.contents)

    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return f"<p>{escape(clean_text(meta.get('content')))}</p>"
    return ""


def find_wechat_url(soup: BeautifulSoup, raw_html: str, detail: dict) -> str | None:
    anchors = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str) or "mp.weixin.qq.com" not in href:
            continue
        anchors.append((clean_text(anchor.get_text(" ", strip=True)), href))

    for text, href in anchors:
        if "阅读原文" in text or "原文" in text:
            cleaned = clean_wechat_url(href)
            if cleaned:
                return cleaned
    for _, href in anchors:
        cleaned = clean_wechat_url(href)
        if cleaned:
            return cleaned

    found: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and "mp.weixin.qq.com" in value:
            found.append(value)

    walk(detail)
    for candidate in found:
        cleaned = clean_wechat_url(candidate)
        if cleaned:
            return cleaned

    raw = raw_html.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
    for match in re.finditer(r"https?://mp\.weixin\.qq\.com/[^\s\"'<>\\]+", raw):
        cleaned = clean_wechat_url(match.group(0))
        if cleaned:
            return cleaned
    return None


def fetch_paper_detail(session: requests.Session, cont_id: str) -> dict | None:
    url = f"{PAPER_BASE}/newsDetail_forward_{cont_id}"
    response = request(session, "GET", url, headers={"Referer": "https://m.thepaper.cn/"})
    if response is None:
        return None

    raw_html = response.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(raw_html, "lxml")
    detail: dict = {}
    next_data = None
    script = soup.find("script", id="__NEXT_DATA__")
    if script is not None:
        try:
            next_data = json.loads(script.get_text())
        except Exception:  # noqa: BLE001
            next_data = None

    if isinstance(next_data, dict):
        page_props = ((next_data.get("props") or {}).get("pageProps") or {})
        detail_data = page_props.get("detailData") or {}
        if isinstance(detail_data, dict):
            candidate = (
                detail_data.get("contentDetail")
                or detail_data.get("liveDetail")
                or (detail_data.get("specialDetail") or {}).get("specialInfo")
                or {}
            )
            if isinstance(candidate, dict):
                detail = candidate

    title = clean_text(detail.get("name") or detail.get("shareName") or "")
    if not title:
        h1 = soup.find("h1")
        if h1 is not None:
            title = clean_text(h1.get_text(" ", strip=True))
    if not title:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = clean_text(og_title.get("content"))

    content = detail.get("content") or ""
    if not isinstance(content, str):
        content = ""
    if not content.strip():
        content = extract_dom_content(soup)

    if detail.get("videos") and isinstance(detail.get("videos"), list):
        video_parts = []
        for video in detail["videos"]:
            if not isinstance(video, dict):
                continue
            src = normalize_url(video.get("url"), force_https=True)
            poster = normalize_url(video.get("coverUrl"), force_https=True)
            if src:
                attrs = f'src="{escape(src)}" controls preload="metadata"'
                if poster:
                    attrs += f' poster="{escape(poster)}"'
                video_parts.append(f"<video {attrs}></video>")
        if video_parts:
            content = "".join(video_parts) + content

    summary = clean_text(detail.get("summary") or detail.get("desc") or "")
    if not summary:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            summary = clean_text(meta.get("content"))

    published = parse_dt(detail.get("publishTime")) or parse_dt(detail.get("pubTime"))
    cover = normalize_url(
        detail.get("bigPic") or detail.get("sharePic") or detail.get("pic"),
        force_https=True,
    )
    original_url = find_wechat_url(soup, raw_html, detail)

    if not content.strip() and not summary:
        return None

    return {
        "title": title,
        "content_html": content,
        "summary": summary,
        "published_at": published.isoformat(timespec="seconds") if published else None,
        "cover": cover,
        "original_url": original_url,
        "raw_tags": [
            clean_text(tag.get("tag"))
            for tag in (detail.get("tagList") or [])
            if isinstance(tag, dict) and clean_text(tag.get("tag"))
        ],
    }


# ---------------------------------------------------------------------------
# 微信合集：历史文章发现（公开页面，无需公众号后台）
# ---------------------------------------------------------------------------

def load_album_cache() -> dict:
    if ALBUM_CACHE_PATH.exists():
        try:
            data = json.loads(ALBUM_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                data.setdefault("album_id", SCAU_ALBUM_ID)
                data.setdefault("biz", SCAU_BIZ)
                data.setdefault("title", "")
                data.setdefault("article_count", 0)
                data.setdefault("items", [])
                return data
        except Exception as exc:  # noqa: BLE001
            log(f"合集缓存读取失败，将重建：{exc}")
    return {
        "album_id": SCAU_ALBUM_ID,
        "biz": SCAU_BIZ,
        "title": "",
        "article_count": 0,
        "fetched": 0,
        "pages": 0,
        "error": None,
        "updated_at": None,
        "items": [],
    }


def parse_album_item(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    url = clean_wechat_url(raw.get("url"))
    title = clean_text(raw.get("title"))
    if not url or not title:
        return None

    published = parse_dt(raw.get("create_time")) or parse_dt(raw.get("update_time"))
    cover = normalize_url(
        raw.get("cover_img_1_1") or raw.get("cover_img_wxid") or raw.get("cover"),
        force_https=True,
    )
    msgid = str(raw.get("msgid") or "").strip()
    itemidx = raw.get("itemidx")
    digits = re.sub(r"\D", "", msgid)
    if digits:
        article_id = "wx" + digits[-24:]
    else:
        article_id = "wx" + hashlib.md5(url.encode("utf-8")).hexdigest()[:18]

    return {
        "id": article_id,
        "title": title,
        "published_at": published.isoformat(timespec="seconds") if published else None,
        "published_label": published.isoformat(timespec="seconds")[:10] if published else "",
        "cover": cover,
        "summary": "",
        "author": ACCOUNT_NAME,
        "paper_url": None,
        "external_url": url,
        "origin": "album",
        "msgid": msgid,
        "itemidx": itemidx,
        "raw_tags": [],
    }


def fetch_album(session: requests.Session, album_id: str, biz: str, max_pages: int) -> tuple[list, dict]:
    meta = {
        "album_id": album_id,
        "biz": biz,
        "title": "",
        "article_count": 0,
        "pages": 0,
        "fetched": 0,
        "error": None,
    }
    if not album_id or not biz:
        meta["error"] = "缺少合集 id 或 biz"
        return [], meta

    # 先访问合集页，让微信种下必要的 cookie。
    request(
        session,
        "GET",
        ALBUM_API,
        retries=1,
        params={"__biz": biz, "album_id": album_id, "scene": 173},
        headers={"Referer": "https://mp.weixin.qq.com/"},
    )

    params = {
        "action": "getalbum",
        "__biz": biz,
        "album_id": album_id,
        "count": 20,
        "f": "json",
    }
    items: list[dict] = []
    begin_msgid = ""
    begin_itemidx = 1

    for page in range(1, max_pages + 1):
        page_params = dict(params)
        if begin_msgid:
            page_params["begin_msgid"] = begin_msgid
            page_params["begin_itemidx"] = str(begin_itemidx)

        response = request(
            session,
            "GET",
            ALBUM_API,
            retries=2,
            params=page_params,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"https://mp.weixin.qq.com/mp/appmsgalbum?__biz={biz}&album_id={album_id}&scene=173",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if response is None:
            meta["error"] = "合集接口请求失败"
            break
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            meta["error"] = "合集接口返回非 JSON"
            break
        if not isinstance(data, dict):
            meta["error"] = "合集接口返回格式异常"
            break

        base_resp = data.get("base_resp") or {}
        ret = base_resp.get("ret", 0)
        if ret not in (0, "0", None):
            meta["error"] = f"合集接口 ret={ret}"
            break

        album_info = data.get("album_resp_info") or {}
        if not meta["title"]:
            meta["title"] = clean_text(album_info.get("title") or "")
        if not meta["article_count"]:
            raw_count = album_info.get("article_count")
            if raw_count is None:
                raw_count = ((data.get("getalbum_resp") or {}).get("base_info") or {}).get("article_count")
            try:
                meta["article_count"] = int(raw_count or 0)
            except (TypeError, ValueError):
                meta["article_count"] = 0

        album_resp = data.get("getalbum_resp") or {}
        article_list = album_resp.get("article_list") or []
        if isinstance(article_list, dict):
            article_list = [article_list]
        if not isinstance(article_list, list) or not article_list:
            break

        meta["pages"] = page
        for raw in article_list:
            parsed = parse_album_item(raw)
            if parsed:
                items.append(parsed)
        meta["fetched"] = len(items)

        last = article_list[-1] if isinstance(article_list[-1], dict) else {}
        next_msgid = str(last.get("msgid") or "")
        next_itemidx = last.get("itemidx", 1)
        continue_flag = album_resp.get("continue_flag")
        if not next_msgid or next_msgid == begin_msgid:
            break
        if continue_flag in (0, "0", False):
            break
        begin_msgid = next_msgid
        begin_itemidx = next_itemidx
        time.sleep(0.7)

    meta["fetched"] = len(items)
    return items, meta


def merge_album_cache(cache: dict, new_items: list, meta: dict) -> tuple[dict, bool]:
    existing = {
        str(item.get("id")): item
        for item in cache.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }
    existing_keys = {
        wechat_key(item.get("external_url"))
        for item in existing.values()
        if wechat_key(item.get("external_url"))
    }

    changed = False
    for item in new_items:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        key = wechat_key(item.get("external_url"))
        if item_id in existing:
            # 补齐缺失的封面/日期，不覆盖已有内容。
            current = existing[item_id]
            for field in ("cover", "published_at", "title"):
                if not current.get(field) and item.get(field):
                    current[field] = item[field]
                    changed = True
            continue
        if key and key in existing_keys:
            continue
        existing[item_id] = item
        if key:
            existing_keys.add(key)
        changed = True

    merged = sorted(existing.values(), key=sort_key, reverse=True)
    if len(merged) > 5000:
        merged = merged[:5000]
        changed = True

    cache["items"] = merged
    cache["fetched"] = len(merged)
    for field in ("album_id", "biz", "title", "article_count"):
        value = meta.get(field)
        if value and cache.get(field) != value:
            cache[field] = value
            changed = True
    if meta.get("error") != cache.get("error"):
        cache["error"] = meta.get("error")
        changed = True
    if meta.get("pages"):
        cache["pages"] = meta["pages"]
    if changed:
        cache["updated_at"] = NOW().isoformat(timespec="seconds")
    return cache, changed


# ---------------------------------------------------------------------------
# 微信原文增强（可选）
# ---------------------------------------------------------------------------

def fetch_wechat_article(session: requests.Session, url: str) -> dict | None:
    response = request(
        session,
        "GET",
        url,
        retries=1,
        timeout=20,
        headers={
            "Referer": "https://mp.weixin.qq.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    if response is None:
        return None

    raw_html = response.content.decode("utf-8", errors="replace")
    if not raw_html:
        return None
    blocked = any(
        marker in raw_html
        for marker in ("环境异常", "访问过于频繁", "wappoc_appmsgcaptcha", "请输入验证码", "完成验证")
    )
    if blocked:
        log("微信原文触发风控，回退到官方澎湃号正文")
        return None
    if "js_content" not in raw_html and "rich_media_content" not in raw_html:
        log("微信原文缺少正文节点，回退到官方澎湃号正文")
        return None

    soup = BeautifulSoup(raw_html, "lxml")
    content_node = soup.find(id="js_content") or soup.select_one(".rich_media_content")
    if content_node is None:
        return None

    title = ""
    title_node = soup.find(id="activity-name")
    if title_node is not None:
        title = clean_text(title_node.get_text(" ", strip=True))
    if not title:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = clean_text(og_title.get("content"))

    account = ""
    account_node = soup.find(id="js_name")
    if account_node is not None:
        account = clean_text(account_node.get_text(" ", strip=True))
    if not account:
        match = re.search(r'var\s+nickname\s*=\s*"([^"]+)"', raw_html)
        if match:
            account = clean_text(match.group(1))
    if account and ACCOUNT_NAME not in account and WECHAT_ID not in account:
        log(f"微信原文账号不匹配（{account}），回退到官方澎湃号正文")
        return None

    published = None
    publish_node = soup.find(id="publish_time")
    if publish_node is not None:
        published = parse_dt(publish_node.get_text(" ", strip=True))
    if published is None:
        match = re.search(r'var\s+ct\s*=\s*"(\d+)"', raw_html)
        if match:
            published = parse_dt(int(match.group(1)))

    cover = None
    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_image and og_image.get("content"):
        cover = normalize_url(og_image.get("content"), force_https=True)
    if not cover:
        match = re.search(r'var\s+msg_cdn_url\s*=\s*"([^"]+)"', raw_html)
        if match:
            cover = normalize_url(match.group(1).replace("\\/", "/"), force_https=True)

    content_html = sanitize_content(str(content_node))
    plain = to_text(content_html)
    if len(plain) < 30 and "<img" not in content_html:
        log("微信原文正文过短，回退到官方澎湃号正文")
        return None

    return {
        "title": title,
        "content_html": content_html,
        "cover": cover,
        "published_at": published.isoformat(timespec="seconds") if published else None,
        "account": account,
        "url": url,
    }


# ---------------------------------------------------------------------------
# HTML 清洗
# ---------------------------------------------------------------------------

ALLOWED_TAGS = {
    "a", "p", "span", "section", "div", "img", "video", "source", "strong", "b",
    "em", "i", "u", "br", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "figure", "figcaption", "table", "thead", "tbody", "tr", "td",
    "th", "code", "pre", "hr",
}
DROP_TAGS = {
    "script", "style", "iframe", "noscript", "template", "object", "embed",
    "form", "input", "button", "select", "textarea", "link", "meta", "head",
}


def normalize_link(value) -> str | None:
    url = normalize_url(value)
    if not url:
        return None
    if url.lower().startswith(("javascript:", "data:", "blob:")):
        return None
    return url


def sanitize_content(raw_html: str) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "lxml")

    for bad in soup.find_all(list(DROP_TAGS)):
        bad.decompose()

    for tag in soup.find_all(True):
        if tag.parent is None:
            continue
        name = (tag.name or "").lower()
        if not name:
            continue
        if name not in ALLOWED_TAGS:
            try:
                tag.unwrap()
            except Exception:  # noqa: BLE001
                tag.decompose()
            continue

        attrs = {}
        if name == "a":
            href = normalize_link(tag.get("href"))
            if href:
                attrs["href"] = href
                attrs["target"] = "_blank"
                attrs["rel"] = "noopener noreferrer"
        elif name == "img":
            src = tag.get("data-src") or tag.get("data-original") or tag.get("data-lazy-src") or tag.get("src")
            if isinstance(src, str) and src.startswith("/") and not src.startswith("//"):
                src = "https://m.thepaper.cn" + src
            src = normalize_url(src, force_https=True)
            if not src:
                tag.decompose()
                continue
            attrs["src"] = src
            alt = tag.get("alt")
            attrs["alt"] = clean_text(alt) if alt else ""
            attrs["loading"] = "lazy"
            attrs["decoding"] = "async"
            attrs["referrerpolicy"] = "no-referrer"
        elif name == "video":
            src = normalize_url(tag.get("src"), force_https=True)
            poster = normalize_url(tag.get("poster"), force_https=True)
            if not src:
                tag.decompose()
                continue
            attrs["src"] = src
            if poster:
                attrs["poster"] = poster
            attrs["controls"] = "controls"
            attrs["preload"] = "metadata"
        elif name == "source":
            src = normalize_url(tag.get("src"), force_https=True)
            if not src:
                tag.decompose()
                continue
            attrs["src"] = src
            if tag.get("type"):
                attrs["type"] = clean_text(tag.get("type"))
        elif name in {"td", "th"}:
            for key in ("colspan", "rowspan"):
                value = tag.get(key)
                if value and str(value).isdigit():
                    attrs[key] = str(value)
        tag.attrs = attrs

    for tag in soup.find_all(["p", "section", "span", "div", "figure", "blockquote"]):
        if not tag.get_text(strip=True) and not tag.find(["img", "video"]):
            tag.decompose()

    body = soup.find("body")
    html_out = "".join(str(child) for child in (body.contents if body else soup.contents))
    html_out = re.sub(r"<!--.*?-->", "", html_out, flags=re.S)
    return html_out.strip()


def to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "lxml")
    for bad in soup.find_all(list(DROP_TAGS)):
        bad.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_summary(content_html: str, preferred: str = "") -> str:
    text = clean_text(preferred)
    if "<" in text and ">" in text:
        text = to_text(str(preferred))
    if len(text) < 25:
        text = re.sub(r"\s+", " ", to_text(content_html)).strip()
    text = re.sub(r"^原创\s*", "", text)
    text = re.sub(r"^#?\s*原创\s*", "", text)
    if len(text) > 120:
        cut = text[:120]
        punct = max(cut.rfind(ch) for ch in "。！？；")
        if punct >= 60:
            cut = cut[: punct + 1]
        text = cut.rstrip("，。；、 ") + "…"
    return text


TAG_RULES = [
    ("通知", ("通知", "公告", "安排", "指南", "招募", "报名", "放假", "开学", "招生", "录取", "考试", "提醒", "服务")),
    ("科研", ("科研", "研究", "学术", "论文", "成果", "团队", "实验室", "期刊", "会议", "论坛", "获奖", "冠军", "特等奖", "金奖", "专利", "项目")),
    ("人物", ("人物", "专访", "故事", "校友", "教师", "学子", "教授", "校长", "院士", "榜样", "青年")),
    ("活动", ("活动", "晚会", "比赛", "展览", "校庆", "返校", "典礼", "开幕", "启动", "直播", "节日")),
    ("校园", ("校园", "食堂", "宿舍", "图书馆", "风景", "花", "节气", "校园卡", "春日", "秋日", "夏天", "冬日")),
]


def classify_tags(title: str, summary: str, extra: list[str] | None = None) -> list[str]:
    text = f"{title} {summary}"
    tags: list[str] = []
    for tag, keywords in TAG_RULES:
        if any(keyword in text for keyword in keywords):
            tags.append(tag)
        if len(tags) >= 2:
            break
    if not tags:
        tags = ["校园"]
    return tags


# ---------------------------------------------------------------------------
# 索引 / RSS
# ---------------------------------------------------------------------------

def empty_index() -> dict:
    return {
        "account": {
            "name": ACCOUNT_NAME,
            "wechat_id": WECHAT_ID,
            "paper_id": PAPER_ID,
            "paper_url": f"https://m.thepaper.cn/gov_{PAPER_ID}",
            "site_url": SITE_URL,
            "repo_url": REPO_URL,
        },
        "generated_at": None,
        "sync": {},
        "stats": {"total": 0, "wechat": 0, "paper": 0, "latest": None},
        "articles": [],
    }


def load_index() -> dict:
    if INDEX_PATH.exists():
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("articles"), list):
                index = empty_index()
                index.update(data)
                index["account"] = {**empty_index()["account"], **(data.get("account") or {})}
                return index
        except Exception as exc:  # noqa: BLE001
            log(f"现有索引读取失败，将重建：{exc}")
    return empty_index()


def sort_key(article: dict) -> str:
    return article.get("published_at") or article.get("fetched_at") or "0000"


def build_stats(articles: list[dict]) -> dict:
    return {
        "total": len(articles),
        "wechat": sum(1 for item in articles if item.get("source") == "wechat"),
        "paper": sum(1 for item in articles if item.get("source") == "paper"),
        "latest": articles[0].get("published_at") if articles else None,
    }


def generate_feed(index: dict) -> None:
    articles = index.get("articles") or []
    if not articles:
        if FEED_PATH.exists():
            FEED_PATH.unlink()
        return

    now = NOW()
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(ACCOUNT_NAME)}官微镜像</title>",
        f"<link>{escape(SITE_URL)}/</link>",
        f"<atom:link href=\"{escape(SITE_URL)}/feed.xml\" rel=\"self\" type=\"application/rss+xml\" />",
        "<description>华南农业大学官方微信公众号推文镜像（非官方站点）</description>",
        "<language>zh-cn</language>",
        f"<lastBuildDate>{format_datetime(now.astimezone(timezone.utc))}</lastBuildDate>",
        "<generator>scau-wechat-mirror</generator>",
    ]
    for article in articles[:40]:
        title = escape(article.get("title") or "无标题")
        link = article.get("original_url") or article.get("paper_url") or f"{SITE_URL}/"
        summary = escape(article.get("summary") or "")
        pub = parse_dt(article.get("published_at"))
        pub_line = f"<pubDate>{format_datetime(pub.astimezone(timezone.utc))}</pubDate>" if pub else ""
        tags = "".join(
            f"<category>{escape(tag)}</category>" for tag in (article.get("tags") or [])
        )
        parts.append(
            "<item>"
            f"<title>{title}</title>"
            f"<link>{escape(link)}</link>"
            f"<guid isPermaLink=\"false\">{escape(article.get('id') or '')}</guid>"
            f"{pub_line}"
            f"<description>{summary}</description>"
            f"{tags}"
            "</item>"
        )
    parts.extend(["</channel>", "</rss>"])
    FEED_PATH.write_text("\n".join(parts) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_article(item: dict, detail: dict, wechat: dict | None) -> tuple[dict, dict] | None:
    content_html = (wechat or {}).get("content_html") or sanitize_content(detail.get("content_html") or "")
    if not content_html:
        fallback = detail.get("summary") or item.get("summary")
        if fallback:
            content_html = f"<p>{escape(clean_text(fallback))}</p>"
    if not content_html:
        return None

    title = clean_text(detail.get("title") or item.get("title"))
    published = detail.get("published_at") or item.get("published_at")
    cover = (wechat or {}).get("cover") or detail.get("cover") or item.get("cover")
    original_url = (
        clean_wechat_url((wechat or {}).get("url"))
        or clean_wechat_url(detail.get("original_url"))
        or clean_wechat_url(item.get("external_url"))
        or normalize_url(item.get("external_url"))
    )
    summary = build_summary(content_html, detail.get("summary") or item.get("summary"))
    tags = classify_tags(title, summary)
    source = "wechat" if wechat else "paper"
    fetched_at = NOW().isoformat(timespec="seconds")

    entry = {
        "id": item["id"],
        "title": title,
        "published_at": published,
        "published_label": (published or "")[:10],
        "cover": cover,
        "summary": summary,
        "tags": tags,
        "source": source,
        "original_url": original_url,
        "paper_url": item.get("paper_url"),
        "word_count": len(to_text(content_html)),
        "fetched_at": fetched_at,
    }
    detail_payload = {
        **entry,
        "content_html": content_html,
    }
    return entry, detail_payload


def cleanup_article_files(articles: list[dict]) -> None:
    keep = {str(article.get("id")) for article in articles}
    if not ARTICLES_DIR.exists():
        return
    for path in ARTICLES_DIR.glob("*.json"):
        if path.stem not in keep:
            try:
                path.unlink()
            except OSError:
                pass


def run(args) -> int:
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

    index = load_index()
    existing: list[dict] = list(index.get("articles") or [])
    seen_ids = {str(item.get("id")) for item in existing}
    seen_keys = {wechat_key(item.get("original_url")) for item in existing}
    seen_keys.discard(None)

    max_new = args.max_new
    if max_new is None:
        max_new = int(os.getenv("MAX_NEW", "0") or 0)
    if max_new <= 0:
        max_new = 40 if not existing else 12

    discover_pages = args.discover_pages
    if discover_pages is None:
        discover_pages = int(os.getenv("DISCOVER_PAGES", "0") or 0)
    if discover_pages <= 0:
        discover_pages = 10 if not existing else 5

    log(f"开始同步：账号={ACCOUNT_NAME}，现有 {len(existing)} 篇，本次最多新增 {max_new} 篇，发现页数 {discover_pages}")

    session = new_session()
    album_cache = load_album_cache() if USE_ALBUM else {"items": []}
    album_items: list[dict] = list(album_cache.get("items") or [])
    album_meta: dict = {}
    album_changed = False

    discovered: dict[str, dict] = {}
    raw_keys: list[str] = []
    for page in range(1, discover_pages + 1):
        items, has_next = fetch_paper_list(session, page)
        if not items:
            if page == 1:
                log("列表接口未返回文章")
            break
        if page == 1 and isinstance(items[0], dict):
            raw_keys = sorted(str(key) for key in items[0].keys())
        for raw in items:
            parsed = parse_list_item(raw)
            if parsed:
                discovered[parsed["id"]] = parsed
        # 政务号接口的 hasNext 偶尔不可靠；继续翻页，遇到空页会自然停止。
        # 这样既能在正常分页时回填历史，也能在接口不支持翻页时安全退化。
        time.sleep(0.8)

    if USE_ALBUM and SCAU_ALBUM_ID and SCAU_BIZ:
        pages = ALBUM_MAX_PAGES if not album_items else 1
        log(f"读取微信合集：最多 {pages} 页")
        fetched_items, album_meta = fetch_album(session, SCAU_ALBUM_ID, SCAU_BIZ, pages)
        album_cache, album_changed = merge_album_cache(album_cache, fetched_items, album_meta)
        album_items = album_cache.get("items") or []
        if album_changed and not args.dry_run:
            write_json(ALBUM_CACHE_PATH, album_cache)
        log(
            f"合集缓存 {len(album_items)} 篇；接口标注 {album_meta.get('article_count')} 篇；"
            f"错误：{album_meta.get('error')}"
        )
    else:
        album_meta = {"error": "未启用合集回填"}

    if album_items:
        paper_keys = {wechat_key(item.get("external_url")) for item in discovered.values()}
        paper_keys.discard(None)
        paper_titles = {
            (clean_text(item.get("title")), (item.get("published_at") or "")[:10])
            for item in discovered.values()
        }
        added = 0
        for item in album_items:
            if item.get("id") in seen_ids:
                continue
            key = wechat_key(item.get("external_url"))
            if key and (key in seen_keys or key in paper_keys):
                continue
            title_key = (clean_text(item.get("title")), (item.get("published_at") or "")[:10])
            if title_key in paper_titles:
                continue
            discovered[item["id"]] = item
            if key:
                paper_keys.add(key)
            paper_titles.add(title_key)
            added += 1
        log(f"合集补充 {added} 篇待处理文章")

    log(f"列表发现 {len(discovered)} 篇")
    last_error = None
    if not discovered:
        last_error = "列表与合集接口均未返回可识别的文章"

    fresh: list[dict] = []
    for item in discovered.values():
        if item["id"] in seen_ids:
            continue
        key = wechat_key(item.get("external_url"))
        if key and key in seen_keys:
            continue
        fresh.append(item)
    fresh.sort(key=sort_key, reverse=True)
    log(f"待抓取新文章 {len(fresh)} 篇")

    fetched = 0
    wechat_count = 0
    failed = 0
    wechat_attempts = 0

    for item in fresh[:max_new]:
        if item.get("origin") == "album":
            detail = {
                "title": item.get("title"),
                "content_html": "",
                "summary": item.get("summary") or "",
                "published_at": item.get("published_at"),
                "cover": item.get("cover"),
                "original_url": item.get("external_url"),
            }
        else:
            detail = fetch_paper_detail(session, item["id"])
            if detail is None:
                failed += 1
                last_error = "澎湃号正文抓取失败"
                log(f"跳过（正文抓取失败）：{item['title'][:42]}")
                time.sleep(1.0)
                continue

        original = clean_wechat_url(detail.get("original_url")) or clean_wechat_url(item.get("external_url"))
        key = wechat_key(original)
        if key and key in seen_keys:
            log(f"跳过（微信原文已存在）：{item['title'][:42]}")
            continue
        wechat = None
        if WECHAT_FETCH and original and wechat_attempts < WECHAT_MAX:
            wechat_attempts += 1
            wechat = fetch_wechat_article(session, original)
            if wechat:
                wechat_count += 1
            time.sleep(1.2)

        if item.get("origin") == "album" and not wechat:
            failed += 1
            last_error = "微信原文抓取失败（合集文章）"
            log(f"跳过（合集文章微信抓取失败）：{item['title'][:42]}")
            continue

        built = build_article(item, detail, wechat)
        if not built:
            failed += 1
            last_error = "正文清洗后为空"
            log(f"跳过（正文为空）：{item['title'][:42]}")
            continue

        entry, detail_payload = built
        if not args.dry_run:
            write_json(ARTICLES_DIR / f"{entry['id']}.json", detail_payload)
        existing.append(entry)
        seen_ids.add(entry["id"])
        key = wechat_key(entry.get("original_url"))
        if key:
            seen_keys.add(key)
        fetched += 1
        log(f"[{fetched}/{min(len(fresh), max_new)}] {entry['title'][:46]}（{entry['source']}）")
        time.sleep(1.0)

    if args.dry_run:
        log(f"dry-run 完成：新增 {fetched} 篇，其中微信全文 {wechat_count} 篇")
        return 0

    changed = fetched > 0
    first_write = not INDEX_PATH.exists()

    if changed or first_write:
        articles = existing
        # 去重（以 id 为主，同时兼顾微信原文链接）
        deduped: list[dict] = []
        seen: set[str] = set()
        for article in articles:
            article_id = str(article.get("id") or "")
            if article_id and article_id in seen:
                continue
            seen.add(article_id)
            deduped.append(article)
        deduped.sort(key=sort_key, reverse=True)
        if len(deduped) > MAX_ARTICLES:
            deduped = deduped[:MAX_ARTICLES]

        index["articles"] = deduped
        index["account"] = {
            **empty_index()["account"],
            **{
                "name": ACCOUNT_NAME,
                "wechat_id": WECHAT_ID,
                "paper_id": PAPER_ID,
                "paper_url": f"https://m.thepaper.cn/gov_{PAPER_ID}",
                "site_url": SITE_URL,
                "repo_url": REPO_URL,
            },
        }
        index["generated_at"] = NOW().isoformat(timespec="seconds")
        previous_sync = index.get("sync") or {}
        index["sync"] = {
            "last_success": NOW().isoformat(timespec="seconds") if changed else previous_sync.get("last_success"),
            "last_error": None if changed else last_error,
            "last_discovered": len(discovered),
            "last_fresh": len(fresh),
            "last_new": fetched,
            "last_wechat": wechat_count,
            "last_failed": failed,
            "last_pages": discover_pages,
            "last_raw_keys": raw_keys,
            "last_list": dict(LAST_LIST_META),
            "album": {
                "id": SCAU_ALBUM_ID,
                "title": album_cache.get("title"),
                "article_count": album_cache.get("article_count"),
                "cached": len(album_items),
                "pages": album_meta.get("pages"),
                "error": album_meta.get("error"),
            },
        }
        index["stats"] = build_stats(deduped)

        write_json(INDEX_PATH, index, pretty=True)
        generate_feed(index)
        cleanup_article_files(deduped)
    else:
        log("本次没有新文章，保持现有站点不变")

    log(
        f"同步完成：新增 {fetched} 篇，微信全文 {wechat_count} 篇，"
        f"失败 {failed} 篇，库内共 {len(index.get('articles') or [])} 篇"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="华南农业大学官微镜像同步")
    parser.add_argument("--max-new", type=int, default=None, help="本次最多抓取的新文章数")
    parser.add_argument("--discover-pages", type=int, default=None, help="列表抓取页数（每页 20 条）")
    parser.add_argument("--no-wechat", action="store_true", help="不尝试抓取微信原文，仅用官方澎湃号正文")
    parser.add_argument("--dry-run", action="store_true", help="只抓取不写文件")
    args = parser.parse_args()
    if args.no_wechat:
        globals()["WECHAT_FETCH"] = False

    try:
        return run(args)
    except Exception:  # noqa: BLE001
        log("同步出现未预期异常")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
