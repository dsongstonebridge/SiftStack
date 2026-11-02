"""search_by_address waits out a 429 instead of reading as "no record". Offline."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import datasift_api as d  # noqa: E402


class Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._p = status, text, payload or {}
        self.ok = status < 400

    def json(self):
        return self._p


THROTTLED = Resp(429, '{"detail":"Request was throttled. Expected available in 8 seconds."}')


class ThrottleTests(unittest.TestCase):
    def _run(self, responses):
        with mock.patch.object(d, "_headers", return_value={}), mock.patch.object(d, "_pace"),                 mock.patch.object(d.requests, "post", side_effect=responses) as post,                 mock.patch.object(d.time, "sleep") as sleep:
            try:
                return d.search_by_address("1"), post.call_count, [c.args[0] for c in sleep.call_args_list]
            except d.DataSiftAPIError as e:
                return e, post.call_count, [c.args[0] for c in sleep.call_args_list]

    def test_waits_for_the_server_hint_then_returns(self):
        out, calls, waits = self._run([THROTTLED, THROTTLED, Resp(200, payload={"results": [{"uuid": "u"}]})])
        self.assertEqual((out, calls, waits), ([{"uuid": "u"}], 3, [9, 9]))

    def test_a_throttle_that_never_clears_still_raises(self):
        out, calls, _ = self._run([THROTTLED] * 6)
        self.assertIsInstance(out, d.DataSiftAPIError)
        self.assertEqual(calls, 6)

    def test_other_errors_are_not_retried(self):
        out, calls, _ = self._run([Resp(500, "boom")])
        self.assertIsInstance(out, d.DataSiftAPIError)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
