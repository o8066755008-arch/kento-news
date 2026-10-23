"""data.db の内容を site/data.json に書き出す。site/ をそのまま公開すればスマホで見られる。"""
import json
import time
from pathlib import Path

from crawler import open_db

SITE_DIR = Path(__file__).resolve().parent / "site"


def build(conn, out: Path = SITE_DIR / "data.json", limit: int = 3000) -> dict:
    news = [
        {"title": title_ja or title, "original": title if lang != "ja" else None,
         "translated": bool(title_ja), "lang": lang, "source": source,
         "published": published, "link": link, "image": image, "kind": kind or "news"}
        for title, title_ja, lang, source, published, link, image, kind in conn.execute(
            "SELECT title, title_ja, lang, source, published, link, image, kind FROM articles "
            "WHERE COALESCE(kind, 'news') != 'starto' ORDER BY published DESC LIMIT ?", (limit,))
    ]
    # STARTO のグループ別ニュース（ここ7日・各グループ新しい順に最大40件）
    starto: dict[str, list] = {}
    since7 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    for grp, title, source, published, link, image in conn.execute(
            "SELECT grp, title, source, published, link, image FROM articles "
            "WHERE kind = 'starto' AND published >= ? ORDER BY published DESC", (since7,)):
        lst = starto.setdefault(grp, [])
        if len(lst) < 40:
            lst.append({"title": title, "source": source, "published": published, "link": link, "image": image,
                        "lang": "ja", "kind": "starto"})
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
    # イベント：これからの予定と、終わって2週間以内のもの（日付未定も含む）
    import events as ev
    ev.open_events(conn)
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 90 * 86400))  # 過去の出来事は90日分
    events = [
        {"title": t, "kind": k, "date": d, "date_text": dt, "place": pl, "region": r, "url": u, "source": src,
         "tier": tier, "label": label or "ニュース", "year_certain": bool(yc), "time": tm or ""}
        for t, k, d, dt, pl, r, u, src, tier, label, yc, tm in conn.execute(
            "SELECT title, kind, date, date_text, place, region, url, source, tier, source_label, year_certain, time "
            "FROM events WHERE date = '' OR date >= ? ORDER BY date = '', date, time, tier", (since,))
    ]
    groups_meta = {g["name"]: {"solo": g.get("solo", False), "ended": g.get("ended", False)}
                   for g in config["groups"]}
    data = {"updated": time.strftime("%Y-%m-%d %H:%M"), "news": news, "digest": digest, "fanclub": fanclub,
            "groups_meta": groups_meta, "events": events, "starto": starto,
            "vapid_public_key": config.get("notify", {}).get("vapid_public_key", ""),
            "subscribe_form": config.get("notify", {}).get("subscribe_form"),
            "fan_form_url": config.get("fan_form_url", "")}
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


if __name__ == "__main__":
    d = build(open_db())
    print(f"ニュース {len(d['news'])} 件 / ファンクラブ {len(d['fanclub'])} グループ → site/data.json")
