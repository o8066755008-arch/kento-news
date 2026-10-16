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
from crawler import fetch, feed_url, open_db, parse_rss

CONFIG_PATH = Path(__file__).resolve().parent / "groups.json"

# イベントらしい見出しを選ぶための言葉（日本語・英語・韓国語・中国語・タイ語）
EVENT_WORDS = re.compile(
    r"ライブ|ツアー|公演|コンサート|イベント|ファンミ|オフ会|握手|舞台挨拶|出演|開催|上映|来日|来台|来韓|"
    r"フェス|展示|ポップアップ|発売記念|サイン会|チケット|放送|配信|番組|ドラマ|映画|公開|舞台|登場|ゲスト|MC|"
    r"tour|concert|live|fan ?meeting|festival|event|premiere|"
    r"콘서트|투어|팬미팅|공연|내한|演唱會|巡演|見面會|粉絲見面|簽名會|คอนเสิร์ต|แฟนมีต",
    re.IGNORECASE)

VERSION = 5  # 読み取り方を変えたら上げる（それまでのイベントを消して読み直す）

# イベントを探すためのニュース検索（Google ニュース）: (検索語, hl, gl, ceid)
EVENT_QUERIES = [
    ("中島健人 (ライブ OR ツアー OR 公演 OR コンサート)", "ja", "JP", "JP:ja"),
    ("中島健人 (イベント OR ファンミーティング OR 舞台挨拶 OR サイン会 OR 握手会)", "ja", "JP", "JP:ja"),
    ("中島健人 (出演 OR 放送 OR 配信 OR 番組 OR ゲスト)", "ja", "JP", "JP:ja"),
    ("中島健人 (映画 OR ドラマ OR 舞台) (公開 OR 放送 OR 上演)", "ja", "JP", "JP:ja"),
    ("나카지마 켄토 (팬미팅 OR 콘서트 OR 내한 OR 공연)", "ko", "KR", "KR:ko"),
    ("中島健人 (演唱會 OR 見面會 OR 來台 OR 簽名會)", "zh-TW", "TW", "TW:zh-Hant"),
    ('"Kento Nakajima" (concert OR tour OR "fan meeting" OR festival OR event)', "en-US", "US", "US:en"),
]

