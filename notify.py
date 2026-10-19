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
  PUSH_SUBSCRIPTIONS  : （任意）手で登録した宛先。JSON の配列
宛先は groups.json の notify.subscribe_csv_url（アプリから自動登録される Google フォームの回答）からも読む。
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


def form_subscriptions(csv_url: str) -> list[dict]:
    """アプリの「通知をオンにする」で Google フォームに自動登録された宛先を読む。"""
    import csv
    import io
    import urllib.request
    try:
        req = urllib.request.Request(csv_url, headers={"User-Agent": "kento-news"})
        with urllib.request.urlopen(req, timeout=20) as res:
            text = res.read().decode("utf-8-sig")
    except Exception as e:
        print(f"[通知] 登録フォームの読み込みに失敗しました: {e}", flush=True)
        return []
    subs = []
    for row in csv.reader(io.StringIO(text)):
        for cell in row:
            try:
                sub = json.loads(cell)
            except ValueError:
                continue  # 見出しの行やタイムスタンプ、壊れた登録は飛ばす
            if isinstance(sub, dict) and str(sub.get("endpoint", "")).startswith("https://") and "keys" in sub:
                subs.append(sub)
    return subs


def all_subscriptions(conn, config: dict) -> list[dict]:
    """Secrets の宛先とフォームの宛先をまとめ、同じ端末の重複と、使えなくなった宛先を除く。"""
    subs = []
    env = os.environ.get("PUSH_SUBSCRIPTIONS")
    if env:
        loaded = json.loads(env)
        subs += loaded if isinstance(loaded, list) else [loaded]
    csv_url = config.get("notify", {}).get("subscribe_csv_url")
    if csv_url:
        subs += form_subscriptions(csv_url)
    dead = set(json.loads(get_state(conn, "push_dead") or "[]"))
    seen, out = set(), []
    for sub in subs:
        ep = sub["endpoint"]
        if ep not in seen and ep not in dead:
            seen.add(ep)
            out.append(sub)
    return out


def get_state(conn, key):
    row = conn.execute("SELECT value FROM watch_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def send(conn, config: dict, title: str, body: str) -> None:
    key = os.environ.get("VAPID_PRIVATE_KEY")
    targets = all_subscriptions(conn, config)
    if not (key and targets):
        print("[通知] 鍵または宛先がないため送りません", flush=True)
        return
    from pywebpush import WebPushException, webpush

    dead = set(json.loads(get_state(conn, "push_dead") or "[]"))
    sent = 0
    for sub in targets:
        host = sub.get("endpoint", "")
        device = "iPhone/iPad" if "apple.com" in host else "Android" if "googleapis.com" in host else host[:30]
        try:
            webpush(sub, json.dumps({"title": title, "body": body, "url": APP_URL}, ensure_ascii=False),
                    vapid_private_key=key, vapid_claims={"sub": CONTACT})
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                dead.add(host)  # アプリを入れ直したなどで使えなくなった宛先。次からは送らない
            print(f"[通知] {device} に送れませんでした: {status}", flush=True)
    conn.execute("INSERT INTO watch_state (key, value) VALUES ('push_dead', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(sorted(dead)),))
    conn.commit()
    print(f"[通知] {sent} / {len(targets)} 台に送りました", flush=True)


def reminders(conn, now, rules: dict) -> list[str]:
    """放送前・締切前のリマインド。同じ予定・同じ段階では1回だけ知らせる。

    - 出演: 開始時刻があれば、開始までが「次の更新まで（1時間）」以内になった回で。なければ当日の朝
    - チケット: 前日（9時以降）と当日の朝
    - 発売日・コンサート・イベント: 前日（9時以降）
    """
    from datetime import datetime, timedelta
    if not rules.get("reminders", True):
        return []
    try:
        rows = conn.execute("SELECT id, kind, date, time, title, date_text FROM events WHERE date != ''").fetchall()
    except Exception:
        return []
    today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    lines = []

    def once(key: str, line: str):
        if get_state(conn, "remind:" + key) is None:
            conn.execute("INSERT INTO watch_state (key, value) VALUES (?, '1') ON CONFLICT(key) DO NOTHING",
                         ("remind:" + key,))
            lines.append(line)

    for eid, kind, day, hhmm, title, date_text in rows:
        if kind == "出演":
            if hhmm:
                start = datetime.fromisoformat(f"{day}T{hhmm}")
                if timedelta(0) < start - now <= timedelta(hours=rules.get("remind_hours", 1)):
                    once(f"{eid}:soon", f"📺 まもなく {hhmm}〜 {title}")
            elif day == today and now.hour >= 6:
                once(f"{eid}:today", f"📺 今日 {title}")
        elif kind == "チケット":
            if day == tomorrow and now.hour >= 9:
                once(f"{eid}:eve", f"🎫 明日 {hhmm + ' ' if hhmm else ''}{title}")
            if day == today and now.hour >= 6:
                once(f"{eid}:today", f"🎫 今日 {hhmm + ' ' if hhmm else ''}{title}")
        elif kind in ("発売日", "コンサート", "イベント") and day == tomorrow and now.hour >= 9:
            once(f"{eid}:eve", f"{'💿' if kind == '発売日' else '🎤' if kind == 'コンサート' else '🎉'} 明日 {title}")
    conn.commit()
    return lines


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    conn = open_db()
    if "--test" in sys.argv:
        send(conn, config, f"{NAME} ニュース", "🔔 テスト通知です。通知の設定ができました！")
        return 0
    rules = config.get("notify", {})
    cur = current_state(conn, config["groups"])
    row = conn.execute("SELECT value FROM watch_state WHERE key = 'notify_state'").fetchone()
    if row is None:
        print("[通知] 初回のため、今の状態を覚えるだけにします")
    else:
        from datetime import datetime
        lines = reminders(conn, datetime.now(), rules) + changes(json.loads(row[0]), cur, rules)
        max_lines = rules.get("max_lines", 4)
        if lines:
            body = "\n".join(lines[:max_lines]) + (f"\nほか {len(lines) - max_lines} 件" if len(lines) > max_lines else "")
            print(f"[通知] {len(lines)} 件の変化:\n{body}")
            send(conn, config, f"{NAME} の新着", body)
        else:
            print("[通知] 知らせる変化はありません")
    conn.execute("INSERT INTO watch_state (key, value) VALUES ('notify_state', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(cur, ensure_ascii=False),))
    conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
