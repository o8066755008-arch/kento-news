"""前回の実行から変わったことを、iPad / iPhone に通知する（Web Push）。

通知する条件（しきい値）は groups.json の "notify" で変えられる:
  - new_topics     : ここ3日間のトピックに新しい話題が増えたら知らせる
  - youtube_step   : 中島健人の YouTube 登録者がこの人数の区切りを超えたら知らせる（例: 10000 → 26万人突破）
  - fc_step        : FC 会員数（推計）・会員番号がこの区切りを超えたら知らせる
  - new_events     : 新しいイベント（ライブ・出演・オフ会など）が見つかったら知らせる
  - rank_change    : FC・YouTube の STARTO 内の順位が変わったら知らせる
1回の実行で送る通知は1通だけ（変化をまとめて書く）。初回は今の状態を覚えるだけで送らない。

必要な環境変数（GitHub の Secrets）:
  VAPID_PRIVATE_KEY   : 通知に署名する鍵
  PUSH_SUBSCRIPTIONS  : アプリの「通知をオンにする」で表示された文字列（複数の端末なら JSON の配列）
"""
import json
import os
import time
import sys
from pathlib import Path

from crawler import open_db

CONFIG_PATH = Path(__file__).resolve().parent / "groups.json"
NAME = "中島健人"
APP_URL = "https://o8066755008-arch.github.io/kento-news/"
CONTACT = "https://o8066755008-arch.github.io"  # 通知サービスに伝える連絡先（https のドメインだけが使える）


def latest(conn) -> dict[str, dict[str, int]]:
    """{グループ: {metric: 最新の値}}"""
    out: dict[str, dict[str, int]] = {}
    for grp, metric, value in conn.execute(
            "SELECT grp, metric, value FROM metrics m WHERE date = "
            "(SELECT MAX(date) FROM metrics WHERE grp = m.grp AND metric = m.metric)"):
        out.setdefault(grp, {})[metric] = value
    return out


def fc_value(m: dict[str, int]) -> int | None:
    """アプリのランキングと同じ優先順位（公式 → 定点観測の推計 → 報道）。"""
    for key in ("official", "estimate", "press"):
        if key in m:
            return m[key]
    return None


def rank(values: dict[str, int | None], name: str) -> int | None:
    ordered = sorted((v, g) for g, v in values.items() if v is not None)[::-1]
    names = [g for _, g in ordered]
    return names.index(name) + 1 if name in names else None


def current_state(conn, groups: list[dict]) -> dict:
    vals = latest(conn)
    ended = {g["name"] for g in groups if g.get("ended")}
    me = vals.get(NAME, {})
    row = conn.execute("SELECT value FROM watch_state WHERE key = 'digest'").fetchone()
    topics = [f"{t['region']}|{t['title']}" for t in json.loads(row[0])["topics"]] if row else []
    today = time.strftime("%Y-%m-%d")
    try:
        events = [f"{eid}|{dt or '日付未定'} {title}" for eid, dt, title in conn.execute(
            "SELECT id, date_text, title FROM events WHERE date = '' OR date >= ?", (today,))]
    except Exception:  # まだイベントの表がない
        events = []
    return {
        "topics": topics,
        "events": events,
        "youtube": me.get("youtube"),
        "fc": fc_value(me),
        "member_no": me.get("member_no"),
        "fc_rank": rank({g: fc_value(m) for g, m in vals.items() if g not in ended}, NAME),
        "yt_rank": rank({g: m.get("youtube") for g, m in vals.items()}, NAME),
    }


def man(v: int) -> str:
    return f"{v / 10000:g}万"


def crossed(prev: int | None, cur: int | None, step: int) -> int | None:
    """prev から cur の間に step の区切りを超えたら、その区切りの値を返す。"""
    if prev is None or cur is None or step <= 0:
        return None
    return cur // step * step if cur // step > prev // step else None


def changes(prev: dict, cur: dict, rules: dict) -> list[str]:
    lines = []
    if rules.get("new_topics", True):
        for t in cur["topics"]:
            if t not in prev.get("topics", []):
                region, title = t.split("|", 1)
                lines.append(f"{'🇯🇵' if region == '国内' else '🌏'} {title}")
    if rules.get("new_events", True):
        known = {e.split("|", 1)[0] for e in prev.get("events", [])}
        for e in cur.get("events", []):
            eid, label = e.split("|", 1)
            if eid not in known:
                lines.append(f"🎤 {label}")
    if (v := crossed(prev.get("youtube"), cur["youtube"], rules.get("youtube_step", 10000))):
        lines.append(f"▶️ YouTube 登録者が {man(v)}人を突破")
    if (v := crossed(prev.get("fc"), cur["fc"], rules.get("fc_step", 10000))):
        lines.append(f"🎫 FC 会員数（推計）が {man(v)}人に")
    if (v := crossed(prev.get("member_no"), cur["member_no"], rules.get("fc_step", 10000))):
        lines.append(f"🎫 会員番号が {man(v)}番台に")
    if rules.get("rank_change", True):
        for key, label in (("fc_rank", "FC 会員数"), ("yt_rank", "YouTube 登録者")):
            a, b = prev.get(key), cur[key]
            if a and b and a != b:
                lines.append(f"{'📈' if b < a else '📉'} {label}の順位 {a}位 → {b}位")
    return lines


def send(title: str, body: str) -> None:
    key, subs = os.environ.get("VAPID_PRIVATE_KEY"), os.environ.get("PUSH_SUBSCRIPTIONS")
    if not (key and subs):
        print("[通知] VAPID_PRIVATE_KEY / PUSH_SUBSCRIPTIONS が未設定のため送りません")
        return
    from pywebpush import WebPushException, webpush

    targets = json.loads(subs)
    for sub in targets if isinstance(targets, list) else [targets]:
        try:
            webpush(sub, json.dumps({"title": title, "body": body, "url": APP_URL}, ensure_ascii=False),
                    vapid_private_key=key, vapid_claims={"sub": CONTACT})
            print("[通知] 送りました")
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            hint = "（通知の登録が切れています。アプリでもう一度オンにしてください）" if status in (404, 410) else ""
            print(f"[通知] 送れませんでした: {status} {hint}", file=sys.stderr)


def main() -> int:
    if "--test" in sys.argv:
        send(f"{NAME} ニュース", "🔔 テスト通知です。通知の設定ができました！")
        return 0
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    rules = config.get("notify", {})
    conn = open_db()
    cur = current_state(conn, config["groups"])
    row = conn.execute("SELECT value FROM watch_state WHERE key = 'notify_state'").fetchone()
    if row is None:
        print("[通知] 初回のため、今の状態を覚えるだけにします")
    else:
        lines = changes(json.loads(row[0]), cur, rules)
        max_lines = rules.get("max_lines", 4)
        if lines:
            body = "\n".join(lines[:max_lines]) + (f"\nほか {len(lines) - max_lines} 件" if len(lines) > max_lines else "")
            print(f"[通知] {len(lines)} 件の変化:\n{body}")
            send(f"{NAME} の新着", body)
        else:
            print("[通知] 知らせる変化はありません")
    conn.execute("INSERT INTO watch_state (key, value) VALUES ('notify_state', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(cur, ensure_ascii=False),))
    conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
