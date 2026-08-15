# -*- coding: utf-8 -*-
"""WeChat album scraper module - fetches and processes articles."""

import re
import time
import base64
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from urllib.parse import unquote

ALBUM_API_TEMPLATE = (
    "https://mp.weixin.qq.com/mp/appmsgalbum"
    "?action=getalbum&__biz={biz}"
    "&album_id={album_id}"
    "&count={count}&is_reverse={is_reverse}&f=json"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
}

CSS = """* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f5f5f5; color: #333; line-height: 1.8; }
.container { max-width: 680px; margin: 20px auto; background: #fff; padding: 40px 30px; border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }
.header { border-bottom: 1px solid #eee; padding-bottom: 20px; margin-bottom: 25px; }
.title { font-size: 24px; font-weight: 700; color: #1a1a1a; line-height: 1.4; margin-bottom: 12px; }
.meta { font-size: 13px; color: #999; }
.meta span { margin-right: 15px; }
.content { font-size: 16px; color: #333; word-wrap: break-word; line-height: 1.8; }
.content p { margin: 15px 0; line-height: 1.8; }
.content section { margin: 10px 0; line-height: 1.8; }
.content span { line-height: 1.8; }
.content img { max-width: 100% !important; height: auto !important; display: block; margin: 15px auto; border-radius: 4px; }
.content hr { border: none; border-top: 1px solid #eee; margin: 20px 0; }
.content s, .content del, .content strike { text-decoration: line-through !important; }
.content a { color: #576b95 !important; text-decoration: underline !important; cursor: pointer !important; pointer-events: auto !important; }
.content a:hover { color: #2f54bf !important; }
.video-cover { position: relative; width: 100%; margin: 15px 0; cursor: pointer; border-radius: 8px; overflow: hidden; background: #000; display: block; }
.video-cover img { max-width: 100% !important; height: auto !important; display: block !important; margin: 0 !important; border-radius: 0 !important; }
.video-cover .play-btn { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 60px; height: 60px; background: rgba(0,0,0,0.6); border-radius: 50%; display: flex; align-items: center; justify-content: center; }
.video-cover .play-btn::after { content: ''; display: block; width: 0; height: 0; border-style: solid; border-width: 12px 0 12px 20px; border-color: transparent transparent transparent #fff; margin-left: 4px; }
.video-cover .video-label { position: absolute; bottom: 8px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.7); color: #fff; font-size: 12px; padding: 4px 12px; border-radius: 12px; white-space: nowrap; }
.content blockquote { border-left: 4px solid #d9d9d9; background: #f8f8f8; padding: 12px 16px; margin: 15px 0; color: #666; font-size: 15px; line-height: 1.75; border-radius: 0 4px 4px 0; }
.content blockquote p { margin: 5px 0; line-height: 1.75; }
.content blockquote p:last-child { margin-bottom: 0; }
.footer { margin-top: 30px; padding-top: 15px; border-top: 1px solid #eee; font-size: 12px; color: #bbb; word-break: break-all; }
.footer a { color: #576b95 !important; text-decoration: underline !important; cursor: pointer !important; }"""


def fetch_album_list(biz, album_id, count=20, is_reverse=1):
    """Fetch the latest article list from the album API.

    Returns a tuple: (article_list, album_info)
    - article_list: list of article dicts
    - album_info: dict with album metadata (title, nickname, etc.) or empty dict
    """
    url = ALBUM_API_TEMPLATE.format(biz=biz, album_id=album_id, count=count, is_reverse=is_reverse)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    resp_data = data.get("getalbum_resp", {})
    article_list = resp_data.get("article_list", [])
    base_info = resp_data.get("base_info", {})
    album_info = {
        "title": base_info.get("title", ""),
        "nickname": base_info.get("nickname", ""),
        "article_count": base_info.get("article_count", ""),
    }
    return article_list, album_info


