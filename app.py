# -*- coding: utf-8 -*-
"""WeChat Album Reader - Flask web application with scheduled scraping."""

import os
import sys
import re
import json
import io
import zipfile
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import scraper

# Support both normal Python and PyInstaller onefile mode
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "articles.db")
ARTICLES_DIR = os.path.join(BASE_DIR, "articles")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    """Load album configuration from config.json."""
    if not os.path.exists(CONFIG_PATH):
        return []
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_album_names():
    """Return list of album names from config."""
    return [a["name"] for a in load_config()]


def get_all_album_names():
    """Return merged list of album names from config and database.

    Config names come first (preserving order), then DB-only names
    (from articles whose album config has been deleted).
    """
    config_names = get_album_names()
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT album_name FROM articles WHERE album_name IS NOT NULL AND album_name != ''"
    ).fetchall()
    conn.close()
    seen = set(config_names)
    for row in rows:
        name = row["album_name"]
        if name not in seen:
            config_names.append(name)
            seen.add(name)
    return config_names


def save_config(albums):
    """Save album configuration to config.json."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(albums, f, ensure_ascii=False, indent=2)


def parse_album_url(url):
    """Extract __biz and album_id from a WeChat album or article URL.

    Supports:
    - Album URLs: mp.weixin.qq.com/mp/appmsgalbum?__biz=xxx&album_id=xxx
    - Article URLs with query params: mp.weixin.qq.com/s?__biz=xxx&...
    - Short article URLs: mp.weixin.qq.com/s/xxx (fetches page to extract __biz and album_id)
    """
    from urllib.parse import urlparse, parse_qs
    import requests as req

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    biz = params.get("__biz", [""])[0]
    album_id = params.get("album_id", [""])[0]

    # If it's an album URL with both params, we're done
    if biz and album_id:
        return biz, album_id

    # If it's a short article URL, fetch the page to extract __biz and album_id
    if "/s/" in url or "/s?" in url:
        try:
            resp = req.get(url, headers=scraper.HEADERS, timeout=15, allow_redirects=True)
            html = resp.text

            # Extract biz from page
            if not biz:
                biz_m = re.search(r'__biz\s*=\s*([A-Za-z0-9=]+)', html)
                if biz_m:
                    biz = biz_m.group(1)

            # Extract album_id from album links embedded in the article page
            if not album_id:
                album_m = re.search(r'album_id[=:"\s&]+(\d{10,})', html)
                if album_m:
                    album_id = album_m.group(1)
        except Exception:
            pass

    return biz, album_id


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(ARTICLES_DIR, exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT NOT NULL,
            url           TEXT UNIQUE NOT NULL,
            author        TEXT,
            summary       TEXT,
            content_html  TEXT,
            publish_time  TEXT,
            create_time   INTEGER,
            saved_at      TEXT,
            url_hash      TEXT,
            file_path     TEXT,
            album_name    TEXT,
            is_read       INTEGER DEFAULT 0,
            read_at       TEXT
        )
    """)
    # Migration: add columns if missing (for existing databases)
    migration_cols = [
        ("file_path", "TEXT"),
        ("album_name", "TEXT"),
        ("is_read", "INTEGER DEFAULT 0"),
        ("read_at", "TEXT"),
    ]
    for col, col_type in migration_cols:
        try:
            conn.execute(f"SELECT {col} FROM articles LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {col_type}")
            print(f"[db] Added {col} column to articles table")
    # Create indexes for performance
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_album_name ON articles(album_name)",
        "CREATE INDEX IF NOT EXISTS idx_create_time ON articles(create_time DESC)",
        "CREATE INDEX IF NOT EXISTS idx_album_create_time ON articles(album_name, create_time DESC)",
        "CREATE INDEX IF NOT EXISTS idx_album_unread ON articles(album_name, is_read)",
    ]:
        conn.execute(idx_sql)
    # Create FTS5 full-text search index (title + content) with trigram tokenizer
    # for Chinese substring matching support
    conn.execute("DROP TABLE IF EXISTS articles_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts
        USING FTS5(id, title, content, tokenize='trigram')
    """)
    # Rebuild index if empty (populate from existing articles)
    rebuild_fts_index()
    conn.commit()
    conn.close()


def _extract_text(html):
    """Extract plain text from HTML content, stripping tags and base64 data."""
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', ' ', html, flags=re.IGNORECASE)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:50000]  # Limit to 50k chars to keep FTS index manageable


def rebuild_fts_index():
    """Rebuild the FTS5 index from all existing articles."""
    conn = get_db()
    # Clear existing index
    conn.execute("DELETE FROM articles_fts")
    # Populate from all articles
    rows = conn.execute("SELECT id, title, content_html FROM articles").fetchall()
    count = 0
    for r in rows:
        content = _extract_text(r["content_html"] or "")
        conn.execute(
            "INSERT INTO articles_fts(rowid, id, title, content) VALUES (?, ?, ?, ?)",
            (r["id"], r["id"], r["title"], content),
        )
        count += 1
    conn.commit()
    conn.close()
    print(f"[fts] Rebuilt FTS index: {count} articles")
    return count


def _safe_filename(title, article_id=None):
    """Create a safe filename from article title only."""
    safe = re.sub(r'[\\/:*?"<>|]', '_', title)
    safe = safe.strip().strip('.')
    if not safe:
        safe = f"article_{article_id}" if article_id else "untitled"
    if len(safe) > 80:
        safe = safe[:80]
    return f"{safe}.html"


def html_to_markdown(html_content, title, author, publish_time, source_url):
    """Convert article HTML content to Markdown format."""
    from markdownify import markdownify as md

    # Build markdown with metadata header
    lines = []
    lines.append(f"# {title}")
    lines.append("")
    if author:
        lines.append(f"> **作者：** {author}")
    if publish_time:
        lines.append(f"> **发布时间：** {publish_time}")
    if source_url:
        lines.append(f"> **原文链接：** {source_url}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Strip script and style tags before conversion
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', ' ', html_content, flags=re.IGNORECASE)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', ' ', text, flags=re.IGNORECASE)

    # Convert HTML to Markdown
    md_text = md(text, heading_style="ATX", strip=["img"])
    lines.append(md_text.strip())

    return "\n".join(lines)


def _is_short_query(search):
    """Check if search query is too short for trigram FTS (1-2 chars)."""
    return len(search.strip()) <= 2


def count_articles(album=None, search=None, unread_only=False):
    conn = get_db()
    if search:
        if _is_short_query(search):
            # Short query: use LIKE fallback (trigram can't handle 1-2 char queries)
            pattern = f"%{search}%"
            sql = """
                SELECT COUNT(*) as n
                FROM articles a
                WHERE a.title LIKE ? ESCAPE '\\'
            """
            params = [pattern]
        else:
            sql = """
                SELECT COUNT(*) as n
                FROM articles a
                JOIN articles_fts ON a.id = articles_fts.rowid
                WHERE articles_fts MATCH ?
            """
            params = [search]
    else:
        sql = "SELECT COUNT(*) as n FROM articles WHERE 1=1"
        params = []
    if album:
        if search and _is_short_query(search):
            sql += " AND a.album_name = ?"
        else:
            sql += " AND album_name = ?" if not search else " AND a.album_name = ?"
        params.append(album)
    if unread_only:
        if search and _is_short_query(search):
            sql += " AND a.is_read = 0"
        else:
            sql += " AND is_read = 0" if not search else " AND a.is_read = 0"
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row["n"]


def get_articles(search=None, album=None, limit=20, offset=0, unread_only=False):
    conn = get_db()
    # Exclude content_html from list queries — it can be very large (base64 images)
    # and is never needed on listing pages, only on the article detail page.
    if search:
        if _is_short_query(search):
            # Short query: use LIKE fallback (trigram can't handle 1-2 char queries)
            pattern = f"%{search}%"
            query = (
                "SELECT a.id, a.title, a.url, a.author, a.summary, a.publish_time,"
                " a.create_time, a.saved_at, a.url_hash, a.file_path, a.album_name, a.is_read, a.read_at"
                " FROM articles a"
                " WHERE a.title LIKE ? ESCAPE '\\'"
            )
            params = [pattern]
        else:
            # Use FTS for full-text search
            query = (
                "SELECT a.id, a.title, a.url, a.author, a.summary, a.publish_time,"
                " a.create_time, a.saved_at, a.url_hash, a.file_path, a.album_name, a.is_read, a.read_at"
                " FROM articles a"
                " JOIN articles_fts ON a.id = articles_fts.rowid"
                " WHERE articles_fts MATCH ?"
            )
            params = [search]
    else:
        query = (
            "SELECT id, title, url, author, summary, publish_time,"
            " create_time, saved_at, url_hash, file_path, album_name, is_read, read_at"
            " FROM articles WHERE 1=1"
        )
        params = []
    if album:
        if search and _is_short_query(search):
            query += " AND a.album_name = ?"
        else:
            query += " AND album_name = ?" if not search else " AND a.album_name = ?"
        params.append(album)
    if unread_only:
        if search and _is_short_query(search):
            query += " AND a.is_read = 0"
        else:
            query += " AND is_read = 0" if not search else " AND a.is_read = 0"
    query += " ORDER BY create_time DESC LIMIT ? OFFSET ?"
    params.append(limit)
    params.append(offset)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def get_unread_counts():
    """Return dict mapping album_name -> number of unread articles."""
    conn = get_db()
    query = (
        "SELECT album_name, COUNT(*) as n FROM articles "
        "WHERE is_read = 0 AND album_name IS NOT NULL AND album_name != '' "
        "GROUP BY album_name"
    )
    rows = conn.execute(query).fetchall()
    conn.close()
    return {r["album_name"]: r["n"] for r in rows}


def get_article(article_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    conn.close()
    return row


def mark_article_read(article_id):
    """Mark an article as read. Returns True if state changed."""
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "UPDATE articles SET is_read = 1, read_at = ? WHERE id = ? AND is_read = 0",
        (now, article_id),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def mark_all_read(album=None):
    """Mark all articles (optionally in one album) as read. Returns count updated."""
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if album:
        cur = conn.execute(
            "UPDATE articles SET is_read = 1, read_at = ? WHERE is_read = 0 AND album_name = ?",
            (now, album),
        )
    else:
        cur = conn.execute(
            "UPDATE articles SET is_read = 1, read_at = ? WHERE is_read = 0",
            (now,),
        )
    updated = cur.rowcount
    conn.commit()
    conn.close()
    return updated


def article_exists(url):
    conn = get_db()
    row = conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,)).fetchone()
    conn.close()
    return row is not None


def delete_article(article_id):
    """Delete an article from database and remove its HTML file."""
    conn = get_db()
    row = conn.execute("SELECT file_path FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        conn.close()
        return False
    # Delete HTML file if exists
    if row["file_path"]:
        file_path = os.path.join(ARTICLES_DIR, row["file_path"])
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
    conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    conn.execute("DELETE FROM articles_fts WHERE rowid = ?", (article_id,))
    conn.commit()
    conn.close()
    return True


def save_article(meta, url, create_time, album_name=None):
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO articles
           (title, url, author, summary, content_html, publish_time, create_time, saved_at, url_hash, album_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            meta["title"],
            url,
            meta["author"],
            "",
            meta["content_html"],
            meta["publish_time"],
            create_time,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            hashlib.md5(url.encode()).hexdigest(),
            album_name,
        ),
    )
    article_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Save standalone HTML file to local directory
    file_path = None
    try:
        filename = _safe_filename(meta["title"], article_id)
        full_html = scraper.build_full_html(
            meta["title"],
            meta["author"],
            meta["publish_time"],
            meta["content_html"],
            url,
        )
        file_path = os.path.join(ARTICLES_DIR, filename)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(full_html)
        # Update database with file path
        conn = get_db()
        conn.execute("UPDATE articles SET file_path = ? WHERE id = ?", (filename, article_id))
        conn.commit()
        conn.close()
        print(f"[scraper] Saved HTML file: {filename}")
    except Exception as e:
        print(f"[scraper] Failed to save HTML file: {e}")
        file_path = None

    # Insert into FTS5 full-text search index
    content_text = _extract_text(meta["content_html"] or "")
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO articles_fts(rowid, id, title, content) VALUES (?, ?, ?, ?)",
        (article_id, article_id, meta["title"], content_text),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Scraping job
# ---------------------------------------------------------------------------

scrape_status = {"running": False, "last_run": None, "last_new": 0, "last_error": None, "next_run": None}


def run_scrape(max_new=10):
    """Fetch album list and save new articles for all configured albums."""
    if scrape_status["running"]:
        return
    scrape_status["running"] = True
    new_count = 0
    errors = []
    try:
        albums = load_config()
        if not albums:
            scrape_status["last_error"] = "No albums configured"
            return

        for album in albums:
            album_name = album.get("name", "Unknown")
            biz = album.get("biz", "")
            album_id = album.get("album_id", "")
            count = album.get("count", 20)
            is_reverse = album.get("is_reverse", 1)
            if not biz or not album_id:
                errors.append(f"{album_name}: missing biz or album_id")
                continue

            print(f"[scraper] Scraping album: {album_name} (is_reverse={is_reverse})")
            try:
                article_list, album_info = scraper.fetch_album_list(biz, album_id, count, is_reverse=is_reverse)
            except Exception as e:
                errors.append(f"{album_name}: {e}")
                print(f"[scraper] Failed to fetch album list for {album_name}: {e}")
                continue

            # Use API-provided album title if available
            api_title = album_info.get("title", "")
            if api_title and api_title != album_name:
                print(f"[scraper] Album name: '{album_name}' -> '{api_title}' (from API)")
                album_name = api_title

            saved = 0
            for art in article_list:
                url = art.get("url", "")
                if not url or article_exists(url):
                    continue
                if saved >= max_new:
                    break
                try:
                    meta = scraper.fetch_article_detail(url)
                    save_article(meta, url, art.get("create_time", 0), album_name=album_name)
                    saved += 1
                    new_count += 1
                except Exception as e:
                    print(f"[scraper] Failed to save article: {e}")
            print(f"[scraper] {album_name}: saved {saved} new articles")

        scrape_status["last_new"] = new_count
        scrape_status["last_error"] = "; ".join(errors) if errors else None
    except Exception as e:
        scrape_status["last_error"] = str(e)
        print(f"[scraper] Error: {e}")
    finally:
        scrape_status["running"] = False
        scrape_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        scrape_status["next_run"] = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{% block title %}WeChat Album Reader{% endblock %}</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f0f2f5; color: #333; }
.navbar { background: #fff; border-bottom: 1px solid #e8e8e8; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
.navbar h1 { font-size: 20px; color: #1a1a1a; }
.navbar h1 a { color: #1a1a1a; text-decoration: none; }
.navbar .stats { font-size: 13px; color: #999; }
.navbar .actions a { margin-left: 12px; font-size: 14px; color: #576b95; text-decoration: none; }
.navbar .actions a:hover { color: #2f54bf; }
.search-bar { max-width: 800px; margin: 20px auto; padding: 0 20px; display: flex; gap: 8px; }
.search-bar input { flex: 1; padding: 10px 16px; border: 1px solid #d9d9d9; border-radius: 6px; font-size: 14px; }
.search-bar button { padding: 10px 24px; background: #576b95; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
.search-bar button:hover { background: #2f54bf; }
.container { max-width: 800px; margin: 0 auto; padding: 0 20px 40px; }
.article-card { background: #fff; border-radius: 8px; padding: 20px 24px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); transition: box-shadow 0.2s; }
.article-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.12); }
.article-card a { text-decoration: none; color: #1a1a1a; }
.article-card h2 { font-size: 17px; margin-bottom: 6px; }
.article-card .meta { font-size: 12px; color: #999; }
.article-card .summary { font-size: 14px; color: #666; margin-top: 8px; line-height: 1.6; }
.article-detail { background: #fff; border-radius: 8px; padding: 40px 30px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.article-detail h1 { font-size: 24px; font-weight: 700; color: #1a1a1a; margin-bottom: 12px; line-height: 1.4; }
.article-detail .meta { font-size: 13px; color: #999; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid #eee; display: flex; align-items: center; flex-wrap: wrap; gap: 4px 0; }
.article-detail .meta span { margin-right: 16px; }
.article-content { font-size: 16px; color: #333; line-height: 1.8; }
.article-content p { margin: 15px 0; line-height: 1.8; }
.article-content section { margin: 10px 0; line-height: 1.8; }
.article-content span { line-height: 1.8; }
.article-content img { max-width: 100% !important; height: auto !important; display: block; margin: 15px auto; border-radius: 4px; }
.article-content hr { border: none; border-top: 1px solid #eee; margin: 20px 0; }
.article-content s, .article-content del, .article-content strike { text-decoration: line-through !important; }
.article-content a { color: #576b95 !important; text-decoration: underline !important; }
.article-content blockquote { border-left: 4px solid #d9d9d9; background: #f8f8f8; padding: 12px 16px; margin: 15px 0; color: #666; font-size: 15px; line-height: 1.75; border-radius: 0 4px 4px 0; }
.article-content blockquote p { margin: 5px 0; line-height: 1.75; }
.article-content blockquote p:last-child { margin-bottom: 0; }
.back-link { display: inline-block; margin-bottom: 16px; font-size: 14px; color: #576b95; text-decoration: none; }
.empty { text-align: center; padding: 60px 20px; color: #999; }
.status-bar { max-width: 800px; margin: 0 auto 16px; padding: 0 20px; }
.status-bar .info { font-size: 12px; color: #999; }
.album-tabs { max-width: 800px; margin: 0 auto 12px; padding: 0 20px; display: flex; gap: 8px; flex-wrap: wrap; }
.album-tabs a { padding: 5px 14px; border-radius: 16px; font-size: 13px; color: #666; background: #fff; border: 1px solid #e8e8e8; text-decoration: none; transition: all 0.2s; }
.album-tabs a:hover { border-color: #576b95; color: #576b95; }
.album-tabs a.active { background: #576b95; color: #fff; border-color: #576b95; }
.album-table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
.album-table th { text-align: left; padding: 10px 12px; font-size: 13px; color: #999; border-bottom: 2px solid #eee; }
.album-table td { padding: 12px; font-size: 14px; border-bottom: 1px solid #f0f0f0; }
.album-table tr:hover { background: #fafafa; }
.album-form { background: #fff; border-radius: 8px; padding: 24px 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin-bottom: 20px; }
.album-form h3 { font-size: 17px; margin-bottom: 16px; color: #1a1a1a; }
.form-row { display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.form-group { flex: 1; min-width: 200px; }
.form-group label { display: block; font-size: 13px; color: #666; margin-bottom: 4px; }
.form-group input, .form-group select { width: 100%; padding: 8px 12px; border: 1px solid #d9d9d9; border-radius: 6px; font-size: 14px; }
.form-group .hint { font-size: 12px; color: #aaa; margin-top: 3px; }
.btn { padding: 8px 20px; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
.btn-primary { background: #576b95; color: #fff; }
.btn-primary:hover { background: #2f54bf; }
.btn-danger { background: #e74c3c; color: #fff; }
.btn-danger:hover { background: #c0392b; }
.btn-sm { padding: 4px 12px; font-size: 13px; }
.btn-outline { background: #fff; color: #576b95; border: 1px solid #d9d9d9; }
.btn-outline:hover { border-color: #576b95; }
.btn-outline:disabled, .btn-outline:disabled:hover { background: #f5f5f5; color: #b0b0b0; border-color: #e8e8e8; cursor: not-allowed; }
.form-actions { display: flex; gap: 8px; margin-top: 8px; }
.alert { padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }
.alert-success { background: #f0f9eb; color: #67c23a; border: 1px solid #e1f3d8; }
.alert-error { background: #fef0f0; color: #f56c6c; border: 1px solid #fde2e2; }
.album-name-cell { font-weight: 500; color: #1a1a1a; }
.mono { font-family: Consolas, "Courier New", monospace; font-size: 12px; color: #888; }
.batch-toolbar { display: flex; align-items: center; gap: 12px; padding: 10px 0; margin-bottom: 8px; }
.batch-toolbar label { font-size: 14px; color: #666; cursor: pointer; display: flex; align-items: center; gap: 4px; }
.batch-toolbar .batch-count { font-size: 13px; color: #999; }
.article-card { display: flex; align-items: flex-start; gap: 10px; }
.card-checkbox { padding-top: 4px; flex-shrink: 0; }
.card-checkbox input { width: 16px; height: 16px; cursor: pointer; }
.article-card .card-link { text-decoration: none; color: #1a1a1a; flex: 1; }

/* Toast */
.toast-container { position: fixed; top: 20px; right: 20px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }
.toast { min-width: 280px; max-width: 400px; padding: 12px 20px; border-radius: 6px; font-size: 14px; color: #fff; opacity: 0; transform: translateX(100%); transition: all 0.3s ease; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
.toast.show { opacity: 1; transform: translateX(0); }
.toast-success { background: #52c41a; }
.toast-error { background: #e74c3c; }
.toast-info { background: #576b95; }

/* Custom Confirm Modal */
.modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.4); z-index: 10000; display: none; align-items: center; justify-content: center; }
.modal-overlay.show { display: flex; }
.modal-box { background: #fff; border-radius: 8px; padding: 24px 28px; min-width: 320px; max-width: 420px; box-shadow: 0 8px 24px rgba(0,0,0,0.2); }
.modal-box .modal-msg { font-size: 15px; color: #333; line-height: 1.6; margin-bottom: 20px; }
.modal-box .modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

/* Pagination */
.pagination { display: flex; align-items: center; justify-content: center; gap: 6px; margin-top: 24px; flex-wrap: wrap; }
.pagination a, .pagination span { display: inline-flex; align-items: center; justify-content: center; min-width: 36px; height: 36px; padding: 0 10px; border-radius: 6px; font-size: 14px; text-decoration: none; border: 1px solid #e8e8e8; color: #666; background: #fff; transition: all 0.2s; }
.pagination a:hover { border-color: #576b95; color: #576b95; }
.pagination .active { background: #576b95; color: #fff; border-color: #576b95; }
.pagination .disabled { color: #ccc; background: #f9f9f9; cursor: not-allowed; }
.pagination .page-info { font-size: 13px; color: #999; margin: 0 8px; }
/* Read status dot */
.read-dot-unread { width: 8px; height: 8px; border-radius: 50%; background: #576b95; display: inline-block; flex-shrink: 0; margin-top: 3px; }
.read-dot-read { width: 8px; height: 8px; border-radius: 50%; background: #e0e0e0; display: inline-block; flex-shrink: 0; margin-top: 3px; }
.read-unread-filter { margin-bottom: 12px; }

</style>
</head>
<body>
<div class="navbar">
    <h1><a href="/">WeChat Album Reader</a></h1>
    <div class="stats">共 {{ article_count }} 篇</div>
    <div class="actions">
        <a href="/rss">RSS</a>
        <a href="/scrape">手动抓取</a>
        <a href="/albums">专辑管理</a>
    </div>
</div>
{% block content %}{% endblock %}
<div class="toast-container" id="toast-container"></div>
<div class="modal-overlay" id="confirm-modal">
    <div class="modal-box">
        <div class="modal-msg" id="confirm-msg"></div>
        <div class="modal-actions">
            <button class="btn btn-outline" id="confirm-cancel">取消</button>
            <button class="btn btn-danger" id="confirm-ok">确认</button>
        </div>
    </div>
</div>
<script>
function showToast(msg, type) {
    var container = document.getElementById('toast-container');
    var toast = document.createElement('div');
    toast.className = 'toast toast-' + (type || 'info');
    toast.textContent = msg;
    container.appendChild(toast);
    requestAnimationFrame(function() { toast.classList.add('show'); });
    setTimeout(function() {
        toast.classList.remove('show');
        setTimeout(function() { toast.remove(); }, 300);
    }, 3000);
}
function showConfirm(msg, onConfirm) {
    var modal = document.getElementById('confirm-modal');
    document.getElementById('confirm-msg').textContent = msg;
    modal.classList.add('show');
    var okBtn = document.getElementById('confirm-ok');
    var cancelBtn = document.getElementById('confirm-cancel');
    var cleanup = function() {
        modal.classList.remove('show');
        okBtn.onclick = null;
        cancelBtn.onclick = null;
    };
    okBtn.onclick = function() { cleanup(); if (onConfirm) onConfirm(); };
    cancelBtn.onclick = cleanup;
}
</script>
</body>
</html>"""


INDEX_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    """
<div class="search-bar">
    <form method="get" action="/" style="display:flex;flex:1;gap:8px;">
        <input type="text" name="q" placeholder="搜索文章标题..." value="{{ search or '' }}">
        {% if current_album %}<input type="hidden" name="album" value="{{ current_album }}">{% endif %}
        <button type="submit">搜索</button>
    </form>
</div>
{% if albums %}
<div class="album-tabs">
    <a href="/{% if unread_only %}?unread=1{% endif %}{% if search %}{% if unread_only %}&{% else %}?{% endif %}q={{ search }}{% endif %}" class="{% if not current_album %}active{% endif %}">全部{% if all_unread %}<span style="color:#e74c3c"> ({{ all_unread }})</span>{% endif %}</a>
    {% for name in albums %}
    <a href="/?album={{ name }}{% if unread_only %}&unread=1{% endif %}{% if search %}&q={{ search }}{% endif %}" class="{% if current_album == name %}active{% endif %}">{{ name }}{% if unread_counts.get(name) %}<span style="color:#e74c3c"> ({{ unread_counts.get(name) }})</span>{% endif %}</a>
    {% endfor %}
</div>
{% endif %}
<div class="status-bar">
    <span class="info">
        最后抓取: {{ status.last_run or '从未' }}
        {% if status.next_run %} | 下次抓取: {{ status.next_run }}{% endif %}
        {% if status.last_new %} | 上次新增: {{ status.last_new }} 篇{% endif %}
        {% if status.last_error %} | <span style="color:#e74c3c">错误: {{ status.last_error }}</span>{% endif %}
        {% if status.running %} | <span style="color:#e67e22">正在抓取中...</span>{% endif %}
    </span>
</div>
<div class="container">
    {% if msg %}
    <div class="alert alert-{{ msg_type }}">{{ msg }}</div>
    {% endif %}
    {% if articles %}
    <form method="post" action="/articles/mark-all-read" id="mark-all-form">
        <input type="hidden" name="q" value="{{ search or '' }}">
        <input type="hidden" name="album" value="{{ current_album or '' }}">
    </form>
    <div class="read-unread-filter" style="display:flex;gap:8px;align-items:center;">
        <a href="/?album={{ current_album or '' }}&{% if unread_only %}q={{ search or '' }}{% else %}unread=1{% if search %}&q={{ search or '' }}{% endif %}{% endif %}" class="btn btn-outline btn-sm">
            {{ '显示全部' if unread_only else '仅看未读' }}
        </a>
        {% if (all_unread if not current_album else unread_counts.get(current_album, 0)) > 0 %}
        <button type="button" class="btn btn-primary btn-sm" onclick="confirmMarkAll()">标记全部已读</button>
        {% endif %}
        {% if current_album %}
        <a href="/export/zip/album?album={{ current_album }}" class="btn btn-outline btn-sm" id="export-album-btn" onclick="var btn=this;btn.textContent='正在打包...';btn.style.pointerEvents='none';btn.style.opacity='0.6';setTimeout(function(){btn.textContent='导出该专辑ZIP';btn.style.pointerEvents='';btn.style.opacity='1';},5000);">导出该专辑ZIP</a>
        {% endif %}
        {% if unread_only %}
        <span class="batch-count">仅显示 {{ article_count }} 篇未读</span>
        {% endif %}
    </div>
    <form method="post" action="/articles/delete-batch" id="batch-form">
        <input type="hidden" name="q" value="{{ search or '' }}">
        <input type="hidden" name="album" value="{{ current_album or '' }}">
        <div class="batch-toolbar">
            <label><input type="checkbox" id="select-all" onchange="toggleAll(this)"> 全选</label>
            <button type="button" class="btn btn-danger btn-sm" onclick="confirmBatchDelete()">批量删除</button>
            <button type="button" class="btn btn-primary btn-sm" onclick="batchExport('zip')">导出 ZIP</button>
            <button type="button" class="btn btn-primary btn-sm" onclick="batchExport('markdown')">导出 Markdown</button>
            <span class="batch-count" id="batch-count"></span>
        </div>
        {% for a in articles %}
        <div class="article-card">
            <div class="{{ 'read-dot-unread' if not a.is_read else 'read-dot-read' }}"></div>
            <div class="card-checkbox"><input type="checkbox" name="article_ids" value="{{ a.id }}" class="article-checkbox" onchange="updateCount()"></div>
            <a href="/article/{{ a.id }}" class="card-link">
                <h2>{{ a.title }}</h2>
                <div class="meta">{{ a.publish_time }} · {{ a.author }}{% if a.album_name %} · {{ a.album_name }}{% endif %}</div>
            </a>
        </div>
        {% endfor %}
    </form>
    {% if total_pages > 1 %}
    <div class="pagination">
        {% set page_qs = 'page=' %}
        {% if search %}{% set page_qs = 'q=' ~ (search | urlencode) ~ '&amp;' ~ page_qs %}{% endif %}
        {% if current_album %}{% set page_qs = 'album=' ~ (current_album | urlencode) ~ '&amp;' ~ page_qs %}{% endif %}
        {% set page_qs = '?' ~ page_qs %}
        {% if page > 1 %}
        <a href="/{{ page_qs }}{{ page - 1 }}">&laquo; 上一页</a>
        {% else %}
        <span class="disabled">&laquo; 上一页</span>
        {% endif %}
        {% for p in range(1, total_pages + 1) %}
            {% if p == page %}
            <span class="active">{{ p }}</span>
            {% elif p == 1 or p == total_pages or (p >= page - 2 and p <= page + 2) %}
            <a href="/{{ page_qs }}{{ p }}">{{ p }}</a>
            {% elif p == 2 or p == total_pages - 1 %}
            <span class="disabled">...</span>
            {% endif %}
        {% endfor %}
        {% if page < total_pages %}
        <a href="/{{ page_qs }}{{ page + 1 }}">下一页 &raquo;</a>
        {% else %}
        <span class="disabled">下一页 &raquo;</span>
        {% endif %}
        <span class="page-info">{{ page }} / {{ total_pages }} 页</span>
    </div>
    {% endif %}
    {% else %}
        <div class="empty">
            <p>暂无文章</p>
            <p style="margin-top:12px"><a href="/scrape" style="color:#576b95">点击手动抓取</a></p>
        </div>
    {% endif %}
</div>
<script>
function toggleAll(src) {
    document.querySelectorAll('.article-checkbox').forEach(cb => cb.checked = src.checked);
    updateCount();
}
function updateCount() {
    var n = document.querySelectorAll('.article-checkbox:checked').length;
    var el = document.getElementById('batch-count');
    el.textContent = n > 0 ? '已选 ' + n + ' 篇' : '';
}
function confirmBatchDelete() {
    var n = document.querySelectorAll('.article-checkbox:checked').length;
    if (n === 0) { showToast('请先选择要删除的文章', 'error'); return; }
    showConfirm('确认删除选中的 ' + n + ' 篇文章？此操作不可撤销。', function() {
        document.getElementById('batch-form').submit();
    });
}
function batchExport(format) {
    var n = document.querySelectorAll('.article-checkbox:checked').length;
    if (n === 0) { showToast('请先选择要导出的文章', 'error'); return; }
    var form = document.getElementById('batch-form');
    form.action = '/export/' + format;
    form.submit();
    setTimeout(function() { form.action = '/articles/delete-batch'; }, 100);
}
function confirmMarkAll() {
    showConfirm('确认将当前列表中的所有文章标记为已读？', function() {
        document.getElementById('mark-all-form').submit();
    });
}
</script>
""",
)


