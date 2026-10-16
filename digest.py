"""ここ3日間のニュースを、国内・海外のトピックごとに AI でまとめる。

記事本文は読まず、見出し（海外記事は翻訳済みの見出し）だけからまとめる。
対象の記事が前回と同じなら作り直さない（API の利用を減らすため）。

使い方:
    python3 digest.py
"""
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone

import translate
from crawler import open_db

DAYS = 3

SYSTEM = (
    "あなたは中島健人さんのファン向けニュースの編集者です。渡された見出しの一覧を、"
    "同じ出来事ごとにトピックとしてまとめてください。\n"
    "- region は日本国内での活動・話題なら「国内」、韓国やアジア・欧米など海外での活動や"
    "海外メディアの話題なら「海外」にします。\n"
    "- title は20字程度の見出し、summary は見出しから分かることだけを2〜3文の日本語で書きます。"
    "見出しにない内容を推測で足さないでください。\n"
    "- article_ids には、そのトピックに含まれる記事の番号をすべて入れます。\n"
    "- 中島健人さんと関係の薄い記事はトピックにしなくてかまいません。\n"
    "- 話題の大きい順（記事が多い順）に並べ、国内・海外それぞれ最大8件にします。"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "enum": ["国内", "海外"]},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "article_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["region", "title", "summary", "article_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}

LANG_LABEL = {"ja": "日本のメディア", "ko": "韓国のメディア", "en": "英語圏のメディア",
              "zh": "台湾・香港のメディア", "th": "タイのメディア", "vi": "ベトナムのメディア",
              "id": "インドネシアのメディア", "es": "中南米のメディア", "pt": "ブラジルのメディア",
              "fr": "フランスのメディア"}


def recent_articles(conn, now: datetime) -> list[dict]:
    since = (now - timedelta(days=DAYS)).astimezone(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT link, COALESCE(title_ja, title), lang, source, published FROM articles "
        "WHERE published >= ? ORDER BY published DESC",
        (since,),
    ).fetchall()
    return [{"link": link, "title": title, "lang": lang, "source": source, "published": pub}
            for link, title, lang, source, pub in rows]


def build_digest(articles: list[dict]) -> dict | None:
    lines = "\n".join(
        f"{i}. [{LANG_LABEL.get(a['lang'], a['lang'])}] {a['title']}（{a['source']}）"
        for i, a in enumerate(articles))
    result = translate.ask_json(SYSTEM, lines, SCHEMA)
    if result is None:
        return None
    topics = []
    for t in result["topics"]:
        ids = sorted({i for i in t["article_ids"] if 0 <= i < len(articles)})
        if not ids:
            continue
        topics.append({
            "region": t["region"], "title": t["title"], "summary": t["summary"],
            "articles": [{k: articles[i][k] for k in ("title", "link", "source", "published", "lang")}
                         for i in ids],
        })
    return {"days": DAYS, "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "topics": topics}


def main() -> int:
    conn = open_db()
    articles = recent_articles(conn, datetime.now(timezone.utc))
    if not articles:
        print(f"[トピックス] ここ{DAYS}日間の記事がありません")
        return 0
    digest_key = hashlib.sha256(json.dumps([a["link"] for a in articles]).encode()).hexdigest()
    row = conn.execute("SELECT value FROM watch_state WHERE key = 'digest_key'").fetchone()
    if row and row[0] == digest_key:
        print("[トピックス] 記事に変化がないため前回のまとめを使います")
        return 0
    digest = build_digest(articles)
    if digest is None:
        return 1
    conn.executemany(
        "INSERT INTO watch_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [("digest", json.dumps(digest, ensure_ascii=False)), ("digest_key", digest_key)],
    )
    conn.commit()
    counts = {r: sum(t["region"] == r for t in digest["topics"]) for r in ("国内", "海外")}
    print(f"[トピックス] 記事 {len(articles)} 件 → 国内 {counts['国内']} 件 / 海外 {counts['海外']} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
