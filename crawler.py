"""中島健人さんの国内・海外ニュースを集めるクローラー。

ニュース配信用の RSS（Google ニュース検索）を日本・韓国・英語圏の3地域で読み、
見出し・配信元・日時・リンクだけを保存する（記事本文はコピーしない）。
海外の見出しは translate.py で日本語にする。

使い方:
    python3 crawler.py
"""
import sqlite3
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import translate

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
USER_AGENT = "kento-news-crawler/0.2 (personal use)"

# (言語, 検索語, hl, gl, ceid)
SOURCES = [
    ("ja", "中島健人", "ja", "JP", "JP:ja"),
    ("ko", "나카지마 켄토", "ko", "KR", "KR:ko"),
    ("en", "Kento Nakajima", "en-US", "US", "US:en"),
]


def feed_url(query: str, hl: str, gl: str, ceid: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read()


def parse_rss(xml_bytes: bytes, lang: str = "ja") -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub).isoformat() if pub else ""
        except (TypeError, ValueError):
            published = ""
        # Google ニュースの見出しは「タイトル - 配信元」形式なので配信元を外す
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        if title and link:
            items.append({
                "title": title, "link": link, "source": source,
                "published": published, "lang": lang,
                "title_ja": title if lang == "ja" else None,
            })
    return items


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS articles (
            link TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            title_ja TEXT,
            lang TEXT NOT NULL DEFAULT 'ja',
            source TEXT,
            published TEXT,
            fetched_at TEXT DEFAULT (datetime('now', 'localtime'))
        )"""
    )
    # 会員数まわりの数値。metric は official（公式発表）/ member_no（ファン投稿の会員番号）
    # / youtube（登録者数）/ spotify（フォロワー数）
    conn.execute(
        """CREATE TABLE IF NOT EXISTS metrics (
            date TEXT NOT NULL,
            grp TEXT NOT NULL,
            metric TEXT NOT NULL,
            value INTEGER NOT NULL,
            source TEXT,
            PRIMARY KEY (date, grp, metric)
        )"""
    )
    # 前回の状態（Wikipedia の内容、確認済みニュース、チャンネル ID など）
    conn.execute("CREATE TABLE IF NOT EXISTS watch_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("DROP TABLE IF EXISTS fanclub_counts")
    return conn


def save(conn: sqlite3.Connection, items: list[dict]) -> int:
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO articles (link, title, title_ja, lang, source, published) "
        "VALUES (:link, :title, :title_ja, :lang, :source, :published)",
        items,
    )
    conn.commit()
    return conn.total_changes - before


def translate_pending(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT link, title FROM articles WHERE title_ja IS NULL ORDER BY published DESC LIMIT 300"
    ).fetchall()
    if not rows:
        return 0
    translated = translate.to_japanese([title for _, title in rows])
    if not translated:
        return 0
    conn.executemany(
        "UPDATE articles SET title_ja = ? WHERE link = ?",
        [(ja, link) for (link, _), ja in zip(rows, translated)],
    )
    conn.commit()
    return len(translated)


def main() -> int:
    conn = open_db()
    total = 0
    for lang, query, hl, gl, ceid in SOURCES:
        try:
            items = parse_rss(fetch(feed_url(query, hl, gl, ceid)), lang)
        except Exception as e:  # 1地域が失敗しても他は続ける
            print(f"[{lang}] 取得に失敗しました: {e}", file=sys.stderr)
            continue
        added = save(conn, items)
        total += len(items)
        print(f"[{lang}] 取得 {len(items)} 件 / 新規 {added} 件")
    print(f"翻訳 {translate_pending(conn)} 件")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