ARTICLE_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    """
<div class="container">
    <a href="/" class="back-link">&larr; 返回列表</a>
    <div class="article-detail">
        <h1>{{ article.title }}</h1>
        <div class="meta">
            <span>Author: {{ article.author }}</span>
            <span>Published: {{ article.publish_time }}</span>
            <span>Saved: {{ article.saved_at }}</span>
            {% if article.album_name %}<span>Album: {{ article.album_name }}</span>{% endif %}
            {% if article.file_path %}
            <span style="margin-left:auto;">
                <a href="/article/{{ article.id }}/html" style="color:#576b95;text-decoration:underline;">查看HTML文件</a>
                &nbsp;|&nbsp;
                <a href="/article/{{ article.id }}/download" style="color:#576b95;text-decoration:underline;">下载</a>
            </span>
            {% endif %}
        </div>
        <div class="article-content">
            {{ article.content_html | safe }}
        </div>
    </div>
</div>
""",
)


ALBUM_MANAGE_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    """
<div class="container">
    <a href="/" class="back-link">&larr; 返回首页</a>
    {% if msg %}
    <div class="alert alert-{{ msg_type }}">{{ msg }}</div>
    {% endif %}
    <div class="album-form">
        <h3 id="form-title">添加专辑</h3>
        <form id="album-form" method="post" action="/albums/add">
            <input type="hidden" name="edit_index" id="edit_index" value="">
            <div class="form-row">
                <div class="form-group" style="flex:3;">
                    <label>微信链接（粘贴专辑或文章链接，自动提取参数）</label>
                    <input type="text" id="url_input" placeholder="https://mp.weixin.qq.com/mp/appmsgalbum?__biz=...&album_id=...">
                    <div class="hint">支持专辑链接（含__biz和album_id）或文章短链接</div>
                </div>
                <div class="form-group" style="flex:0 0 auto;">
                    <label style="visibility:hidden;">.</label>
                    <div style="display:flex; align-items:center; gap:8px;">
                        <button type="button" class="btn btn-outline" id="parse-btn" onclick="parseUrl()" disabled>解析链接</button>
                        <span id="parse-tip" style="color:#e74c3c; font-size:12px;"></span>
                    </div>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>专辑名称（留空则自动获取）</label>
                    <input type="text" name="name" id="f_name" placeholder="自动从API获取">
                </div>
                <div class="form-group">
                    <label>__biz</label>
                    <input type="text" name="biz" id="f_biz" placeholder="如 Mzg4MzY5NzE3MQ==" required>
                </div>
                <div class="form-group">
                    <label>album_id</label>
                    <input type="text" name="album_id" id="f_album_id" placeholder="如 4475164850729730048" required>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group" style="max-width:120px;">
                    <label>抓取数量</label>
                    <input type="number" name="count" id="f_count" value="20" min="1" max="100">
                </div>
                <div class="form-group" style="max-width:200px;">
                    <label>排序方式 (is_reverse)</label>
                    <select name="is_reverse" id="f_is_reverse">
                        <option value="1">倒序（最新优先）</option>
                        <option value="0">正序（最早优先）</option>
                    </select>
                    <div class="hint">不同专辑默认排序不同，若抓取非最新文章请切换</div>
                </div>
            </div>
            <div class="form-actions">
                <button type="submit" class="btn btn-primary" id="submit-btn">添加</button>
                <button type="button" class="btn btn-outline" id="cancel-btn" style="display:none;" onclick="resetForm()">取消编辑</button>
            </div>
        </form>
    </div>

    <div class="album-form">
        <h3>已配置专辑（{{ albums|length }}）</h3>
        {% if albums %}
        <table class="album-table">
            <thead>
                <tr>
                    <th>名称</th>
                    <th>__biz</th>
                    <th>album_id</th>
                    <th>排序</th>
                    <th>数量</th>
                    <th>已抓取</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
                {% for a in albums %}
                <tr>
                    <td class="album-name-cell">{{ a.name }}</td>
                    <td class="mono">{{ a.biz }}</td>
                    <td class="mono">{{ a.album_id }}</td>
                    <td>{{ '倒序' if a.is_reverse == 1 else '正序' }}</td>
                    <td>{{ a.count }}</td>
                    <td>{{ album_counts[loop.index0] }} 篇</td>
                    <td>
                        <button class="btn btn-outline btn-sm" onclick="editAlbum({{ loop.index0 }}, '{{ a.name|e }}', '{{ a.biz }}', '{{ a.album_id }}', {{ a.count }}, {{ a.is_reverse }})">编辑</button>
                        <button type="button" class="btn btn-danger btn-sm" onclick="confirmDeleteAlbum({{ loop.index0 }}, '{{ a.name|e }}', {{ album_counts[loop.index0] }})">删除</button>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color:#999;padding:20px 0;">暂无配置的专辑，请在上方添加。</p>
        {% endif %}
    </div>
</div>

<script>
document.getElementById('url_input').addEventListener('input', function() {
    var btn = document.getElementById('parse-btn');
    btn.disabled = !this.value.trim();
});

function showParseTip(msg, color) {
    var tip = document.getElementById('parse-tip');
    if (tip) { tip.textContent = msg; tip.style.color = color || '#e74c3c'; tip.style.display = 'inline'; }
}
function hideParseTip() {
    var tip = document.getElementById('parse-tip');
    if (tip) { tip.style.display = 'none'; }
}
function parseUrl() {
    var url = document.getElementById('url_input').value.trim();
    if (!url) {
        showParseTip('请先粘贴微信链接');
        return;
    }
    hideParseTip();
    fetch('/albums/parse-url', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: 'url=' + encodeURIComponent(url)
    })
    .then(r => r.json())
    .then(data => {
        if (data.biz) document.getElementById('f_biz').value = data.biz;
        if (data.album_id) document.getElementById('f_album_id').value = data.album_id;
        if (data.album_name) document.getElementById('f_name').value = data.album_name;
        if (data.article_count) {
            showParseTip('解析成功：' + (data.album_name || '未知') + '（' + data.article_count + ' 篇），已自动填入下方', '#52c41a');
        } else if (data.biz && !data.album_id) {
            showParseTip('已提取 __biz，未找到 album_id，请手动输入');
        } else if (data.biz) {
            showParseTip('已提取 __biz，请继续填写', '#52c41a');
        }
    })
    .catch(err => showParseTip('解析失败：' + err));
}

function editAlbum(index, name, biz, albumId, count, isReverse) {
    document.getElementById('form-title').textContent = '编辑专辑';
    document.getElementById('edit_index').value = index;
    document.getElementById('f_name').value = name;
    document.getElementById('f_biz').value = biz;
    document.getElementById('f_album_id').value = albumId;
    document.getElementById('f_count').value = count;
    document.getElementById('f_is_reverse').value = isReverse;
    document.getElementById('submit-btn').textContent = '保存';
    document.getElementById('cancel-btn').style.display = '';
    document.getElementById('album-form').action = '/albums/edit/' + index;
    document.getElementById('form-title').scrollIntoView({behavior: 'smooth'});
}

function resetForm() {
    document.getElementById('form-title').textContent = '添加专辑';
    document.getElementById('edit_index').value = '';
    document.getElementById('f_name').value = '';
    document.getElementById('f_biz').value = '';
    document.getElementById('f_album_id').value = '';
    document.getElementById('f_count').value = '20';
    document.getElementById('f_is_reverse').value = '1';
    document.getElementById('submit-btn').textContent = '添加';
    document.getElementById('cancel-btn').style.display = 'none';
    document.getElementById('album-form').action = '/albums/add';
    document.getElementById('url_input').value = '';
    document.getElementById('parse-btn').disabled = true;
    hideParseTip();
}

function confirmDeleteAlbum(index, name, count) {
    var modal = document.getElementById('confirm-modal');
    var msgEl = document.getElementById('confirm-msg');
    var esc = document.createElement('div');
    esc.textContent = name;
    var safeName = esc.innerHTML;
    msgEl.innerHTML =
        '<p style="margin:0 0 12px;font-weight:500;">确认删除专辑「' + safeName + '」？</p>' +
        '<label style="display:block;margin-bottom:8px;cursor:pointer;">' +
            '<input type="radio" name="del-opt" value="config" checked style="margin-right:6px;vertical-align:middle;">' +
            '<span style="vertical-align:middle;">仅删除配置（保留 ' + count + ' 篇已抓取文章）</span>' +
        '</label>' +
        '<label style="display:block;cursor:pointer;">' +
            '<input type="radio" name="del-opt" value="all" style="margin-right:6px;vertical-align:middle;">' +
            '<span style="vertical-align:middle;color:#e74c3c;">删除配置及所有文章（' + count + ' 篇将永久删除）</span>' +
        '</label>';
    modal.classList.add('show');
    var okBtn = document.getElementById('confirm-ok');
    var cancelBtn = document.getElementById('confirm-cancel');
    var cleanup = function() {
        modal.classList.remove('show');
        okBtn.onclick = null;
        cancelBtn.onclick = null;
    };
    okBtn.onclick = function() {
        var selected = document.querySelector('input[name="del-opt"]:checked');
        var choice = selected ? selected.value : 'config';
        cleanup();
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = '/albums/delete/' + index;
        if (choice === 'all') {
            var hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = 'delete_articles';
            hidden.value = '1';
            form.appendChild(hidden);
        }
        document.body.appendChild(form);
        form.submit();
    };
    cancelBtn.onclick = cleanup;
}
</script>
""",
)


