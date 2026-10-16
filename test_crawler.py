import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_site
import crawler
import digest
import thumbnails
import fanclub

SAMPLE = """<?xml version="1.0"?><rss><channel>
<item><title>테스트 제목 - 테스트일보</title><link>https://example.com/a</link>
<pubDate>Wed, 15 Oct 2026 10:00:00 GMT</pubDate><source url="https://example.com">테스트일보</source></item>
</channel></rss>""".encode()


BING = """<?xml version="1.0"?><rss xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss"><channel>
<item><title>테스트 제목</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url=https%3a%2f%2fnews.example.kr%2fa1&amp;c=1</link>
<pubDate>Wed, 15 Oct 2026 12:00:00 GMT</pubDate><News:Source>예시뉴스</News:Source>
<News:Image>http://www.bing.com/th?id=OVFT.abc&amp;pid=News</News:Image></item>
</channel></rss>""".encode()


class CrawlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = crawler.open_db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_parse_marks_foreign_titles_untranslated(self):
        items = crawler.parse_rss(SAMPLE, "ko")
        self.assertEqual(items[0]["title"], "테스트 제목")
        self.assertIsNone(items[0]["title_ja"])
        self.assertEqual(crawler.parse_rss(SAMPLE, "ja")[0]["title_ja"], "테스트 제목")

    def test_save_dedupes_and_translates(self):
        items = crawler.parse_rss(SAMPLE, "ko")
        self.assertEqual(crawler.save(self.conn, items), 1)
        self.assertEqual(crawler.save(self.conn, items), 0)
        with mock.patch("translate.to_japanese", return_value=["テスト見出し"]):
            self.assertEqual(crawler.translate_pending(self.conn), 1)
        out = Path(self.tmp.name) / "data.json"
        build_site.build(self.conn, out)
        news = json.loads(out.read_text(encoding="utf-8"))["news"][0]
        self.assertEqual(news["title"], "テスト見出し")
        self.assertEqual(news["original"], "테스트 제목")

    def test_translation_skipped_keeps_pending(self):
        crawler.save(self.conn, crawler.parse_rss(SAMPLE, "ko"))
        with mock.patch("translate.to_japanese", return_value=None):
            self.assertEqual(crawler.translate_pending(self.conn), 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM articles WHERE title_ja IS NULL").fetchone()[0], 1)

    def test_metrics_one_row_per_day(self):
        fanclub.record(self.conn, "A", "youtube", 100, "x", "2026-10-15")
        fanclub.record(self.conn, "A", "youtube", 110, "x", "2026-10-16")
        fanclub.record(self.conn, "A", "youtube", 120, "x", "2026-10-16")
        out = Path(self.tmp.name) / "data.json"
        yt = build_site.build(self.conn, out)["fanclub"]["A"]["youtube"]
        self.assertEqual([p["value"] for p in yt], [100, 120])

    def test_wiki_snippets_keeps_member_sentences_only(self):
        text = "結成は2012年。ファンクラブ会員数は2024年に300万人を突破した。\nCDを100万枚売った。"
        self.assertEqual(fanclub.wiki_snippets(text), "ファンクラブ会員数は2024年に300万人を突破した。")

    def test_save_official_rejects_bad_numbers_and_dates(self):
        items = [{"group": "A", "count": 3000000, "date": "2024-05-01", "quote": "q"},
                 {"group": "A", "count": 999999999, "date": "", "quote": "q"},
                 {"group": "B", "count": 500, "date": "去年", "quote": "q"}]
        self.assertEqual(fanclub.save_official(self.conn, items, "src", "2026-10-17"), 2)
        rows = self.conn.execute("SELECT date, grp, value FROM metrics ORDER BY grp").fetchall()
        self.assertEqual(rows, [("2024-05-01", "A", 3000000), ("2026-10-17", "B", 500)])

    def test_parse_masked_number(self):
        self.assertEqual(fanclub.parse_masked_number("1234xxx"), 1234000)
        self.assertEqual(fanclub.parse_masked_number("1,234,***"), 1234000)
        self.assertEqual(fanclub.parse_masked_number("1234567"), 1234000)
        self.assertIsNone(fanclub.parse_masked_number("abc"))
        self.assertIsNone(fanclub.parse_masked_number("12"))

    def test_digest_keeps_only_recent_and_valid_ids(self):
        from datetime import datetime, timezone
        crawler.save(self.conn, crawler.parse_rss(SAMPLE, "ko"))  # 2026-10-15 の記事
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        articles = digest.recent_articles(self.conn, now)
        self.assertEqual(len(articles), 1)
        self.assertEqual(digest.recent_articles(self.conn, datetime(2026, 10, 30, tzinfo=timezone.utc)), [])
        fake = {"topics": [{"region": "海外", "title": "T", "summary": "S", "points": ["a", "b", "c", "d"], "article_ids": [0, 5]},
                           {"region": "国内", "title": "空", "summary": "S", "points": [], "article_ids": [9]}]}
        with mock.patch("translate.ask_json", return_value=fake):
            d = digest.build_digest(articles)
        self.assertEqual(len(d["topics"]), 1)
        self.assertEqual(d["topics"][0]["articles"][0]["link"], "https://example.com/a")
        self.assertEqual(d["topics"][0]["points"], ["a", "b", "c"])

    def test_bing_gives_real_url_and_https_image(self):
        it = crawler.parse_bing_rss(BING, "ko")[0]
        self.assertEqual(it["link"], "https://news.example.kr/a1")
        self.assertEqual(it["image"], "https://www.bing.com/th?id=OVFT.abc&pid=News")
        self.assertEqual(it["source"], "예시뉴스")

    def test_bing_with_bare_ampersand_in_namespace(self):
        broken = BING.replace(b'?q=x&amp;format=rss', b'?q=x&format=rss')
        self.assertEqual(len(crawler.parse_bing_rss(broken, "ko")), 1)

    def test_same_headline_from_two_sources_is_saved_once_with_image(self):
        google = crawler.parse_rss(SAMPLE, "ko")   # 「테스트 제목」画像なし
        self.assertEqual(crawler.save(self.conn, google), 1)
        self.assertEqual(crawler.save(self.conn, crawler.parse_bing_rss(BING, "ko")), 0)
        rows = self.conn.execute("SELECT link, image FROM articles").fetchall()
        self.assertEqual(rows, [("https://example.com/a", "https://www.bing.com/th?id=OVFT.abc&pid=News")])

    def test_truncated_headline_is_same_article(self):
        full = [{"title": "중島健人「人集まらなかったらどうしよう」アジアツアーの舞台裏で見せた不安と葛藤",
                 "link": "https://g/1", "source": "s", "published": "2026-10-15", "lang": "ja",
                 "title_ja": "x", "image": None}]
        cut = [dict(full[0], title="중島健人「人集まらなかったらどうしよう」アジアツアーの舞台 ...",
                    link="https://b/1", image="https://img/1")]
        self.assertEqual(crawler.save(self.conn, full), 1)
        self.assertEqual(crawler.save(self.conn, cut), 0)
        self.assertEqual(self.conn.execute("SELECT image FROM articles").fetchall(), [("https://img/1",)])
        # 以前の版で二重に入ったものは整理で消える
        self.conn.execute("INSERT INTO articles (link, title, lang, title_key) VALUES (?, ?, 'ja', ?)",
                          ("https://b/2", cut[0]["title"], crawler.title_key(cut[0]["title"])))
        self.assertEqual(crawler.remove_duplicates(self.conn), 1)
        self.assertEqual(self.conn.execute("SELECT link FROM articles").fetchall(), [("https://g/1",)])

    def test_page_snippets_keep_table_rows(self):
        html = ("<html><script>var a=1;</script><h2>会員数一覧</h2><table>"
                "<tr><td>Snow Man</td></tr><tr><td>約300万人</td></tr>"
                "<tr><td>プロフィール</td><td>2012年結成</td></tr></table><p>関係ない文章</p></html>")
        text = fanclub.page_text(html)
        self.assertNotIn("var a", text)
        snip = fanclub.page_snippets(text, ["Snow Man"])
        self.assertIn("Snow Man\n約300万人", snip)
        self.assertNotIn("関係ない", snip)

    def test_find_image(self):
        page = '<head><meta content="/img/a.jpg?x=1&amp;y=2" property="og:image"></head>'
        self.assertEqual(thumbnails.find_image(page, "https://site.example/news/1"),
                         "https://site.example/img/a.jpg?x=1&y=2")
        self.assertIsNone(thumbnails.find_image('<meta property="og:image" content="http://a/b.jpg">', "http://a/"))
        self.assertIsNone(thumbnails.find_image("<head></head>", "https://a/"))

    def test_is_group_channel(self):
        self.assertTrue(fanclub.is_group_channel("timelesz", "timelesz [タイムレス]"))
        self.assertTrue(fanclub.is_group_channel("Hey! Say! JUMP", "Hey! Say! JUMP"))
        self.assertFalse(fanclub.is_group_channel("NEWS", "Official iNews"))
        self.assertFalse(fanclub.is_group_channel("NEWS", "TBS NEWS DIG"))
        self.assertFalse(fanclub.is_group_channel("SUPER EIGHT", "SUPER EIGHT - Topic"))
        self.assertFalse(fanclub.is_group_channel("NEWS", "News Start"))
        self.assertFalse(fanclub.is_group_channel("SUPER EIGHT", "Supereight"))
        self.assertTrue(fanclub.is_group_channel("SUPER EIGHT", "SUPER EIGHT official"))

    def test_fan_csv_and_estimate_ignores_outliers(self):
        csv_text = ("タイムスタンプ,グループ,会員番号\n"
                    "2026/10/16 9:00:00,A,1000xxx\n"
                    "2026/10/16 10:00:00,A,１１００ｘｘｘ\n"
                    "2026/10/16 11:00:00,A,9000xxx\n"   # いたずら（初日の外れ値）
                    "2026/10/17 9:00:00,A,\"1,200,345\"\n"   # 伏せずに書いても千の位で切り捨て
                    "2026/10/17 9:30:00,A,5000***\n"        # いたずら（前日の1.3倍超）
                    "2026/10/17 9:35:00,A,わかりません\n"
                    "2026/10/17 9:40:00,Z,1300xxx\n")       # 一覧にないグループ
        reports = fanclub.parse_fan_csv(csv_text, ["A"])
        self.assertEqual(len(reports), 5)
        est = fanclub.estimate_member_no(reports)["A"]
        self.assertEqual(est, [("2026-10-16", 1100000, 3), ("2026-10-17", 1200000, 2)])

if __name__ == "__main__":
    unittest.main()
