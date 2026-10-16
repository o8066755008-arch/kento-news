"""data.db の内容を site/data.json に書き出す。site/ をそのまま公開すればスマホで見られる。"""
import json
import time
from pathlib import Path

from crawler import open_db

SITE_DIR = Path(__file__).resolve().parent / "site"


def build(conn, out: Path = SITE_DIR / "data.json", limit: int = 800) -> dict:
    news = [
        {"title": title_ja or title, "original": title if lang != "ja" else None,
         "translated": bool(title_ja), "lang": lang, "source": source,
         "published": published, "link": link, "image": image}
        for title, title_ja, lang, source, published, link, image in conn.execute(
            "SELECT title, title_ja, lang, source, published, link, image FROM articles "
            "ORDER BY published DESC LIMIT ?", (limit,))
    ]
    # {グループ: {metric: [{date, value, source}, ...]}}
    fanclub: dict[str, dict] = {}
    for day, grp, metric, value, source in conn.execute(
            "SELECT date, grp, metric, value, source FROM metrics ORDER BY date"):
        fanclub.setdefault(grp, {}).setdefault(metric, []).append(
            {"date": day, "value": value, "source": source})
    config = json.loads((Path(__file__).resolve().parent / "groups.json").read_text(encoding="utf-8"))
    row = conn.execute("SELECT value FROM watch_state WHERE key = 'digest'").fetchone()
    digest = json.loads(row[0]) if row else None
    if digest:
        # まとめを作った後に見つかったサムネイルも使えるようにする
        images = dict(conn.execute("SELECT link, image FROM articles WHERE image IS NOT NULL"))
        for t in digest["topics"]:
            for a in t["articles"]:
                a["image"] = images.get(a["link"])
    data = {"updated": time.strftime("%Y-%m-%d %H:%M"), "news": news, "digest": digest, "fanclub": fanclub,
            "fan_form_url": config.get("fan_form_url", "")}
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


if __name__ == "__main__":
    d = build(open_db())
    print(f"ニュース {len(d['news'])} 件 / ファンクラブ {len(d['fanclub'])} グループ → site/data.json")
