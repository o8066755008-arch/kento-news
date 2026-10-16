"""中島健人さんのイベント情報（ライブ・イベント・出演・ファンのオフ会など）を集める。

取得元:
  - 集めたニュースのうち、ライブ・イベントらしい見出しの記事（国内・海外とも）
  - groups.json の event_pages に書いたページ（公式のお知らせ、オフ会の告知サイトなど。1日1回）
どちらも AI で「いつ・どこで・何があるか」を読み取り、はっきりした予定だけを残す。

使い方:
    python3 events.py
"""
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import translate
from crawler import open_db

CONFIG_PATH = Path(__file__).resolve().parent / "groups.json"

# イベントらしい見出しを選ぶための言葉（日本語・英語・韓国語・中国語・タイ語）
EVENT_WORDS = re.compile(
    r"ライブ|ツアー|公演|コンサート|イベント|ファンミ|オフ会|握手|舞台挨拶|出演|開催|上映|来日|来台|来韓|"
    r"フェス|展示|ポップアップ|発売記念|サイン会|チケット|"
    r"tour|concert|live|fan ?meeting|festival|event|premiere|"
    r"콘서트|투어|팬미팅|공연|내한|演唱會|巡演|見面會|粉絲見面|簽名會|คอนเสิร์ต|แฟนมีต",
    re.IGNORECASE)

