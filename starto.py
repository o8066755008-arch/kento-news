"""STARTO ENTERTAINMENT に関わるニュースを、グループ・ソロごとに集める。

groups.json の各グループ（中島健人さん本人は除く）と、事務所全体の話題を Google ニュースで検索する。
中島健人さんのニュースとは混ぜず、「STARTO」タブにグループ別で表示する（kind = 'starto'、grp = グループ名）。
3時間おきの更新でだけ動かす（本数が多いため）。

使い方:
    python3 starto.py
"""
import json
import sys
from pathlib import Path

from crawler import feed_url, fetch, open_db, parse_rss, save

CONFIG_PATH = Path(__file__).resolve().parent / "groups.json"
ME = "中島健人"
COMPANY = "事務所全体"

# グループ名だけでは別の意味の記事が混ざるもの（NEWS・嵐 など）は、メンバー名で絞る
QUERY_OVERRIDES = {
    "NEWS": '"NEWS" (加藤シゲアキ OR 増田貴久 OR 小山慶一郎)',
    "嵐": "嵐 (櫻井翔 OR 相葉雅紀 OR 松本潤 OR 二宮和也 OR 大野智)",
    "WEST.": '"WEST." (重岡大毅 OR 桐山照史 OR 中間淳太 OR 神山智洋 OR 藤井流星 OR 濱田崇裕 OR 小瀧望)',
    "SUPER EIGHT": '"SUPER EIGHT" (横山裕 OR 村上信五 OR 丸山隆平 OR 安田章大 OR 大倉忠義)',
    "20th Century": '"20th Century" (坂本昌行 OR 長野博 OR 井ノ原快彦)',
}


def queries(groups: list[dict]) -> list[tuple[str, str]]:
    out = [(COMPANY, '("STARTO ENTERTAINMENT" OR スタートエンターテイメント OR スタエン) when:3d')]
    for g in groups:
        name = g["name"]
        if name == ME:
            continue
        q = QUERY_OVERRIDES.get(name, f'"{name}"')
        out.append((name, f"{q} when:3d"))
    return out


def main() -> int:
    groups = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["groups"]
    conn = open_db()
    total = 0
    for grp, q in queries(groups):
        try:
            items = parse_rss(fetch(feed_url(q, "ja", "JP", "JP:ja")), "ja")
        except Exception as e:
            print(f"[STARTO] {grp} の取得に失敗しました: {e}", file=sys.stderr)
            continue
        for it in items:
            it["kind"], it["grp"] = "starto", grp
        added = save(conn, items)
        total += added
        print(f"[STARTO] {grp} 取得 {len(items)} 件 / 新規 {added} 件")
    print(f"[STARTO] 新規 合計 {total} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
