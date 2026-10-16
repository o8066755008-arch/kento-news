"""STARTO ENTERTAINMENT 所属グループのファンクラブ会員数と関連する数字を1日1回記録する。

取得元（groups.json で設定）:
  - 公式発表: ニュースの見出しと Wikipedia から、会員数の記述を AI で読み取る
  - 会員番号: ファンが Google フォームで送ってくれた番号から、最新の番号を推定する
  - YouTube 登録者数: YouTube Data API（環境変数 YOUTUBE_API_KEY）
  - Spotify フォロワー数: Spotify Web API（SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET）
キーや URL が未設定の取得元は飛ばす。

使い方:
    python3 fanclub.py                              # すべての取得元から今日の数を記録
    python3 fanclub.py add "Snow Man" 1234567       # 公式発表の数を手動で記録
"""
import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import translate
from crawler import USER_AGENT, feed_url, fetch, open_db, parse_rss

CONFIG_PATH = Path(__file__).resolve().parent / "groups.json"
# Wikimedia の規約で連絡先入りの User-Agent が求められる
WIKI_UA = "kento-news/0.3 (https://github.com/o8066755008-arch/kento-news)"
MAX_COUNT = 20_000_000  # これを超える数は誤りとみなす
MIN_YOUTUBE_SUBS = 10_000  # 公式チャンネルならこれより多いはず


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def record(conn, grp: str, metric: str, value: int, source: str, day: str | None = None) -> None:
    conn.execute(
        "INSERT INTO metrics (date, grp, metric, value, source) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(date, grp, metric) DO UPDATE SET value = excluded.value, source = excluded.source",
        (day or date.today().isoformat(), grp, metric, value, source),
    )
    conn.commit()


def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM watch_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO watch_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_json(url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read())


# ---------------------------------------------------------------- 公式発表（AI で読み取り）

EXTRACT_SYSTEM = (
    "あなたは芸能ニュースのデータ担当です。渡されたテキストから、ファンクラブの会員数が"
    "数字で書かれている箇所だけを抜き出してください。テキストにない数字は作らず、会員数の記述がなければ"
    "空の配列を返します。「120万人」は 1200000 のように人数を整数にします。"
    "kind は、公式発表・本人や事務所の発言にもとづくなら「official」、"
    "「〜とみられる」「〜ようだ」「推定」など報道側の見立てなら「press」にします。"
    "date は会員数がその数になった（または発表された）日付が書かれていれば YYYY-MM-DD、"
    "なければ空文字にします。quote には根拠となる原文を短く入れます。"
)


def extract_schema(group_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "group": {"type": "string", "enum": group_names},
                        "count": {"type": "integer"},
                        "date": {"type": "string"},
                        "kind": {"type": "string", "enum": ["official", "press"]},
                        "quote": {"type": "string"},
                    },
                    "required": ["group", "count", "date", "kind", "quote"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def save_official(conn, items: list[dict], source: str, fallback_day: str) -> int:
    saved = 0
    for it in items:
        if not 0 < it["count"] <= MAX_COUNT:
            continue
        day = it["date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", it["date"]) else fallback_day
        kind = it.get("kind", "official")
        record(conn, it["group"], kind, it["count"], source, day)
        label = {"official": "公式", "press": "報道", "estimate": "推計"}.get(kind, kind)
        print(f"[{label}] {it['group']} {it['count']:,} 人（{day}）: {it['quote']}")
        saved += 1
    return saved


def crawl_news(conn, groups: list[dict]) -> None:
    names = [g["name"] for g in groups]
    new = []
    for name in names:
        try:
            items = parse_rss(fetch(feed_url(f'"{name}" (ファンクラブ OR FC) 会員', "ja", "JP", "JP:ja")))
        except Exception as e:
            print(f"[ニュース] {name} の取得に失敗しました: {e}", file=sys.stderr)
            continue
        for it in items:
            if get_state(conn, "news2:" + it["link"]) is None and re.search(r"会員|FC|ファンクラブ", it["title"]):
                new.append(it)
    if not new:
        print("[ニュース] 会員数の新しい記事はありません")
        return
    text = "\n".join(f"- {it['title']}（{it['published'][:10]}）" for it in new)
    result = translate.ask_json(EXTRACT_SYSTEM, text, extract_schema(names))
    if result is None:
        return  # 次回もう一度読む
    for it in result["items"]:
        src = next((n for n in new if it["quote"] and it["quote"][:10] in n["title"]), None)
        save_official(conn, [it], src["link"] if src else "ニュース", (src or new[0])["published"][:10] or date.today().isoformat())
    for it in new:
        set_state(conn, "news2:" + it["link"], "1")


def wiki_snippets(wikitext: str) -> str:
    """会員数が書かれていそうな文だけを取り出す（AI に送る量を減らすため）。"""
    sentences = re.split(r"(?<=。)|\n", wikitext)
    hits = [s.strip() for s in sentences
            if re.search(r"ファンクラブ|FC", s) and re.search(r"会員", s) and re.search(r"\d", s)]
    return "\n".join(hits)


def page_text(html_text: str) -> str:
    """HTML から本文の文字だけを取り出す。"""
    import html as htmllib
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html_text)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|td|th)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return "\n".join(line.strip() for line in htmllib.unescape(t).splitlines() if line.strip())


