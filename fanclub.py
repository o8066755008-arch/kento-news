"""STARTO ENTERTAINMENT 所属グループのファンクラブ会員数を1日1回記録する。

公式の会員数は公開されていないため、取得元は fanclub_sources.json で指定する。
各グループの "url" に会員数が載っている公開ページ、"pattern" に数字を取り出す
正規表現（最初のグループが数字）を書く。url が空のグループは自動取得しない。

使い方:
    python3 fanclub.py                 # 設定に従って今日の数を取得
    python3 fanclub.py add "Snow Man" 1234567   # 手動で今日の数を記録
"""
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

from crawler import USER_AGENT, open_db

CONFIG_PATH = Path(__file__).resolve().parent / "fanclub_sources.json"


def extract_count(page: str, pattern: str) -> int | None:
    m = re.search(pattern, page)
    if not m:
        return None
    return int(re.sub(r"[^\d]", "", m.group(1)))


def record(conn, grp: str, count: int, source: str, day: str | None = None) -> None:
    conn.execute(
        "INSERT INTO fanclub_counts (date, grp, count, source) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(date, grp) DO UPDATE SET count = excluded.count, source = excluded.source",
        (day or date.today().isoformat(), grp, count, source),
    )
    conn.commit()


def crawl(conn) -> int:
    groups = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["groups"]
    ok = 0
    for g in groups:
        if not g.get("url"):
            continue
        try:
            req = urllib.request.Request(g["url"], headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as res:
                page = res.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[{g['name']}] 取得に失敗しました: {e}", file=sys.stderr)
            continue
        count = extract_count(page, g["pattern"])
        if count is None:
            print(f"[{g['name']}] ページに会員数が見つかりませんでした", file=sys.stderr)
            continue
        record(conn, g["name"], count, g["url"])
        print(f"[{g['name']}] {count:,} 人")
        ok += 1
    if not any(g.get("url") for g in groups):
        print("fanclub_sources.json に取得元 URL が設定されていません")
    return ok


def main() -> int:
    conn = open_db()
    if len(sys.argv) == 4 and sys.argv[1] == "add":
        record(conn, sys.argv[2], int(sys.argv[3]), "手動入力")
        print(f"[{sys.argv[2]}] {int(sys.argv[3]):,} 人を記録しました")
        return 0
    crawl(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
