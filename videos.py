"""YouTube の動画を集める（公式チャンネルの新着と、名前で検索した動画）。

YouTube Data API（環境変数 YOUTUBE_API_KEY）を使う。動画そのものは保存せず、
タイトル・チャンネル名・公開日時・サムネイル・リンクだけを「動画」として記事の表に入れる。

使い方:
    python3 videos.py
"""
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crawler import open_db, save, translate_pending
from fanclub import get_json, get_state

API = "https://www.googleapis.com/youtube/v3"
NAME = "中島健人"
QUERIES = ["中島健人", "ケンティー", "Kento Nakajima", "나카지마 켄토"]
DAYS = 14


def guess_lang(text: str) -> str:
    if re.search(r"[가-힣]", text):
        return "ko"
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    if re.search(r"[฀-๿]", text):
        return "th"
    if re.search(r"[一-鿿]", text):
        return "zh"
    return "en"


def to_item(video_id: str, sn: dict) -> dict:
    title = sn["title"]
    lang = guess_lang(title)
    thumb = (sn.get("thumbnails", {}).get("high") or sn.get("thumbnails", {}).get("medium") or {}).get("url")
    return {"title": title, "link": f"https://www.youtube.com/watch?v={video_id}",
            "source": f"YouTube・{sn.get('channelTitle', '')}", "published": sn.get("publishedAt", ""),
            "lang": lang, "title_ja": title if lang == "ja" else None, "image": thumb, "kind": "video",
            "desc": sn.get("description", "")}


def official_uploads(key: str) -> list[dict]:
    """公式チャンネル（groups.json に登録したハンドル）の新着動画。"""
    groups = json.loads((Path(__file__).resolve().parent / "groups.json").read_text(encoding="utf-8"))["groups"]
    me = next((g for g in groups if g["name"] == NAME), {})
    params = {"part": "contentDetails", "key": key}
    if me.get("youtube_channel_id"):
        params["id"] = me["youtube_channel_id"]
    elif me.get("youtube_handle"):
        params["forHandle"] = me["youtube_handle"]
    else:
        return []
    ch = get_json(f"{API}/channels?{urllib.parse.urlencode(params)}").get("items", [])
    if not ch:
        return []
    uploads = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    q = urllib.parse.urlencode({"part": "snippet", "playlistId": uploads, "maxResults": 25, "key": key})
    return [to_item(it["snippet"]["resourceId"]["videoId"], it["snippet"])
            for it in get_json(f"{API}/playlistItems?{q}").get("items", [])]


def searched(key: str, query: str) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = urllib.parse.urlencode({"part": "snippet", "type": "video", "order": "date", "maxResults": 50,
                                "publishedAfter": since, "q": query, "key": key})
    return [to_item(it["id"]["videoId"], it["snippet"]) for it in get_json(f"{API}/search?{q}").get("items", [])]


def main() -> int:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        print("[動画] YOUTUBE_API_KEY が未設定です")
        return 0
    conn = open_db()
    jobs = [("公式チャンネル", lambda: official_uploads(key))] + [(f"検索「{q}」", (lambda q=q: searched(key, q))) for q in QUERIES]
    for name, job in jobs:
        try:
            items = job()
        except Exception as e:
            print(f"[動画] {name} の取得に失敗しました: {e}", file=sys.stderr)
            continue
        print(f"[動画] {name} 取得 {len(items)} 件 / 新規 {save(conn, items)} 件")
    print(f"[動画] 翻訳 {translate_pending(conn)} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
