"""記事のサムネイル画像を補う。

記事ページに書かれている共有用の代表画像（og:image / twitter:image）の URL だけを読み取って保存する。
画像そのものはダウンロード・保存しない。各サイトの robots.txt で禁止されていれば読まない。

使い方:
    python3 thumbnails.py
"""
import base64
import html
import re
import sys
import urllib.parse
import urllib.request
import urllib.robotparser

from crawler import USER_AGENT, open_db

MAX_PER_RUN = 80
MAX_BYTES = 400_000  # <head> が読めれば十分なので先頭だけ読む

_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def allowed(url: str) -> bool:
    parts = urllib.parse.urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as res:
                rp.parse(res.read().decode("utf-8", errors="replace").splitlines())
        except Exception:
            rp.parse([])  # robots.txt がなければ許可とみなす
        _robots[base] = rp
    return _robots[base].can_fetch(USER_AGENT, url)


def read_head(url: str) -> tuple[str, str]:
    """ページの先頭を読み、(最終的な URL, HTML) を返す。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.geturl(), res.read(MAX_BYTES).decode("utf-8", errors="replace")


def decode_google_news(link: str) -> str | None:
    """Google ニュースのリンクから記事の URL を取り出す（古い形式のリンクだけ取り出せる）。"""
    m = re.search(r"/articles/([A-Za-z0-9_-]+)", link)
    if not m:
        return None
    raw = m.group(1)
    try:
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        return None
    found = re.search(rb"https?://[\x21-\x7e]+", data)
    return found.group(0).decode() if found else None


def find_image(page: str, page_url: str) -> str | None:
    for prop in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
        for pattern in (
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{re.escape(prop)}["\']',
        ):
            m = re.search(pattern, page, re.IGNORECASE)
            if m:
                url = urllib.parse.urljoin(page_url, html.unescape(m.group(1).strip()))
                if url.startswith("https://"):  # アプリは https なので http の画像は表示できない
                    return url
    return None


def article_url(link: str) -> str | None:
    if "news.google.com" not in link:
        return link
    decoded = decode_google_news(link)
    if decoded:
        return decoded
    # 新しい形式のリンクは記事ページへの転送先を Google のページから探す
    try:
        _, page = read_head(link)
    except Exception:
        return None
    m = re.search(r'data-n-au="(https?://[^"]+)"', page)
    return html.unescape(m.group(1)) if m else None


def main() -> int:
    conn = open_db()
    rows = conn.execute(
        "SELECT link FROM articles WHERE image IS NULL AND COALESCE(image_tried, 0) = 0 "
        "ORDER BY published DESC LIMIT ?", (MAX_PER_RUN,)
    ).fetchall()
    found = 0
    for (link,) in rows:
        image = None
        url = article_url(link)
        if url and allowed(url):
            try:
                final_url, page = read_head(url)
                image = find_image(page, final_url)
            except Exception:
                pass
        conn.execute("UPDATE articles SET image = ?, image_tried = 1 WHERE link = ?", (image, link))
        conn.commit()
        found += bool(image)
    print(f"[サムネイル] {len(rows)} 件を確認 / 画像あり {found} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
