"""中島健人さんの国内・海外ニュースを集めるクローラー。

ニュース配信用の RSS（Google ニュース検索・Bing ニュース検索）を各国の版で読み、
見出し・配信元・日時・リンク・サムネイル画像の URL だけを保存する（記事本文はコピーしない）。
海外の見出しは translate.py で日本語にし、サムネイルは thumbnails.py で補う。

使い方:
    python3 crawler.py
"""
import re
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
# 機械からのアクセスであることと連絡先を名乗る、一般的なクローラーの書き方（ブラウザのふりはしない）
USER_AGENT = "Mozilla/5.0 (compatible; kento-news/1.0; +https://o8066755008-arch.github.io/kento-news/)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.8",
}

# 名前の表記ゆれ。英語表記は姓名の順が両方使われる
EN_QUERY = '"Kento Nakajima" OR "Nakajima Kento"'

# Google ニュース: (言語, 検索語, hl, gl, ceid)
SOURCES = [
    ("ja", "中島健人", "ja", "JP", "JP:ja"),
    # 公式 SNS（Instagram・X・TikTok）の投稿は、規約上アプリから直接は集められないので、投稿を取り上げた記事で拾う
    ("ja", "中島健人 (インスタ OR Instagram OR インスタグラム OR TikTok OR SNS OR ポスト OR 投稿 OR 更新)", "ja", "JP", "JP:ja"),
    ("ko", "나카지마 켄토", "ko", "KR", "KR:ko"),
    ("en", EN_QUERY, "en-US", "US", "US:en"),
    ("en", EN_QUERY, "en-SG", "SG", "SG:en"),
    ("en", EN_QUERY, "en-PH", "PH", "PH:en"),
    ("zh", "中島健人 OR 中岛健人", "zh-TW", "TW", "TW:zh-Hant"),
    ("zh", "中島健人 OR 中岛健人", "zh-HK", "HK", "HK:zh-Hant"),
    ("th", f"นากาจิมะ เคนโตะ OR {EN_QUERY}", "th", "TH", "TH:th"),
    ("vi", EN_QUERY, "vi", "VN", "VN:vi"),
    ("id", EN_QUERY, "id", "ID", "ID:id"),
    ("es", EN_QUERY, "es-419", "MX", "MX:es-419"),
    ("pt", EN_QUERY, "pt-BR", "BR", "BR:pt-419"),
    ("fr", EN_QUERY, "fr", "FR", "FR:fr"),
]

# Bing ニュース: (言語, 検索語, market)。リンクが記事に直接つながり、サムネイルも付く
BING_SOURCES = [
    ("ja", "中島健人", "ja-JP"),
    ("ko", "나카지마 켄토", "ko-KR"),
    ("en", "Kento Nakajima", "en-US"),  # Bing は OR と引用符の組み合わせで0件になるため
    ("zh", "中島健人", "zh-TW"),
    ("th", "Kento Nakajima", "th-TH"),
]