PER_PAGE = 20

@app.route("/")
def index():
    search = request.args.get("q", "").strip()
    album = request.args.get("album", "").strip() or None
    unread_only = request.args.get("unread", "").strip() == "1"
    page = max(1, request.args.get("page", 1, type=int))
    total = count_articles(album=album, search=search, unread_only=unread_only)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * PER_PAGE
    articles = get_articles(
        search=search, album=album, limit=PER_PAGE, offset=offset, unread_only=unread_only
    )
    return render_template_string(
        INDEX_TEMPLATE,
        articles=articles,
        search=search,
        current_album=album,
        unread_only=unread_only,
        albums=get_all_album_names(),
        unread_counts=get_unread_counts(),
        all_unread=count_articles(unread_only=True),
        status=scrape_status,
        article_count=total,
        page=page,
        total_pages=total_pages,
        msg=request.args.get("msg", ""),
        msg_type=request.args.get("type", ""),
    )


@app.route("/article/<int:article_id>")
def article_detail(article_id):
    article = get_article(article_id)
    if not article:
        return "Article not found", 404
    # Mark article as read when viewed
    mark_article_read(article_id)
    return render_template_string(
        ARTICLE_TEMPLATE, article=article, article_count=count_articles()
    )


@app.route("/article/<int:article_id>/html")
def article_html(article_id):
    """Serve the standalone HTML file for the article."""
    article = get_article(article_id)
    if not article:
        return "Article not found", 404
    filename = article["file_path"]
    if not filename:
        return "HTML file not saved", 404
    file_path = os.path.join(ARTICLES_DIR, filename)
    if not os.path.exists(file_path):
        return "HTML file not found on disk", 404
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content, mimetype="text/html")