def fetch_article_detail(url):
    """Download and parse a single article. Returns dict with metadata and content HTML."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    # Extract metadata
    title_m = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    title = title_m.group(1) if title_m else "Untitled"

    time_m = re.search(r"var\s+createTime\s*=\s*'([^']+)'", html)
    publish_time = time_m.group(1) if time_m else "Unknown"

    author_m = re.search(r'<a[^>]*id="js_name"[^>]*>([\s\S]*?)</a>', html)
    author = author_m.group(1).strip() if author_m else "Unknown"

    # Extract content area
    soup = BeautifulSoup(html, "html.parser")
    content_div = soup.find("div", id="js_content")
    if content_div:
        content_html = str(content_div)
        # Remove the outer div wrapper
        content_html = re.sub(r'^<div[^>]*id="js_content"[^>]*>', '', content_html)
        content_html = re.sub(r'</div>$', '', content_html)
    else:
        content_html = html

    # Inline images
    content_html = _inline_images(content_html)

    # Fix image attributes: data-src -> src
    content_html = re.sub(r'<img([^>]*?)data-src="', r'<img\1src="', content_html)

    # Replace video iframes with clickable cover images
    content_html = _process_video_iframes(content_html, url)

    # Clean <a> tag inline styles only
    content_html = re.sub(r'<a([^>]*?)\s+style="[^"]*"', r'<a\1', content_html)

    # Remove text-decoration:none from inline styles
    content_html = re.sub(r'style="([^"]*?)text-decoration:\s*none([^"]*?)"', r'style="\1\2"', content_html)
    content_html = re.sub(r'style=";;*"', '', content_html)
    content_html = re.sub(r'style=";\s*"', '', content_html)
    content_html = re.sub(r'\s+style="\s*"', '', content_html)

    return {
        "title": title,
        "author": author,
        "publish_time": publish_time,
        "content_html": content_html,
    }


def _inline_images(content_html):
    """Download images and replace URLs with base64 data URIs."""
    img_urls = re.findall(r'data-src="(https?://mmbiz[^"]+)"', content_html)
    seen = set()

    for img_url in img_urls:
        if img_url in seen:
            continue
        seen.add(img_url)
        try:
            img_resp = requests.get(
                img_url,
                headers={"User-Agent": HEADERS["User-Agent"], "Referer": "https://mp.weixin.qq.com/"},
                timeout=15,
            )
            img_resp.raise_for_status()

            ct = "image/jpeg"
            if "wx_fmt=png" in img_url or "mmbiz_png" in img_url:
                ct = "image/png"
            elif "wx_fmt=gif" in img_url:
                ct = "image/gif"
            elif "wx_fmt=webp" in img_url:
                ct = "image/webp"

            b64 = base64.b64encode(img_resp.content).decode()
            data_uri = f"data:{ct};base64,{b64}"
            content_html = content_html.replace(img_url, data_uri)
            time.sleep(0.2)
        except Exception:
            pass

    return content_html


def _process_video_iframes(content_html, source_url):
    """Replace WeChat video iframes with clickable cover images (since the video player requires auth)."""
    pattern = re.compile(
        r'<iframe\s+class="video_iframe[^"]*"[^>]*?data-cover="([^"]+)"[^>]*?>'
    )

    def replace_video(m):
        encoded_cover = m.group(1)
        cover_url = unquote(encoded_cover)
        try:
            resp = requests.get(
                cover_url,
                headers={"User-Agent": HEADERS["User-Agent"], "Referer": "https://mp.weixin.qq.com/"},
                timeout=15,
            )
            resp.raise_for_status()
            b64 = base64.b64encode(resp.content).decode()
            ct = "image/jpeg"
            if "wx_fmt=png" in cover_url or "mmbiz_png" in cover_url:
                ct = "image/png"
            elif "wx_fmt=gif" in cover_url:
                ct = "image/gif"
            elif "wx_fmt=webp" in cover_url:
                ct = "image/webp"
            data_uri = f"data:{ct};base64,{b64}"
            return (
                f'<a class="video-cover" href="{source_url}" target="_blank" title="点击查看原文观看视频">'
                f'<img src="{data_uri}" alt="视频封面">'
                f'<div class="play-btn"></div>'
                f'<div class="video-label">点击观看视频</div></a>'
            )
        except Exception:
            # Fallback: show a text placeholder
            return (
                f'<a class="video-cover" href="{source_url}" target="_blank" '
                f'style="display:flex;align-items:center;justify-content:center;min-height:120px;background:#f0f0f0;border-radius:8px;color:#999;text-decoration:none;font-size:14px;">'
                f'点击查看原文观看视频</a>'
            )

    return pattern.sub(replace_video, content_html)


def build_full_html(title, author, publish_time, content_html, source_url):
    """Build a complete standalone HTML document."""
    saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = (
        f'<div class="container"><div class="header">'
        f'<h1 class="title">{title}</h1>'
        f'<div class="meta"><span>Author: {author}</span>'
        f'<span>Published: {publish_time}</span>'
        f'<span>Saved: {saved_at}</span></div></div>'
    )
    content = f'<div class="content">{content_html}</div>'
    footer = (
        f'<div class="footer">Source: <a href="{source_url}">{source_url}</a>'
        f'<br>Generated by WeChat Album Reader.</div></div>'
    )
    return (
        f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>{title}</title><style>{CSS}</style></head>'
        f'<body>{header}{content}{footer}</body></html>'
    )
