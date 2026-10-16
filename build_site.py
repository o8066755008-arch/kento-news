"""data.db の内容を site/data.json に書き出す。site/ をそのまま公開すればスマホで見られる。"""
import json
import time
from pathlib import Path

from crawler import open_db

SITE_DIR = Path(__file__).resolve().parent / "site"


def build(conn, out: Path = SITE_DIR / "data.json", limit: int = 300) -> dict:
    news = [
        {"title": title_ja or title, "original": title if lang != "ja" else None,
         "translated": bool(title_ja), "lang": lang, "source": source,
         "published": published, "link": link}
        for title, title_ja, lang, source, published, link in conn.execute(
            "SELECT title, title_ja, lang, source, published, link FROM articles "
            "ORDER BY published DESC LIMIT ?", (limit,))
    ]
    fanclub: dict[str, list] = {}
    for day, grp, count in conn.execute(
            "SELECT date, grp, count FROM fanclub_counts ORDER BY date"):
        fanclub.setdefault(grp, []).append({"date": day, "count": count})
    data = {"updated": time.strftime("%Y-%m-%d %H:%M"), "news": news, "fanclub": fanclub}
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


if __name__ == "__main__":
    d = build(open_db())
    print(f"ニュース {len(d['news'])} 件 / ファンクラブ {len(d['fanclub'])} グループ → site/data.json")