@app.route("/article/<int:article_id>/download")
def article_download(article_id):
    """Download the standalone HTML file."""
    from flask import send_file
    article = get_article(article_id)
    if not article:
        return "Article not found", 404
    filename = article["file_path"]
    if not filename:
        return "HTML file not saved", 404
    file_path = os.path.join(ARTICLES_DIR, filename)
    if not os.path.exists(file_path):
        return "HTML file not found on disk", 404
    return send_file(file_path, as_attachment=True, download_name=filename)


@app.route("/scrape")
def manual_scrape():
    import threading
    t = threading.Thread(target=run_scrape, kwargs={"max_new": 10})
    t.daemon = True
    t.start()
    return redirect(url_for("index"))


@app.route("/articles/delete-batch", methods=["POST"])
def article_delete_batch():
    ids = request.form.getlist("article_ids")
    deleted = 0
    for aid in ids:
        try:
            if delete_article(int(aid)):
                deleted += 1
        except Exception:
            pass
    msg = f"已删除 {deleted} 篇文章" if deleted else "未选择文章或删除失败"
    return redirect(url_for("index", q=request.form.get("q", ""), album=request.form.get("album", ""), msg=msg, type="success" if deleted else "error"))


@app.route("/articles/mark-all-read", methods=["POST"])
def article_mark_all_read():
    album = request.form.get("album", "").strip() or None
    updated = mark_all_read(album=album)
    msg = f"已将 {updated} 篇标记为已读" if updated else "没有未读文章"
    return redirect(
        url_for(
            "index",
            q=request.form.get("q", ""),
            album=album,
            msg=msg,
            type="success" if updated else "info",
        )
    )