SYSTEM = (
    "あなたは中島健人さんのファン向けのイベント情報担当です。渡されたテキストから、"
    "中島健人さんが出る（またはファンが集まる）具体的な予定を抜き出してください。\n"
    "- kind は「ライブ」「イベント」「出演」「オフ会」「その他」のどれかにします。"
    "ライブ・コンサート・ツアーは「ライブ」、ファンミーティング・舞台挨拶・サイン会・展示などは「イベント」、"
    "テレビ・映画祭・番組などへの出演は「出演」、ファンが主催する集まりは「オフ会」です。\n"
    "- title は日本語で30字以内。place は会場や都市（日本語、わからなければ空文字）。\n"
    "- date は開催日（期間なら初日）を YYYY-MM-DD、年が書かれていなければ今年として補い、"
    "日付がわからなければ空文字。date_text は「11月3日〜5日」のような元の書き方を日本語で。\n"
    "- region は日本国内なら「国内」、海外なら「海外」。\n"
    "- source_id には根拠になった項目の番号を入れます。\n"
    "- 予定ではない話題（過去の感想、インタビュー、ランキングなど）や、中島健人さんと関係ない予定は入れないでください。"
    "同じ予定が複数の項目に出てきたら1件にまとめます。"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["ライブ", "イベント", "出演", "オフ会", "その他"]},
                    "date": {"type": "string"},
                    "date_text": {"type": "string"},
                    "place": {"type": "string"},
                    "region": {"type": "string", "enum": ["国内", "海外"]},
                    "source_id": {"type": "integer"},
                },
                "required": ["title", "kind", "date", "date_text", "place", "region", "source_id"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


def open_events(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            kind TEXT,
            date TEXT,
            date_text TEXT,
            place TEXT,
            region TEXT,
            url TEXT,
            source TEXT,
            found_at TEXT
        )"""
    )
    return conn


def event_id(title: str, day: str) -> str:
    key = re.sub(r"[\s\W_]+", "", title).lower()[:20] + "|" + day
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def save_events(conn, events: list[dict], sources: list[dict]) -> int:
    added = 0
    today = date.today()
    for e in events:
        if not 0 <= e["source_id"] < len(sources):
            continue
        day = e["date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["date"]) else ""
        # 半年以上前の予定は、昔の話題を読み違えたものとして入れない
        if day and date.fromisoformat(day) < today - timedelta(days=180):
            continue
        src = sources[e["source_id"]]
        eid = event_id(e["title"], day)
        if conn.execute("SELECT 1 FROM events WHERE id = ?", (eid,)).fetchone():
            continue
        conn.execute(
            "INSERT INTO events (id, title, kind, date, date_text, place, region, url, source, found_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, e["title"], e["kind"], day, e["date_text"], e["place"], e["region"],
             src["url"], src["source"], today.isoformat()),
        )
        added += 1
        print(f"[イベント] {e['kind']} {e['date_text'] or '日付未定'} {e['title']}（{e['place'] or '場所未定'}）")
    conn.commit()
    return added


def get_state(conn, key):
    row = conn.execute("SELECT value FROM watch_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key, value):
    conn.execute("INSERT INTO watch_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def from_news(conn) -> None:
    """まだ確認していない、イベントらしい見出しの記事から予定を読み取る。"""
    since = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    rows = conn.execute(
        "SELECT link, title, COALESCE(title_ja, title), source, published FROM articles "
        "WHERE published >= ? ORDER BY published DESC", (since,)).fetchall()
    todo = [r for r in rows
            if (EVENT_WORDS.search(r[1]) or EVENT_WORDS.search(r[2])) and get_state(conn, "ev:" + r[0]) is None]
    if not todo:
        print("[イベント] 新しく確認する記事はありません")
        return
    todo = todo[:80]
    text = f"今日は {date.today().isoformat()} です。\n" + "\n".join(
        f"{i}. {ja}（{src}・{pub[:10]}）" for i, (_, _, ja, src, pub) in enumerate(todo))
    result = translate.ask_json(SYSTEM, text, SCHEMA)
    if result is None:
        return  # 次回もう一度読む
    sources = [{"url": link, "source": src} for link, _, _, src, _ in todo]
    n = save_events(conn, result["events"], sources)
    for link, *_ in todo:
        set_state(conn, "ev:" + link, "1")
    print(f"[イベント] ニュース {len(todo)} 件を確認 / 新しい予定 {n} 件")


def from_pages(conn, pages: list[dict]) -> None:
    """公式のお知らせやオフ会の告知ページを1日1回読む。"""
    import fanclub
    import thumbnails
    today = date.today().isoformat()
    if get_state(conn, "events_daily") == today:
        print("[イベント] 告知ページは今日すでに確認済みです")
        return
    for page in pages:
        url = page["url"]
        if not thumbnails.allowed(url):
            print(f"[イベント] {url} は robots.txt で禁止されているため読みません")
            continue
        try:
            _, raw = thumbnails.read_head(url, max_bytes=2_000_000)
        except Exception as e:
            print(f"[イベント] {url} の取得に失敗しました: {e}", file=sys.stderr)
            continue
        text = fanclub.page_text(raw)
        # 日付らしいものを含む行と前後の行だけを AI に渡す
        lines = text.splitlines()
        keep = sorted({j for i, l in enumerate(lines)
                       if re.search(r"\d{1,2}\s*[/月.]\s*\d{1,2}|\d{4}[/.-]\d{1,2}", l)
                       for j in (i - 1, i, i + 1) if 0 <= j < len(lines)})
        snippet = "\n".join(lines[j] for j in keep)[:15000]
        digest = hashlib.sha256(snippet.encode()).hexdigest()
        print(f"[イベント] {url}: 本文 {len(text)} 文字 / 日付のある部分 {len(snippet)} 文字")
        if not snippet or get_state(conn, "evpage:" + url) == digest:
            continue
        result = translate.ask_json(SYSTEM, f"今日は {today} です。\n0. {snippet}", SCHEMA)
        if result is None:
            continue
        for e in result["events"]:
            e["source_id"] = 0
        n = save_events(conn, result["events"], [{"url": url, "source": page.get("note", url)}])
        print(f"[イベント] {url} から新しい予定 {n} 件")
        set_state(conn, "evpage:" + url, digest)
    set_state(conn, "events_daily", today)


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    conn = open_events(open_db())
    from_news(conn)
    from_pages(conn, config.get("event_pages", []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
