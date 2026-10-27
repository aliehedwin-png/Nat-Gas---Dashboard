import os, sys, unittest
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import news_alerts as n

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>OPEC+ agrees surprise output cut as Hormuz tensions escalate - Reuters</title><link>https://example.com/a</link>
<pubDate>Tue, 27 Oct 2026 14:00:00 GMT</pubDate><source url="https://reuters.com">Reuters</source></item>
<item><title>Natural gas futures fall on mild weather forecasts</title><link>https://example.com/b</link>
<pubDate>Tue, 27 Oct 2026 13:00:00 GMT</pubDate><description>&lt;p&gt;Henry Hub slides&lt;/p&gt;</description></item>
<item><title>Apple unveils new iPhone</title><link>https://example.com/c</link><pubDate>Tue, 27 Oct 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>EIA reports crude draw</title>
<link href="https://example.com/d"/><updated>2026-10-27T15:00:00Z</updated></entry></feed>"""


class News(unittest.TestCase):
    def test_parse_rss_and_google_source(self):
        items = n.parse_feed(RSS, "test")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "OPEC+ agrees surprise output cut as Hormuz tensions escalate")
        self.assertEqual(items[0]["source"], "Reuters (via Google News)")
        self.assertEqual(items[0]["ts"], int(datetime(2026, 10, 27, 14, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(items[1]["summary"], "Henry Hub slides")

    def test_parse_atom(self):
        items = n.parse_feed(ATOM, "atom")
        self.assertEqual(items[0]["link"], "https://example.com/d")
        self.assertIsNotNone(items[0]["ts"])

    def test_classify(self):
        c = n.classify("OPEC+ agrees surprise output cut as Hormuz tensions escalate")
        self.assertEqual(c["markets"], ["CL"])
        self.assertIn("OPEC+", c["topics"])
        self.assertIn("Middle East / Hormuz", c["topics"])
        self.assertEqual(c["bias"], "bullish")
        self.assertTrue(c["urgent"])
        g = n.classify("Natural gas futures fall on mild weather forecasts")
        self.assertEqual(g["markets"], ["NG"])
        self.assertEqual(g["bias"], "bearish")
        self.assertIn("Weather", g["topics"])
        self.assertIsNone(n.classify("Apple unveils new iPhone"))
        self.assertEqual(n.classify("Producers increase supply")  , None)
        self.assertEqual(n.classify("US LNG feedgas hits record")["markets"], ["NG"])
        self.assertEqual(n.classify("Oil prices steady ahead of data")["bias"], "neutral")

    def test_names_and_deescalation(self):
        self.assertIsNone(n.classify("Brent Taylor addresses allegations days before election"))
        self.assertEqual(n.classify("Brent crude slips below $115")["markets"], ["CL"])
        self.assertEqual(n.classify("Oil down 2% on Iran war de-escalation hopes")["bias"], "bearish")
        self.assertFalse(n.classify("Oil steady on de-escalation hopes")["urgent"])

    def test_no_false_ease(self):
        self.assertEqual(n.classify("Oil lease sale planned in Alaska")["bias"], "neutral")

    def test_eastern_and_reminders(self):
        self.assertEqual(n.eastern(datetime(2026, 10, 28, 14, 27, tzinfo=timezone.utc)).hour, 10)   # EDT
        self.assertEqual(n.eastern(datetime(2026, 11, 5, 15, 27, tzinfo=timezone.utc)).hour, 10)    # EST
        self.assertIn("crude", n.report_reminder(datetime(2026, 10, 28, 14, 26, tzinfo=timezone.utc)))
        self.assertIn("storage", n.report_reminder(datetime(2026, 11, 5, 15, 25, tzinfo=timezone.utc)))
        self.assertIsNone(n.report_reminder(datetime(2026, 10, 28, 14, 31, tzinfo=timezone.utc)))
        self.assertIsNone(n.report_reminder(datetime(2026, 10, 27, 14, 26, tzinfo=timezone.utc)))

    def test_toast_xml_escapes(self):
        x = n.toast_xml("CL & NG", ["a <b>", "c"], "https://x.com/?a=1&b=2")
        self.assertIn("CL &amp; NG", x)
        self.assertIn("a &lt;b&gt;", x)
        self.assertIn("a=1&amp;b=2", x)

    def test_check_dedupes(self):
        now = datetime(2026, 10, 27, 15, tzinfo=timezone.utc).timestamp()
        n.http_get = lambda url, timeout=20: RSS
        state = {"seen": {}, "sources": {}}
        new = n.check(state, None, now)
        self.assertEqual(len(new), 2)
        self.assertEqual(new[0]["markets"], ["CL"])
        self.assertEqual(n.check(state, None, now), [])
        self.assertTrue(all(s["ok"] for s in state["sources"].values()))


if __name__ == "__main__":
    unittest.main()
