import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_site
import crawler
import fanclub

SAMPLE = """<?xml version="1.0"?><rss><channel>
<item><title>테스트 제목 - 테스트일보</title><link>https://example.com/a</link>
<pubDate>Wed, 15 Oct 2026 10:00:00 GMT</pubDate><source url="https://example.com">테스트일보</source></item>
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

    def test_fanclub_one_row_per_day(self):
        self.assertEqual(fanclub.extract_count("会員数 1,234,567人", r"会員数\s*([\d,]+)"), 1234567)
        fanclub.record(self.conn, "A", 100, "x", "2026-10-15")
        fanclub.record(self.conn, "A", 110, "x", "2026-10-16")
        fanclub.record(self.conn, "A", 120, "x", "2026-10-16")
        out = Path(self.tmp.name) / "data.json"
        fc = build_site.build(self.conn, out)["fanclub"]["A"]
        self.assertEqual([p["count"] for p in fc], [100, 120])


if __name__ == "__main__":
    unittest.main()