def feed_url(query: str, hl: str, gl: str, ceid: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
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


def bing_url(query: str, market: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://www.bing.com/news/search?q={q}&format=rss&setmkt={market}&setlang={market}"


def parse_bing_rss(xml_bytes: bytes, lang: str) -> list[dict]:
    # Bing の RSS は名前空間の URL に「&」がそのまま入ることがあり、XML として壊れているので直す
    xml_bytes = re.sub(rb"&(?!#?\w+;)", b"&amp;", xml_bytes)
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        extra = {el.tag.split("}")[-1]: (el.text or "").strip() for el in item}
        title = extra.get("title", "")
        link = extra.get("link", "")
        # Bing のリンクはクリック計測用。url= の部分が記事の URL
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        try:
            published = parsedate_to_datetime(extra["pubDate"]).isoformat() if extra.get("pubDate") else ""
        except (TypeError, ValueError):
            published = ""
        if title and real.startswith("http"):
            items.append({
                "title": title, "link": real, "source": extra.get("Source", ""),
                "published": published, "lang": lang,
                "title_ja": title if lang == "ja" else None,
                "image": re.sub(r"^http://", "https://", extra.get("Image", "")) or None,
            })
    return items


TRUNCATED = re.compile(r"\s*(\.\.\.|…)\s*$")


def title_key(title: str) -> str:
    """同じ記事が Google と Bing の両方から来たときに見分けるための見出しの正規化。

    Bing は長い見出しを「…」で省略するので、省略記号は外しておき、前方一致で比べる。
    """
    return re.sub(r"[\s\W_]+", "", TRUNCATED.sub("", title)).lower()[:60]


MIN_PREFIX = 15  # これより短い見出しは前方一致では同じ記事とみなさない


def find_same(conn, link: str, key: str):
    """同じ記事（リンクが同じ、または見出しが同じ・片方が省略された見出し）を探す。"""
    return conn.execute(
        "SELECT link, image FROM articles WHERE link = ? OR title_key = ? "
        "OR (length(?) >= ? AND title_key LIKE ? || '%') "
        "OR (length(title_key) >= ? AND ? LIKE title_key || '%') LIMIT 1",
        (link, key, key, MIN_PREFIX, key, MIN_PREFIX, key),
    ).fetchone()


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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    for col, decl in [("image", "TEXT"), ("title_key", "TEXT"), ("image_tried", "INTEGER DEFAULT 0")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {decl}")
    missing = conn.execute("SELECT link, title FROM articles WHERE title_key IS NULL "
                           "OR title LIKE '%...' OR title LIKE '%…'").fetchall()
    conn.executemany("UPDATE articles SET title_key = ? WHERE link = ?",
                     [(title_key(t), link) for link, t in missing])
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
    # 前回の状態（Wikipedia の内容、確認済みニュース、チャンネル ID など）。読み取り方を変えたときはキーの版を上げて読み直す
    conn.execute("CREATE TABLE IF NOT EXISTS watch_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("DROP TABLE IF EXISTS fanclub_counts")
    return conn


def save(conn: sqlite3.Connection, items: list[dict]) -> int:
    """新しい記事を保存する。同じ見出しの記事がすでにあれば、画像だけ補って保存しない。"""
    added = 0
    for it in items:
        key = title_key(it["title"])
        row = find_same(conn, it["link"], key)
        if row:
            if it.get("image") and not row[1]:
                conn.execute("UPDATE articles SET image = ? WHERE link = ?", (it["image"], row[0]))
            continue
        conn.execute(
            "INSERT INTO articles (link, title, title_ja, lang, source, published, image, title_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (it["link"], it["title"], it["title_ja"], it["lang"], it["source"], it["published"],
             it.get("image"), key),
        )
        added += 1
    conn.commit()
    return added


def translate_pending(conn: sqlite3.Connection, batch: int = 80, limit: int = 400) -> int:
    """翻訳待ちの見出しを batch 件ずつ翻訳する（1回の応答が長くなりすぎないように）。"""
    rows = conn.execute(
        "SELECT link, title FROM articles WHERE title_ja IS NULL ORDER BY published DESC LIMIT ?",
        (limit,),
    ).fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        translated = translate.to_japanese([title for _, title in chunk])
        if not translated:
            break
        conn.executemany(
            "UPDATE articles SET title_ja = ? WHERE link = ?",
            [(ja, link) for (link, _), ja in zip(chunk, translated)],
        )
        conn.commit()
        done += len(translated)
    return done


def remove_duplicates(conn) -> int:
    """以前の版で二重に保存した記事（省略された見出し）を消す。画像は残る方に移す。"""
    removed = 0
    rows = conn.execute("SELECT link, title_key, image FROM articles ORDER BY length(title_key) DESC").fetchall()
    for link, key, image in rows:
        if not key or len(key) < MIN_PREFIX:
            continue
        other = conn.execute(
            "SELECT link, image FROM articles WHERE link != ? AND title_key LIKE ? || '%' "
            "AND length(title_key) > length(?) LIMIT 1", (link, key, key)).fetchone()
        if other:
            if image and not other[1]:
                conn.execute("UPDATE articles SET image = ? WHERE link = ?", (image, other[0]))
            conn.execute("DELETE FROM articles WHERE link = ?", (link,))
            removed += 1
    conn.commit()
    return removed


def main() -> int:
    conn = open_db()
    print(f"重複の整理 {remove_duplicates(conn)} 件")
    total = 0
    feeds = [(lang, f"Google {gl}", feed_url(q, hl, gl, ceid), parse_rss)
             for lang, q, hl, gl, ceid in SOURCES]
    feeds += [(lang, f"Bing {mkt}", bing_url(q, mkt), parse_bing_rss) for lang, q, mkt in BING_SOURCES]
    for lang, name, url, parser in feeds:
        body = b""
        try:
            body = fetch(url)
            items = parser(body, lang)
        except Exception as e:  # 1つの取得元が失敗しても他は続ける
            # 原因を調べられるよう、受け取った内容の先頭も残す
            print(f"[{name}] 取得に失敗しました: {e} / 先頭: {body[:120]!r}", file=sys.stderr)
            continue
        added = save(conn, items)
        total += len(items)
        print(f"[{name}] 取得 {len(items)} 件 / 新規 {added} 件")
    print(f"翻訳 {translate_pending(conn)} 件")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