def page_snippets(text: str, names: list[str]) -> str:
    """まとめページは表の1行に「グループ名：○万人」だけが書かれることが多いので、
    人数らしい数字を含む行と、その前後の行（グループ名の見出し）を残す。"""
    lines = text.splitlines()
    keep = set()
    for i, line in enumerate(lines):
        # 「194万人」のような書き方と、表によくある「1,942,000」のような人数だけの書き方の両方を拾う
        if re.search(r"\d", line) and re.search(r"万|人|名|会員|\d{1,3}(,\d{3})+", line):
            keep.update({i - 1, i, i + 1})
        elif any(n in line for n in names):
            keep.add(i)
    return "\n".join(lines[i] for i in sorted(keep) if 0 <= i < len(lines))


def page_date(raw_html: str) -> str | None:
    """ページの更新日（なければ公開日）を YYYY-MM-DD で返す。"""
    for key in ("article:modified_time", "og:updated_time", "dateModified",
                "article:published_time", "datePublished"):
        m = re.search(rf'{key}["\']?\s*(?:content=|:)\s*["\'](\d{{4}}-\d{{2}}-\d{{2}})', raw_html)
        if m:
            return m.group(1)
    return None


PAGE_NOTE = (
    "このテキストはファンが運営するまとめサイトの記事です。公式発表を引用している箇所でも、"
    "まとめサイト経由の数字なので kind は「press」にしてください。"
)


def site_metric(page: dict) -> str:
    """まとめサイトの数字の記録先。サイトごとに数字の基準がずれるので混ぜない。

    primary のサイトは「estimate」（ランキングに使う定点観測の値）、それ以外は「site:ホスト名」（比較用）。
    """
    return "estimate" if page.get("primary") else "site:" + urllib.parse.urlparse(page["url"]).netloc


def crawl_pages(conn, groups: list[dict], pages: list[dict]) -> None:
    """groups.json の watch_pages に書いたページを読み、会員数の記述が変わったら取り込む。"""
    import thumbnails
    names = [g["name"] for g in groups]
    for page in pages:
        url = page["url"]
        # 定点観測と比較用を切り替えたときは、以前の扱いで記録した分を消す
        if page.get("primary"):
            conn.execute("DELETE FROM metrics WHERE metric = ?", ("site:" + urllib.parse.urlparse(url).netloc,))
        else:
            conn.execute("DELETE FROM metrics WHERE metric = 'estimate' AND source = ?", (url,))
        conn.commit()
        if not thumbnails.allowed(url):
            print(f"[ページ] {url} は robots.txt で禁止されているため読みません")
            continue
        try:
            _, raw = thumbnails.read_head(url, max_bytes=3_000_000)  # まとめページは長いので全体を読む
        except Exception as e:
            print(f"[ページ] {url} の取得に失敗しました: {e}", file=sys.stderr)
            continue
        text = page_text(raw)
        snippet = page_snippets(text, names)
        print(f"[ページ] {url}: 本文 {len(text)} 文字 / 会員数らしい部分 {len(snippet)} 文字")
        digest = hashlib.sha256(snippet.encode()).hexdigest()
        if not snippet:
            print(f"[ページ] {url} に会員数の記述が見つかりません")
            continue
        state_key = f"page:{site_metric(page)}:{url}"
        if get_state(conn, state_key) == digest:
            print(f"[ページ] {url} は前回から変化がありません")
            continue
        result = translate.ask_json(EXTRACT_SYSTEM + PAGE_NOTE, snippet[:20000], extract_schema(names))
        if result is None:
            continue
        for it in result["items"]:
            it["kind"] = site_metric(page)  # まとめサイトの数字は公式扱いにしない
        # 日付がわからない数字は、読んだ日ではなくページの更新日（なければ公開日）で記録する
        n = save_official(conn, result["items"], url, page_date(raw) or date.today().isoformat())
        print(f"[ページ] {url} から {n} 件を記録しました")
        if n:
            set_state(conn, state_key, digest)
        else:
            # 読み取れなかったときは原因を調べられるよう、AI に渡した部分の先頭を残す（次回もう一度読む）
            print(f"[ページ] 読み取り対象（{len(snippet)} 文字）の先頭: {snippet[:400]!r}")