@app.route("/rss")
def rss_feed():
    album = request.args.get("album", "").strip() or None
    articles = get_articles(album=album, limit=50)
    items = []
    for a in articles:
        pub_date = ""
        if a["create_time"]:
            try:
                dt = datetime.fromtimestamp(a["create_time"], tz=timezone.utc)
                pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except Exception:
                pass
        items.append(
            f"""<item>
<title>{a['title']}</title>
<link>{a['url']}</link>
<description>{a['summary'] or a['title']}</description>
<pubDate>{pub_date}</pubDate>
<guid>{a['url']}</guid>
</item>"""
        )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>WeChat Album Reader</title>
<link>http://localhost:5000</link>
<description>WeChat public account album articles</description>
<language>zh-CN</language>
{''.join(items)}
</channel>
</rss>"""
    return Response(xml, mimetype="application/rss+xml")


# ---------------------------------------------------------------------------
# Export helpers & routes
# ---------------------------------------------------------------------------

def _generate_export_zip(articles, fmt, suffix):
    """Generate a ZIP buffer for a list of articles.

    Args:
        articles: list of sqlite3.Row objects
        fmt: 'html' or 'markdown'
        suffix: string appended before .zip (e.g. '_markdown' or '')

    Returns:
        (io.BytesIO, download_filename) tuple
    """
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for article in articles:
            try:
                if fmt == "html":
                    filename = article["file_path"]
                    if filename:
                        file_path = os.path.join(ARTICLES_DIR, filename)
                        if os.path.exists(file_path):
                            zf.write(file_path, f"articles/{filename}")
                    else:
                        title = article["title"] or "untitled"
                        safe_name = _safe_filename(title, article["id"])
                        content = article["content_html"] or ""
                        html = (
                            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                            f"<title>{title}</title></head><body>{content}</body></html>"
                        )
                        zf.writestr(f"articles/{safe_name}", html.encode("utf-8"))
                else:  # markdown
                    md_content = html_to_markdown(
                        article["content_html"] or "",
                        article["title"] or "untitled",
                        article["author"],
                        article["publish_time"],
                        article["url"],
                    )
                    safe_name = re.sub(r'[\\/:*?"<>|]', "_", article["title"] or "untitled")
                    safe_name = safe_name.strip().strip(".")
                    if not safe_name:
                        safe_name = f"article_{article['id']}"
                    if len(safe_name) > 80:
                        safe_name = safe_name[:80]
                    zf.writestr(f"articles/{safe_name}.md", md_content.encode("utf-8"))
            except Exception:
                pass

    zip_buffer.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    download_name = f"articles_export_{timestamp}{suffix}.zip"
    return zip_buffer, download_name


@app.route("/export/zip", methods=["POST"])
def export_zip():
    """Export selected articles as a ZIP file containing standalone HTML files."""
    ids = request.form.getlist("article_ids")
    if not ids:
        return redirect(url_for("index", msg="请先选择要导出的文章", type="error"))
    articles = []
    for aid in ids:
        try:
            a = get_article(int(aid))
            if a:
                articles.append(a)
        except Exception:
            pass
    zip_buffer, dl_name = _generate_export_zip(articles, "html", "")
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name=dl_name)


@app.route("/export/markdown", methods=["POST"])
def export_markdown():
    """Export selected articles as Markdown files in a ZIP archive."""
    ids = request.form.getlist("article_ids")
    if not ids:
        return redirect(url_for("index", msg="请先选择要导出的文章", type="error"))
    articles = []
    for aid in ids:
        try:
            a = get_article(int(aid))
            if a:
                articles.append(a)
        except Exception:
            pass
    zip_buffer, dl_name = _generate_export_zip(articles, "markdown", "_markdown")
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name=dl_name)


@app.route("/export/zip/album")
def export_zip_album():
    """Export all articles in a specific album as ZIP (HTML format)."""
    album_name = request.args.get("album", "").strip()
    if not album_name:
        return redirect(url_for("index", msg="请指定专辑名称", type="error"))
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM articles WHERE album_name = ? ORDER BY create_time DESC",
        (album_name,),
    ).fetchall()
    conn.close()
    safe = re.sub(r'[\\/:*?"<>|]', "_", album_name)
    zip_buffer, dl_name = _generate_export_zip(rows, "html", f"_{safe}")
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name=dl_name)


# ---------------------------------------------------------------------------
# Album management routes
# ---------------------------------------------------------------------------

def _get_album_counts(albums):
    """Get article count for each album from database."""
    conn = get_db()
    counts = []
    for a in albums:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM articles WHERE album_name = ?", (a["name"],)
        ).fetchone()
        counts.append(row["n"])
    conn.close()
    return counts


@app.route("/albums")
def album_manage():
    albums = load_config()
    album_counts = _get_album_counts(albums)
    msg = request.args.get("msg", "")
    msg_type = request.args.get("type", "")
    return render_template_string(
        ALBUM_MANAGE_TEMPLATE,
        albums=albums,
        album_counts=album_counts,
        article_count=count_articles(),
        msg=msg,
        msg_type=msg_type,
    )


@app.route("/albums/add", methods=["POST"])
def album_add():
    name = request.form.get("name", "").strip()
    biz = request.form.get("biz", "").strip()
    album_id = request.form.get("album_id", "").strip()
    count = int(request.form.get("count", 20))
    is_reverse = int(request.form.get("is_reverse", 1))

    if not biz or not album_id:
        return redirect(url_for("album_manage", msg="biz 和 album_id 不能为空", type="error"))

    # Auto-fetch album name from API if not provided
    if not name:
        try:
            _, album_info = scraper.fetch_album_list(biz, album_id, count=1, is_reverse=is_reverse)
            name = album_info.get("title", "") or f"专辑_{album_id[:8]}"
        except Exception:
            name = f"专辑_{album_id[:8]}"

    albums = load_config()

    # Check for duplicates
    for a in albums:
        if a["biz"] == biz and a["album_id"] == album_id:
            return redirect(url_for("album_manage", msg=f"专辑已存在：{a['name']}", type="error"))

    albums.append({
        "name": name,
        "biz": biz,
        "album_id": album_id,
        "count": count,
        "is_reverse": is_reverse,
    })
    save_config(albums)
    print(f"[config] Added album: {name}")
    return redirect(url_for("album_manage", msg=f"已添加专辑：{name}", type="success"))


@app.route("/albums/edit/<int:index>", methods=["POST"])
def album_edit(index):
    albums = load_config()
    if index < 0 or index >= len(albums):
        return redirect(url_for("album_manage", msg="无效的专辑索引", type="error"))

    old_name = albums[index]["name"]
    name = request.form.get("name", "").strip()
    biz = request.form.get("biz", "").strip()
    album_id = request.form.get("album_id", "").strip()
    count = int(request.form.get("count", 20))
    is_reverse = int(request.form.get("is_reverse", 1))

    if not biz or not album_id:
        return redirect(url_for("album_manage", msg="biz 和 album_id 不能为空", type="error"))

    # Auto-fetch name if empty
    if not name:
        try:
            _, album_info = scraper.fetch_album_list(biz, album_id, count=1, is_reverse=is_reverse)
            name = album_info.get("title", "") or f"专辑_{album_id[:8]}"
        except Exception:
            name = f"专辑_{album_id[:8]}"

    albums[index] = {
        "name": name,
        "biz": biz,
        "album_id": album_id,
        "count": count,
        "is_reverse": is_reverse,
    }
    save_config(albums)

    # Update album_name in database if name changed
    if name != old_name:
        conn = get_db()
        conn.execute("UPDATE articles SET album_name = ? WHERE album_name = ?", (name, old_name))
        conn.commit()
        conn.close()
        print(f"[config] Renamed album in DB: '{old_name}' -> '{name}'")

    print(f"[config] Updated album at index {index}: {name}")
    return redirect(url_for("album_manage", msg=f"已更新专辑：{name}", type="success"))


@app.route("/albums/delete/<int:index>", methods=["POST"])
def album_delete(index):
    albums = load_config()
    if index < 0 or index >= len(albums):
        return redirect(url_for("album_manage", msg="无效的专辑索引", type="error"))

    removed = albums.pop(index)
    save_config(albums)

    delete_articles_flag = request.form.get("delete_articles", "") == "1"
    if delete_articles_flag:
        conn = get_db()
        rows = conn.execute(
            "SELECT file_path FROM articles WHERE album_name = ?", (removed["name"],)
        ).fetchall()
        for row in rows:
            if row["file_path"]:
                fp = os.path.join(ARTICLES_DIR, row["file_path"])
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
        # Delete articles from FTS first, then from main table
        ids = conn.execute(
            "SELECT id FROM articles WHERE album_name = ?", (removed["name"],)
        ).fetchall()
        id_list = [r["id"] for r in ids]
        for aid in id_list:
            conn.execute("DELETE FROM articles_fts WHERE rowid = ?", (aid,))
        cur = conn.execute(
            "DELETE FROM articles WHERE album_name = ?", (removed["name"],)
        )
        deleted_count = cur.rowcount
        conn.commit()
        conn.close()
        print(f"[config] Deleted album: {removed['name']} (with {deleted_count} articles)")
        return redirect(url_for("album_manage", msg=f"已删除专辑「{removed['name']}」及 {deleted_count} 篇文章", type="success"))
    else:
        print(f"[config] Deleted album: {removed['name']} (articles kept)")
        return redirect(url_for("album_manage", msg=f"已删除专辑：{removed['name']}（已抓取的文章保留）", type="success"))


@app.route("/albums/parse-url", methods=["POST"])
def album_parse_url():
    """AJAX endpoint: parse a WeChat URL and return biz, album_id, and album name."""
    url = request.form.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL不能为空"})

    biz, album_id = parse_album_url(url)
    result = {"biz": biz, "album_id": album_id, "album_name": "", "article_count": ""}

    # If we have both biz and album_id, try to fetch album info
    if biz and album_id:
        try:
            # Try is_reverse=1 first, then 0
            for rev in [1, 0]:
                try:
                    _, album_info = scraper.fetch_album_list(biz, album_id, count=1, is_reverse=rev)
                    if album_info.get("title"):
                        result["album_name"] = album_info["title"]
                        result["article_count"] = album_info.get("article_count", "")
                        result["suggested_is_reverse"] = rev
                        break
                except Exception:
                    continue
        except Exception:
            pass

    return jsonify(result)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def start_scheduler():
    scrape_status["next_run"] = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_scrape,
        IntervalTrigger(minutes=10),
        id="scrape_job",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    scheduler = start_scheduler()
    print("=" * 50)
    print("  WeChat Album Reader")
    print("  http://localhost:5000")
    print("  Schedule: every 10 minutes")
    print("=" * 50)
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
