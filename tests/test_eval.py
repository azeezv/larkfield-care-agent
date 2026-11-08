"""Evaluation runner helpers: no model calls."""
import unittest

from evals import run_eval


class EvalTest(unittest.TestCase):
    def test_daily_quota_is_fatal(self):
        exc = RuntimeError(
            "429 RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel")
        self.assertTrue(run_eval._daily_quota_exhausted(exc))
        self.assertTrue(run_eval._daily_quota_exhausted(
            RuntimeError("The daily quota is used up")))

    def test_per_minute_limit_is_not_daily(self):
        exc = RuntimeError("429 RESOURCE_EXHAUSTED: requests per minute")
        self.assertFalse(run_eval._daily_quota_exhausted(exc))
        self.assertTrue(run_eval._rate_limited(exc))


if __name__ == "__main__":
    unittest.main()