def crawl_wikipedia(conn, groups: list[dict]) -> None:
    names = [g["name"] for g in groups]
    changed = []
    for g in groups:
        params = urllib.parse.urlencode({
            "action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
            "titles": g["wikipedia"], "format": "json", "formatversion": "2", "redirects": "1",
        })
        try:
            data = get_json(f"https://ja.wikipedia.org/w/api.php?{params}", {"User-Agent": WIKI_UA})
            page = data["query"]["pages"][0]
            wikitext = page["revisions"][0]["slots"]["main"]["content"]
        except Exception as e:
            print(f"[Wikipedia] {g['name']} の取得に失敗しました: {e}", file=sys.stderr)
            continue
        snippet = wiki_snippets(wikitext)
        digest = hashlib.sha256(snippet.encode()).hexdigest()
        if snippet and get_state(conn, "wiki2:" + g["name"]) != digest:
            changed.append((g, snippet, digest))
    if not changed:
        print("[Wikipedia] 会員数の記述に変化はありません")
        return
    for g, snippet, digest in changed:
        result = translate.ask_json(
            EXTRACT_SYSTEM, f"グループ: {g['name']}\n\n{snippet}", extract_schema(names))
        if result is None:
            continue
        # 記述に日付がなければ、変化に気づいた今日の日付で記録する
        items = [it for it in result["items"] if it["group"] == g["name"]]
        url = "https://ja.wikipedia.org/wiki/" + urllib.parse.quote(g["wikipedia"])
        save_official(conn, items, url, date.today().isoformat())
        set_state(conn, "wiki2:" + g["name"], digest)


# ---------------------------------------------------------------- ファンの投稿（会員番号）

def parse_masked_number(text: str) -> int | None:
    """「1234xxx」のように下3桁を伏せた会員番号を、千の位までの数（1234000）にする。

    下3桁を伏せずに書かれていても、同じく千の位で切り捨てて扱う（番号をそのまま残さないため）。
    """
    t = unicodedata.normalize("NFKC", text).strip()  # 全角数字・全角 x を半角に
    if not re.fullmatch(r"[\d,\s]*\d[\d,\s]*[xX*○●?]{0,3}", t):
        return None
    digits = re.sub(r"\D", "", t)
    masked = len(re.findall(r"[xX*○●?]", t))
    num = int(digits) * 10 ** masked
    return num // 1000 * 1000 if num >= 1000 else None


