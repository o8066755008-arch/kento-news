import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_site
import crawler
import digest
import notify
import events
import videos
import starto
import thumbnails
import fanclub

SAMPLE = """<?xml version="1.0"?><rss><channel>
<item><title>나카지마 켄토 테스트 제목 - 테스트일보</title><link>https://example.com/a</link>
<pubDate>Wed, 15 Oct 2026 10:00:00 GMT</pubDate><source url="https://example.com">테스트일보</source></item>
</channel></rss>""".encode()


BING = """<?xml version="1.0"?><rss xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss"><channel>
<item><title>나카지마 켄토 테스트 제목</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url=https%3a%2f%2fnews.example.kr%2fa1&amp;c=1</link>
<pubDate>Wed, 15 Oct 2026 12:00:00 GMT</pubDate><News:Source>예시뉴스</News:Source>
<News:Image>http://www.bing.com/th?id=OVFT.abc&amp;pid=News</News:Image></item>
</channel></rss>""".encode()


def notify_parse(text):
    """form_subscriptions の CSV の読み方だけを、通信なしで試す。"""
    with mock.patch("urllib.request.urlopen") as op:
        op.return_value.__enter__.return_value.read.return_value = text.encode()
        return notify.form_subscriptions("https://x")


class CrawlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = crawler.open_db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_parse_marks_foreign_titles_untranslated(self):
        items = crawler.parse_rss(SAMPLE, "ko")
        self.assertEqual(items[0]["title"], "나카지마 켄토 테스트 제목")
        self.assertIsNone(items[0]["title_ja"])
        self.assertEqual(crawler.parse_rss(SAMPLE, "ja")[0]["title_ja"], "나카지마 켄토 테스트 제목")

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
        self.assertEqual(news["original"], "나카지마 켄토 테스트 제목")

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
        google = crawler.parse_rss(SAMPLE, "ko")   # 「나카지마 켄토 테스트 제목」画像なし
        self.assertEqual(crawler.save(self.conn, google), 1)
        self.assertEqual(crawler.save(self.conn, crawler.parse_bing_rss(BING, "ko")), 0)
        rows = self.conn.execute("SELECT link, image FROM articles").fetchall()
        self.assertEqual(rows, [("https://example.com/a", "https://www.bing.com/th?id=OVFT.abc&pid=News")])

    def test_truncated_headline_is_same_article(self):
        full = [{"title": "中島健人「人集まらなかったらどうしよう」アジアツアーの舞台裏で見せた不安と葛藤",
                 "link": "https://g/1", "source": "s", "published": "2026-10-15", "lang": "ja",
                 "title_ja": "x", "image": None}]
        cut = [dict(full[0], title="中島健人「人集まらなかったらどうしよう」アジアツアーの舞台 ...",
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

    def test_page_snippets_keep_plain_number_rows(self):
        html = "<table><tr><td>Snow Man</td><td>1,942,000</td></tr><tr><td>中島健人</td><td>100,000</td></tr></table>"
        snip = fanclub.page_snippets(fanclub.page_text(html), ["Snow Man", "中島健人"])
        self.assertIn("1,942,000", snip)
        self.assertIn("100,000", snip)

    def test_page_date_prefers_modified(self):
        raw = ('<meta property="article:published_time" content="2026-05-01T10:00:00+09:00">'
               '<meta property="article:modified_time" content="2026-10-10T08:00:00+09:00">')
        self.assertEqual(fanclub.page_date(raw), "2026-10-10")
        self.assertEqual(fanclub.page_date('{"datePublished":"2026-05-20T00:00:00"}'), "2026-05-20")
        self.assertIsNone(fanclub.page_date("<html></html>"))

    def test_notify_changes(self):
        prev = {"topics": ["国内|A"], "youtube": 258000, "fc": 100000, "member_no": 100000,
                "fc_rank": 14, "yt_rank": 14}
        cur = dict(prev, topics=["国内|A", "海外|ソウル公演"], youtube=261000, fc_rank=13)
        lines = notify.changes(prev, cur, {})
        self.assertEqual(lines, ["🌏 ソウル公演", "▶️ YouTube 登録者が 26万人を突破", "📈 FC 会員数の順位 14位 → 13位"])
        self.assertEqual(notify.changes(prev, dict(prev, youtube=259000), {}), [])
        self.assertEqual(notify.changes(prev, cur, {"new_topics": False, "rank_change": False}),
                         ["▶️ YouTube 登録者が 26万人を突破"])

    def test_notify_ranks_skip_ended_groups(self):
        for grp, metric, v in [("嵐", "press", 3000000), ("Snow Man", "estimate", 1942000),
                               ("中島健人", "estimate", 100000), ("中島健人", "member_no", 100000),
                               ("中島健人", "youtube", 258000), ("NEWS", "youtube", 293000)]:
            fanclub.record(self.conn, grp, metric, v, "s", "2026-10-10")
        st = notify.current_state(self.conn, [{"name": "嵐", "ended": True}])
        self.assertEqual((st["fc"], st["fc_rank"], st["yt_rank"]), (100000, 2, 2))

    def test_events_saved_once_and_old_ones_dropped(self):
        events.open_events(self.conn)
        from datetime import date, timedelta
        soon = (date.today() + timedelta(days=10)).isoformat()
        old = (date.today() - timedelta(days=400)).isoformat()
        evs = [{"title": "ソウル公演", "kind": "ライブ", "date": soon, "date_text": "x", "place": "ソウル",
                "region": "海外", "source_id": 0},
               {"title": "昔の公演", "kind": "ライブ", "date": old, "date_text": "x", "place": "",
                "region": "国内", "source_id": 0},
               {"title": "日付未定のオフ会", "kind": "オフ会", "date": "未定", "date_text": "", "place": "",
                "region": "国内", "source_id": 0},
               {"title": "番号が変", "kind": "その他", "date": "", "date_text": "", "place": "",
                "region": "国内", "source_id": 9}]
        src = [{"url": "https://e/1", "source": "s"}]
        self.assertEqual(events.save_events(self.conn, evs, src), 2)
        self.assertEqual(events.save_events(self.conn, evs, src), 0)
        self.assertTrue(events.EVENT_WORDS.search("中島健人、アジアツアー開催決定"))
        self.assertTrue(events.EVENT_WORDS.search("나카지마 켄토 팬미팅"))
        self.assertFalse(events.EVENT_WORDS.search("中島健人のインタビュー"))

    def test_event_before_article_date_is_dropped(self):
        events.open_events(self.conn)
        from datetime import date, timedelta
        pub = date.today().isoformat()
        before = (date.today() - timedelta(days=20)).isoformat()
        after = (date.today() + timedelta(days=20)).isoformat()
        evs = [{"title": "もう始まった番組", "kind": "出演", "date": before, "date_text": "", "place": "",
                "region": "国内", "source_id": 0},
               {"title": "これからのライブ", "kind": "ライブ", "date": after, "date_text": "", "place": "",
                "region": "国内", "source_id": 0}]
        n = events.save_events(self.conn, evs, [{"url": "u", "source": "s", "published": pub + "T00:00:00"}])
        self.assertEqual(n, 1)

    def test_same_event_keeps_more_trusted_source(self):
        events.open_events(self.conn)
        from datetime import date, timedelta
        day = (date.today() + timedelta(days=30)).isoformat()
        e = lambda t: {"title": t, "kind": "ライブ", "date": day, "date_text": "x", "place": "",
                       "region": "国内", "year_certain": True, "source_id": 0}
        self.assertEqual(events.save_events(self.conn, [e("東京ドーム公演 決定")], [{"url": "news", "source": "n"}]), 1)
        self.assertEqual(events.save_events(self.conn, [e("中島健人 東京ドーム公演")],
                                            [{"url": "starto", "source": "s", "tier": 1, "label": "公式"}]), 0)
        self.assertEqual(self.conn.execute("SELECT url, tier, source_label FROM events").fetchall(),
                         [("starto", 1, "公式")])

    def test_duplicate_events_merged(self):
        events.open_events(self.conn)
        from datetime import date, timedelta
        day = (date.today() + timedelta(days=9)).isoformat()
        e = lambda t, d: {"title": t, "kind": "出演", "date": d, "date_text": "", "place": "",
                          "region": "国内", "year_certain": True, "source_id": 0}
        src = [{"url": "u", "source": "s"}]
        self.assertEqual(events.save_events(self.conn, [e("第39回東京国際映画祭（ナビゲーター）", "")], src), 1)
        self.assertEqual(events.save_events(self.conn, [e("第39回東京国際映画祭 フェスティバル・ナビゲーター", day)], src), 0)
        self.assertEqual(self.conn.execute("SELECT date FROM events").fetchall(), [(day,)])
        self.assertEqual(events.save_events(self.conn, [e("IDOL1ST KENTY ASIA TOUR 台北公演", day)], src), 1)
        self.assertEqual(events.save_events(self.conn, [e("中島健人 台北ソロコンサート", day)], src), 1)  # 共通語なしは別扱い

    def test_with_links_keeps_detail_urls(self):
        raw = '<li><a href="/program/123/">日曜日の初耳学</a> 10/18(日) 22:00</li><a href="#top">上へ</a>'
        text = fanclub.page_text(events.with_links(raw, "https://thetv.jp/person/1/"))
        self.assertIn("日曜日の初耳学 ［link: https://thetv.jp/program/123/］", text)
        self.assertNotIn("#top", text)

    def test_reruns_and_local_station_broadcasts_are_dropped(self):
        events.open_events(self.conn)
        from datetime import date, timedelta
        day = (date.today() + timedelta(days=3)).isoformat()
        e = lambda t, st, fr: {"title": t, "kind": "出演", "date": day, "date_text": "", "place": "",
                               "region": "国内", "year_certain": True, "station": st, "first_run": fr,
                               "link": "", "source_id": 0}
        src = [{"url": "u", "source": "s", "tier": 2, "label": "番組表"}]
        n = events.save_events(self.conn, [e("相葉マナブ", "テレビ朝日", False), e("アンビリバボーSP", "SAGATV", True),
                                           e("日曜日の初耳学", "TBS", True), e("新番組", "", True)], src)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(r[0] for r in self.conn.execute("SELECT title FROM events")), ["新番組", "日曜日の初耳学"])

    def test_reminders_once_before_broadcast_and_ticket_deadline(self):
        from datetime import datetime
        events.open_events(self.conn)
        rows = [("a", "出演", "2026-10-25", "22:00", "日曜日の初耳学"),
                ("b", "チケット", "2026-10-26", "23:59", "東京公演 FC先行 申込締切"),
                ("c", "発売日", "2026-10-26", "", "新曲発売"),
                ("d", "出演", "2026-10-25", "", "時刻なしの出演")]
        for eid, kind, day, hhmm, title in rows:
            self.conn.execute("INSERT INTO events (id, kind, date, time, title, date_text) VALUES (?, ?, ?, ?, ?, '')",
                              (eid, kind, day, hhmm, title))
        now = datetime(2026, 10, 25, 21, 10)
        lines = notify.reminders(self.conn, now, {})
        self.assertEqual(sorted(lines), sorted(["📺 まもなく 22:00〜 日曜日の初耳学", "🎫 明日 23:59 東京公演 FC先行 申込締切",
                                                "💿 明日 新曲発売", "📺 今日 時刻なしの出演"]))
        self.assertEqual(notify.reminders(self.conn, now, {}), [])  # 2回目は送らない
        self.assertEqual(notify.reminders(self.conn, datetime(2026, 10, 26, 6, 0), {}),
                         ["🎫 今日 23:59 東京公演 FC先行 申込締切"])

    def test_video_items(self):
        self.assertEqual([videos.guess_lang(t) for t in ["中島健人のライブ", "나카지마 켄토", "中島健人演唱會", "Kento live"]],
                         ["ja", "ko", "zh", "en"])
        it = videos.to_item("abc", {"title": "나카지마 켄토 팬미팅", "channelTitle": "ch", "publishedAt": "2026-10-18T12:00:00Z",
                                    "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/abc/hq.jpg"}}})
        self.assertEqual((it["link"], it["lang"], it["title_ja"], it["kind"]),
                         ("https://www.youtube.com/watch?v=abc", "ko", None, "video"))
        crawler.save(self.conn, [it])
        self.assertEqual(self.conn.execute("SELECT kind, image FROM articles").fetchall(),
                         [("video", "https://i.ytimg.com/vi/abc/hq.jpg")])

    def test_yahoo_parse(self):
        from datetime import datetime
        html = ('<li><a href="https://news.yahoo.co.jp/articles/abc123def?source=rss" class="x">'
                '<div><p>中島健人、新ドラマで主演決定</p><span>スポニチ</span><time>10/22(水) 9:15</time></div></a></li>'
                '<a href="https://news.yahoo.co.jp/articles/abc123def">重複</a>').encode()
        items = crawler.parse_yahoo(html, now=datetime(2026, 10, 23))
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["title"], items[0]["source"], items[0]["link"]),
                         ("中島健人、新ドラマで主演決定", "スポニチ", "https://news.yahoo.co.jp/articles/abc123def"))
        self.assertTrue(items[0]["published"].startswith("2026-10-22T09:15"))

    def test_starto_queries_and_kept_apart(self):
        qs = dict(starto.queries([{"name": "中島健人"}, {"name": "NEWS"}, {"name": "Snow Man"}]))
        self.assertNotIn("中島健人", qs)
        self.assertIn("加藤シゲアキ", qs["NEWS"])
        self.assertEqual(qs["Snow Man"], '"Snow Man" when:3d')
        it = {"title": "Snow Man 新曲", "link": "https://s/1", "source": "s", "published": "2026-10-22T00:00:00+00:00",
              "lang": "ja", "title_ja": "Snow Man 新曲", "kind": "starto", "grp": "Snow Man"}
        crawler.save(self.conn, [it])
        from datetime import datetime, timezone
        self.assertEqual(digest.recent_articles(self.conn, datetime(2026, 10, 23, tzinfo=timezone.utc)), [])

    def test_only_kento_articles_are_kept(self):
        mk = lambda t, **kw: dict({"title": t, "link": "https://x/" + t, "source": "s", "published": "2026-10-22",
                                   "lang": "ja", "title_ja": t}, **kw)
        n = crawler.save(self.conn, [mk("中島健人、新ドラマ主演"), mk("BTS Vの振付騒動"), mk("ケンティーの神対応"),
                                     mk("『SとX』配信開始"), mk("別の話題", desc="中島健人さんも出演"),
                                     mk("Snow Man 新曲", kind="starto", grp="Snow Man")])
        self.assertEqual(n, 5)
        self.conn.execute("INSERT INTO articles (link, title, lang) VALUES ('https://old', '関係ない昔の記事', 'ja')")
        self.assertEqual(crawler.purge_unrelated(self.conn), 2)  # 昔の記事と、要約でだけ名前が出た記事

    def test_notify_new_event(self):
        prev = {"topics": [], "events": ["a|11/3 ライブ"], "youtube": None, "fc": None, "member_no": None,
                "fc_rank": None, "yt_rank": None}
        cur = dict(prev, events=["a|11/3 ライブ", "b|12/1 ファンミ"])
        self.assertEqual(notify.changes(prev, cur, {}), ["🎤 12/1 ファンミ"])

    def test_subscriptions_from_secret_and_form_without_duplicates_or_dead(self):
        a = {"endpoint": "https://fcm.googleapis.com/a", "keys": {"p256dh": "x", "auth": "y"}}
        b = {"endpoint": "https://web.push.apple.com/b", "keys": {"p256dh": "x", "auth": "y"}}
        c = {"endpoint": "https://fcm.googleapis.com/c", "keys": {"p256dh": "x", "auth": "y"}}
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["タイムスタンプ", "宛先"])
        for row in (["2026/10/17 9:00:00", json.dumps(a)], ["2026/10/17 9:01:00", json.dumps(b)],
                    ["2026/10/17 9:02:00", "いたずら"], ["2026/10/17 9:03:00", json.dumps(c)]):
            w.writerow(row)
        self.conn.execute("INSERT INTO watch_state VALUES ('push_dead', ?)", (json.dumps([c["endpoint"]]),))
        with mock.patch.dict("os.environ", {"PUSH_SUBSCRIPTIONS": json.dumps([a])}), \
             mock.patch("notify.form_subscriptions", return_value=notify_parse(buf.getvalue())):
            subs = notify.all_subscriptions(self.conn, {"notify": {"subscribe_csv_url": "https://x"}})
        self.assertEqual([x["endpoint"] for x in subs], [a["endpoint"], b["endpoint"]])

    def test_decode_html_shift_jis(self):
        body = '<meta charset="Shift_JIS"><p>中島健人 出演</p>'.encode("cp932")
        self.assertIn("中島健人", thumbnails.decode_html(body, None))
        self.assertIn("中島健人", thumbnails.decode_html("中島健人".encode("euc-jp"), "euc-jp"))

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
