import importlib.util
from pathlib import Path
import unittest

TIMELINE_PATH = Path(__file__).resolve().parents[1] / "nonebot_plugin_twitter" / "timeline.py"
spec = importlib.util.spec_from_file_location("twitter_timeline", TIMELINE_PATH)
timeline = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(timeline)

build_timeline_signature = timeline.build_timeline_signature
get_new_timeline_entries = timeline.get_new_timeline_entries
normalize_timeline_signature = timeline.normalize_timeline_signature


class TimelineSignatureTest(unittest.TestCase):
    def test_timeline_signature_ignores_tweet_retweet_prefix(self):
        self.assertEqual(
            build_timeline_signature("/BailiKoi/status/2053445084826440186#m"),
            "status:2053445084826440186",
        )
        self.assertEqual(
            normalize_timeline_signature("tweet:/BailiKoi/status/2053445084826440186"),
            "status:2053445084826440186",
        )
        self.assertEqual(
            normalize_timeline_signature("retweet:/BailiKoi/status/2053445084826440186"),
            "status:2053445084826440186",
        )

    def test_seen_retweet_does_not_make_same_status_new_when_it_appears_as_tweet(self):
        entries = [
            {
                "signature": "status:2053445084826440186",
                "tweet_id": "2053445084826440186",
            },
            {
                "signature": "status:2053432360092463308",
                "tweet_id": "2053432360092463308",
            },
        ]
        seen_signatures = [
            "retweet:/BailiKoi/status/2053445084826440186",
            "retweet:/BailiKoi/status/2053432360092463308",
        ]

        self.assertEqual(get_new_timeline_entries(entries, seen_signatures), [])


if __name__ == "__main__":
    unittest.main()