def parse_fan_csv(text: str, names: list[str]) -> list[tuple[str, str, int]]:
    """Google フォームの回答 CSV を (日付, グループ, 会員番号) のリストにする。"""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = rows[0]
    try:
        i_time = next(i for i, h in enumerate(header) if "タイムスタンプ" in h or "Timestamp" in h)
        i_grp = next(i for i, h in enumerate(header) if "グループ" in h)
        i_num = next(i for i, h in enumerate(header) if "番号" in h)
    except StopIteration:
        print("[会員番号] フォームの列名（グループ / 番号）が見つかりません", file=sys.stderr)
        return []
    out = []
    for r in rows[1:]:
        if len(r) <= max(i_time, i_grp, i_num) or r[i_grp] not in names:
            continue
        num = parse_masked_number(r[i_num])
        m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", r[i_time])
        if num is None or not m:
            continue
        if 0 < num <= MAX_COUNT:
            out.append((f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", r[i_grp], num))
    return out


def estimate_member_no(reports: list[tuple[str, str, int]]) -> dict[str, list[tuple[str, int, int]]]:
    """グループごとに、その日までに届いた番号の最大値を推定値とする。

    いたずらで大きすぎる番号が入るのを防ぐため、前日までの推定値の 1.3 倍を超える番号は使わない。
    戻り値: {グループ: [(日付, 推定値, その日の投稿数), ...]}
    """
    result: dict[str, list] = {}
    for grp in sorted({g for _, g, _ in reports}):
        best = None
        by_day: dict[str, list[int]] = {}
        for day, g, num in reports:
            if g == grp:
                by_day.setdefault(day, []).append(num)
        for day in sorted(by_day):
            nums = sorted(by_day[day])
            if best is None:
                # 最初の日は、ほかの投稿から大きく外れた番号を除く
                ok = [n for n in nums if n <= nums[len(nums) // 2] * 1.3]
            else:
                ok = [n for n in nums if n <= best * 1.3]
            if ok:
                best = max(best or 0, max(ok))
            if best:
                result.setdefault(grp, []).append((day, best, len(nums)))
    return result


def crawl_fan_reports(conn, config: dict) -> None:
    url = config.get("fan_form_csv_url")
    if not url:
        print("[会員番号] フォームの CSV URL が未設定です")
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as res:
            text = res.read().decode("utf-8-sig")
    except Exception as e:
        print(f"[会員番号] 取得に失敗しました: {e}", file=sys.stderr)
        return
    names = [g["name"] for g in config["groups"]]
    if text.lstrip().startswith("<"):
        print("[会員番号] CSV ではなくページが返ってきました。スプレッドシートの共有設定を確認してください",
              file=sys.stderr)
        return
    reports = parse_fan_csv(text, names)
    print(f"[会員番号] フォームの回答を読み込みました（有効な回答 {len(reports)} 件）")
    for grp, days in estimate_member_no(reports).items():
        for day, value, n in days:
            record(conn, grp, "member_no", value, f"ファン投稿 {n} 件", day)
        print(f"[会員番号] {grp} 推定 {days[-1][1]:,}（投稿 {sum(d[2] for d in days)} 件）")


# ---------------------------------------------------------------- YouTube / Spotify

def is_group_channel(name: str, title: str) -> bool:
    """チャンネル名がグループ名そのもの（大文字小文字も一致）かどうか。

    「timelesz [タイムレス]」「SUPER EIGHT official」のような後ろの補足は認めるが、
    「News Start」「Supereight」「SUPER EIGHT - Topic」のような別チャンネルは除く。
    """
    pattern = re.escape(name) + r"(\s*[\[(（【].*|\s+(official|Official|OFFICIAL)\b.*)?"
    return re.fullmatch(pattern, title.strip()) is not None


def crawl_youtube(conn, groups: list[dict]) -> None:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        print("[YouTube] YOUTUBE_API_KEY が未設定です")
        return
    api = "https://www.googleapis.com/youtube/v3"
    ids = {}
    confirmed = set()  # groups.json で人が確かめて指定したチャンネル（名前の照合をしない）
    for g in groups:
        if g.get("youtube") == "none":
            continue  # 単独の公式チャンネルがないと確認済み
        cid = g.get("youtube_channel_id")
        if not cid and g.get("youtube_handle"):
            q = urllib.parse.urlencode({"part": "id", "forHandle": g["youtube_handle"], "key": key})
            try:
                found = get_json(f"{api}/channels?{q}").get("items", [])
            except Exception as e:
                print(f"[YouTube] {g['name']} のハンドル {g['youtube_handle']} の確認に失敗しました: {e}", file=sys.stderr)
                continue
            if not found:
                print(f"[YouTube] {g['name']} のハンドル {g['youtube_handle']} が見つかりません")
                continue
            cid = found[0]["id"]
        if cid:
            confirmed.add(cid)
        else:
            cid = get_state(conn, "yt:" + g["name"])
        if not cid:
            # 初回だけ検索してチャンネルを決める（結果は last_run.log で確認できる）
            q = urllib.parse.urlencode({"part": "snippet", "type": "channel", "maxResults": 10,
                                        "q": f"{g['name']} STARTO", "key": key})
            try:
                items = get_json(f"{api}/search?{q}").get("items", [])
            except Exception as e:
                print(f"[YouTube] {g['name']} のチャンネル検索に失敗しました: {e}", file=sys.stderr)
                continue
            match = next((it for it in items if is_group_channel(g["name"], it["snippet"]["title"])), None)
            if not match:
                print(f"[YouTube] {g['name']} の公式チャンネルが見つかりません。groups.json に ID を書いてください")
                continue
            cid = match["snippet"]["channelId"]
            set_state(conn, "yt:" + g["name"], cid)
            print(f"[YouTube] {g['name']} → チャンネル「{match['snippet']['title']}」({cid})")
        ids[cid] = g["name"]
    if not ids:
        return
    q = urllib.parse.urlencode({"part": "statistics,snippet", "id": ",".join(ids), "key": key})
    try:
        items = get_json(f"{api}/channels?{q}").get("items", [])
    except Exception as e:
        print(f"[YouTube] 取得に失敗しました: {e}", file=sys.stderr)
        return
    for it in items:
        name = ids[it["id"]]
        if it["id"] not in confirmed and not is_group_channel(name, it["snippet"]["title"]):
            # 以前の検索で別のチャンネルを選んでいた場合は選び直す
            print(f"[YouTube] {name}: 「{it['snippet']['title']}」は別のチャンネルのため選び直します")
            conn.execute("DELETE FROM watch_state WHERE key = ?", ("yt:" + name,))
            conn.execute("DELETE FROM metrics WHERE grp = ? AND metric = 'youtube'", (name,))
            conn.commit()
            continue
        stats = it["statistics"]
        if stats.get("hiddenSubscriberCount"):
            continue
        subs = int(stats["subscriberCount"])
        if subs < MIN_YOUTUBE_SUBS:
            print(f"[YouTube] {name}: 「{it['snippet']['title']}」は登録者 {subs:,} 人で公式とは考えにくいため使いません")
            conn.execute("DELETE FROM metrics WHERE grp = ? AND metric = 'youtube'", (name,))
            conn.commit()
            continue
        record(conn, ids[it["id"]], "youtube", subs, f"https://www.youtube.com/channel/{it['id']}")
        print(f"[YouTube] {ids[it['id']]} {subs:,} 人（{it['snippet']['title']}）")


def crawl_spotify(conn, groups: list[dict]) -> None:
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret):
        print("[Spotify] SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET が未設定です")
        return
    try:
        token = get_json(
            "https://accounts.spotify.com/api/token",
            {"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode(),
             "Content-Type": "application/x-www-form-urlencoded"},
            b"grant_type=client_credentials",
        )["access_token"]
    except Exception as e:
        print(f"[Spotify] 認証に失敗しました: {e}", file=sys.stderr)
        return
    auth = {"Authorization": f"Bearer {token}"}
    for g in groups:
        aid = g.get("spotify_artist_id") or get_state(conn, "sp:" + g["name"])
        try:
            if not aid:
                q = urllib.parse.urlencode({"q": g["name"], "type": "artist", "market": "JP", "limit": 1})
                found = get_json(f"https://api.spotify.com/v1/search?{q}", auth)["artists"]["items"]
                if not found:
                    continue
                aid = found[0]["id"]
                set_state(conn, "sp:" + g["name"], aid)
                print(f"[Spotify] {g['name']} → アーティスト「{found[0]['name']}」({aid})")
            artist = get_json(f"https://api.spotify.com/v1/artists/{aid}", auth)
        except Exception as e:
            print(f"[Spotify] {g['name']} の取得に失敗しました: {e}", file=sys.stderr)
            continue
        followers = artist.get("followers", {}).get("total")
        if followers is not None:
            record(conn, g["name"], "spotify", int(followers), f"https://open.spotify.com/artist/{aid}")
            print(f"[Spotify] {g['name']} {int(followers):,} 人")


MANUAL_PATH = Path(__file__).resolve().parent / "manual_metrics.json"


def load_manual(conn) -> None:
    """manual_metrics.json に人が書いた数字（出典つき）を毎回取り込む。"""
    if not MANUAL_PATH.exists():
        return
    manual = json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
    for m in manual["entries"]:
        record(conn, m["group"], m["metric"], int(m["value"]), m["source"], m["date"])
        print(f"[手動] {m['group']} {m['metric']} {int(m['value']):,}（{m['date']}）")
    # 出典が確かめられなかった記録を消す
    for m in manual.get("remove", []):
        conn.execute("DELETE FROM metrics WHERE grp = ? AND metric = ?", (m["group"], m["metric"]))
        print(f"[手動] {m['group']} の {m['metric']} を削除（{m['reason']}）")
    conn.commit()


def main() -> int:
    conn = open_db()
    if len(sys.argv) == 4 and sys.argv[1] == "add":
        record(conn, sys.argv[2], "official", int(sys.argv[3]), "手動入力")
        print(f"[公式] {sys.argv[2]} {int(sys.argv[3]):,} 人を記録しました")
        return 0
    config = load_config()
    groups = config["groups"]
    for step in (crawl_news, crawl_wikipedia, crawl_youtube, crawl_spotify):
        step(conn, groups)
    crawl_pages(conn, groups, config.get("watch_pages", []))
    load_manual(conn)
    crawl_fan_reports(conn, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