SYSTEM = (
    "あなたは中島健人さんのファン向けのイベント情報担当です。渡されたテキストから、"
    "中島健人さんが出る（またはファンが集まる）具体的な予定を抜き出してください。\n"
    "- kind は「ライブ」「イベント」「出演」「発売・配信」「オフ会」「その他」のどれかにします。"
    "ライブ・コンサート・ツアーは「ライブ」、ファンミーティング・舞台挨拶・サイン会・展示などは「イベント」、"
    "テレビ・映画祭・番組などへの出演は「出演」、CD・Blu-ray・DVD・グッズの発売や配信の開始は「発売・配信」、"
    "ファンが主催する集まりは「オフ会」です。\n"
    "- title は日本語で30字以内。place は会場や都市（日本語、わからなければ空文字）。\n"
    "- 各項目には記事の日付（published）が付いています。予定の年が書かれていないときは、"
    "その記事の日付から見て次に来る日付として年を補ってください（記事が10月で「4月6日」とあれば、"
    "それが既に終わった話なのか翌年の予定なのかを文脈で判断し、終わった話なら入れない）。\n"
    "- 記事の日付の時点ですでに終わっていた予定・始まっていた番組は入れないでください。\n"
    "- date は開催日（期間なら初日）を YYYY-MM-DD、日付がわからなければ空文字。"
    "date_text は「2026年11月3日〜5日」のように年も付けた日本語で書きます。"
    "year_certain は、年が本文に書かれている・記事の日付からはっきり決められるなら true、推測なら false。\n"
    "- 番組表のページでは、これから放送される番組だけを1つずつ（放送日時・局つきで）入れます。"
    "過去の出演作の一覧や、放送日の書かれていない作品紹介は入れないでください。\n"
    "- テキストに ［link: https://...］ の形でリンクが付いていれば、その予定の詳しいページのリンクを link に入れます"
    "（番組・記事・公演ごとのページ。なければ空文字）。\n"
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
                    "kind": {"type": "string", "enum": ["ライブ", "イベント", "出演", "発売・配信", "オフ会", "その他"]},
                    "date": {"type": "string"},
                    "date_text": {"type": "string"},
                    "place": {"type": "string"},
                    "region": {"type": "string", "enum": ["国内", "海外"]},
                    "year_certain": {"type": "boolean"},
                    "link": {"type": "string"},
                    "source_id": {"type": "integer"},
                },
                "required": ["title", "kind", "date", "date_text", "place", "region", "year_certain", "link", "source_id"],
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    for col, decl in [("tier", "INTEGER DEFAULT 4"), ("source_label", "TEXT"), ("year_certain", "INTEGER DEFAULT 1")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
    return conn


NEWS_TIER = 4  # ニュース記事からの予定（公式・番組表・映画情報より信頼度は下）


def norm(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", title).lower()


def event_id(title: str, day: str) -> str:
    key = re.sub(r"[\s\W_]+", "", title).lower()[:20] + "|" + day
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def similar(a: str, b: str) -> bool:
    import difflib
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.5:
        return True
    # 「台北公演」と「台北ソロコンサート」のように、固有の言葉（3文字以上の共通部分）が重なるか
    m = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return m.size >= 4


def same_event(conn, title: str, day: str):
    """同じ予定を探す（別の情報源に同じ予定が載っていることが多いため）。

    日付があれば同じ日付の予定と、日付がなければすべての予定と見出しを比べる。
    """
    if day:
        rows = conn.execute("SELECT id, title, tier, date FROM events WHERE date = ? OR date = ''", (day,))
    else:
        rows = conn.execute("SELECT id, title, tier, date FROM events")
    for eid, other, tier, other_day in rows:
        if similar(title, other):
            return eid, tier, other_day
    return None


def save_events(conn, events: list[dict], sources: list[dict]) -> int:
    """予定を保存する。同じ予定が別の情報源にもあれば、信頼度の高い（tier の小さい）情報源を残す。"""
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
        # 記事の時点で終わっていた予定（読み違いを含む）は入れない
        pub = (src.get("published") or "")[:10]
        if day and pub and day < pub:
            print(f"[イベント] 記事（{pub}）より前の予定のため除外: {e['date_text']} {e['title']}")
            continue
        tier = src.get("tier", NEWS_TIER)
        values = (e["title"], e["kind"], day, e["date_text"], e["place"], e["region"],
                  src["url"], src["source"], tier, src.get("label", "ニュース"), int(e.get("year_certain", True)))
        dup = same_event(conn, e["title"], day)
        if dup:
            # 日付のない方を、日付のある同じ予定で置き換える
            if (tier < (dup[1] or NEWS_TIER) and (day or not dup[2])) or (day and not dup[2]):
                conn.execute("UPDATE events SET title=?, kind=?, date=?, date_text=?, place=?, region=?, url=?, "
                             "source=?, tier=?, source_label=?, year_certain=? WHERE id = ?", values + (dup[0],))
            continue
        eid = event_id(e["title"], day)
        if conn.execute("SELECT 1 FROM events WHERE id = ?", (eid,)).fetchone():
            continue
        conn.execute(
            "INSERT INTO events (title, kind, date, date_text, place, region, url, source, tier, source_label, "
            "year_certain, id, found_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values + (eid, today.isoformat()),
        )
        added += 1
        print(f"[イベント] [{src.get('label', 'ニュース')}] {e['kind']} {e['date_text'] or '日付未定'} {e['title']}")
    conn.commit()
    return added


def get_state(conn, key):
    row = conn.execute("SELECT value FROM watch_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key, value):
    conn.execute("INSERT INTO watch_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def event_candidates(conn) -> list[tuple[str, str, str, str]]:
    """(リンク, 見出し（日本語があれば日本語）, 配信元, 日付) のうち、まだ確認していないもの。"""
    since = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    found: dict[str, tuple] = {}
    for link, title, ja, src, pub in conn.execute(
            "SELECT link, title, COALESCE(title_ja, title), source, published FROM articles "
            "WHERE published >= ?", (since,)):
        if EVENT_WORDS.search(title) or EVENT_WORDS.search(ja):
            found[link] = (link, ja, src, pub)
    for q, hl, gl, ceid in EVENT_QUERIES:
        try:
            items = parse_rss(fetch(feed_url(q, hl, gl, ceid)), "ja")
        except Exception as e:
            print(f"[イベント] 検索「{q}」の取得に失敗しました: {e}", file=sys.stderr)
            continue
        for it in items:
            if it["published"] >= since:
                found.setdefault(it["link"], (it["link"], it["title"], it["source"], it["published"]))
    return sorted((v for k, v in found.items() if get_state(conn, f"ev{VERSION}:" + k) is None),
                  key=lambda v: v[3], reverse=True)


def from_news(conn, batch: int = 60, limit: int = 300) -> None:
    """イベントらしい記事（集めたニュース＋イベント用の検索）から予定を読み取る。"""
    todo = event_candidates(conn)[:limit]
    if not todo:
        print("[イベント] 新しく確認する記事はありません")
        return
    total = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        text = f"今日は {date.today().isoformat()} です。\n" + "\n".join(
            f"{j}. {title}（{src}・published {pub[:10]}）" for j, (_, title, src, pub) in enumerate(chunk))
        result = translate.ask_json(SYSTEM, text, SCHEMA)
        if result is None:
            break  # 次回もう一度読む
        sources = [{"url": link, "source": src, "published": pub} for link, _, src, pub in chunk]
        total += save_events(conn, result["events"], sources)
        for link, *_ in chunk:
            set_state(conn, f"ev{VERSION}:" + link, "1")
    print(f"[イベント] 記事 {len(todo)} 件を確認 / 新しい予定 {total} 件")


def with_links(raw_html: str, page_url: str) -> str:
    """リンクの文字のうしろに ［link: URL］ を書き足す（AI が予定ごとの詳しいページを選べるように）。"""
    import html as htmllib
    import urllib.parse

    def repl(m):
        href = htmllib.unescape(m.group(1))
        if href.startswith(("#", "javascript:", "mailto:")):
            return m.group(0)
        # 「<」「>」で囲むと本文を取り出すときにタグとして消えるので、［link: …］の形にする
        return f"{m.group(2)} ［link: {urllib.parse.urljoin(page_url, href)}］</a>"
    return re.sub(r'(?is)<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                  lambda m: m.group(0)[:m.group(0).find(">") + 1] + repl(m), raw_html)


def from_pages(conn, pages: list[dict]) -> None:
    """公式のお知らせやオフ会の告知ページを1日1回読む。"""
    import fanclub
    import thumbnails
    today = date.today().isoformat()
    if get_state(conn, f"events_daily{VERSION}") == today:
        print("[イベント] 告知ページは今日すでに確認済みです")
        return
    for page in sorted(pages, key=lambda p: p.get("tier", NEWS_TIER)):
        url = page["url"]
        if not thumbnails.allowed(url):
            print(f"[イベント] {url} は robots.txt で禁止されているため読みません")
            continue
        try:
            _, raw = thumbnails.read_head(url, max_bytes=2_000_000)
        except Exception as e:
            print(f"[イベント] {url} の取得に失敗しました: {e}", file=sys.stderr)
            continue
        text = fanclub.page_text(with_links(raw, url))
        # 日付らしいものを含む行と前後の行だけを AI に渡す
        lines = text.splitlines()
        keep = sorted({j for i, l in enumerate(lines)
                       if re.search(r"\d{1,2}\s*[/月.]\s*\d{1,2}|\d{4}[/.-]\d{1,2}", l)
                       for j in (i - 1, i, i + 1) if 0 <= j < len(lines)})
        snippet = "\n".join(lines[j] for j in keep)[:8000]
        digest = hashlib.sha256(snippet.encode()).hexdigest()
        print(f"[イベント] {url}: 本文 {len(text)} 文字 / 日付のある部分 {len(snippet)} 文字")
        if not snippet or get_state(conn, f"evpage{VERSION}:" + url) == digest:
            continue
        result = translate.ask_json(SYSTEM, f"今日は {today} です。\n0. {snippet}", SCHEMA)
        if result is None:
            continue
        base = {"url": url, "source": page.get("note", url), "published": fanclub.page_date(raw) or "",
                "tier": page.get("tier", NEWS_TIER), "label": page.get("label", "")}
        sources = []
        for i, e in enumerate(result["events"]):
            # 予定ごとの詳しいページがあれば「情報元を見る」をそこに向ける（なければ一覧ページ）
            link = e.get("link", "")
            sources.append(dict(base, url=link) if link.startswith(("https://", "http://")) else base)
            e["source_id"] = i
        n = save_events(conn, result["events"], sources)
        print(f"[イベント] {url} から新しい予定 {n} 件")
        set_state(conn, f"evpage{VERSION}:" + url, digest)
    set_state(conn, f"events_daily{VERSION}", today)


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    conn = open_events(open_db())
    if get_state(conn, "events_version") != str(VERSION):
        # 読み取り方を直したので、これまでのイベントを消して読み直す
        conn.execute("DELETE FROM events")
        set_state(conn, "events_version", str(VERSION))
        print("[イベント] 読み取り方を更新したため、イベントを読み直します")
    from_news(conn)
    from_pages(conn, config.get("event_pages", []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
