import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import amazon_ads as ads

CFG = {
    "AMAZON_ADS_CLIENT_ID": "amzn1.application-oa2-client.x",
    "AMAZON_ADS_CLIENT_SECRET": "secret",
    "AMAZON_ADS_REFRESH_TOKEN": "Atzr|x",
    "AMAZON_ADS_REGION": "NA",
    "AMAZON_ADS_PROFILE_ID": "",
}
PROFILE = {"profileId": 111, "countryCode": "US", "currencyCode": "USD", "timezone": "America/Los_Angeles",
           "accountInfo": {"type": "seller", "name": "Acme", "validPaymentMethod": True}}
CAMPAIGN = {"campaignId": "c1", "name": "Brand", "state": "ENABLED", "budget": {"budget": 50}}


class FakeAmazon:
    """Routes ads.http calls to canned responses; records every call."""

    def __init__(self, overrides=None):
        self.calls = []
        self.routes = {
            ("POST", "/auth/o2/token"): (200, {"access_token": "tok", "expires_in": 3600, "refresh_token": "Atzr|new"}),
            ("GET", "/v2/profiles"): (200, [PROFILE]),
            ("POST", "/sp/campaigns/list"): (200, {"campaigns": [CAMPAIGN], "totalResults": 1}),
            ("PUT", "/sp/campaigns"): (207, {"campaigns": {"success": [{"campaignId": "c1"}], "error": []}}),
            ("POST", "/reporting/reports"): (200, {"reportId": "r1", "status": "PENDING"}),
            ("GET", "/reporting/reports/r1"): (200, {"status": "COMPLETED", "url": "https://s3.example/r1"}),
        }
        self.routes.update(overrides or {})

    def __call__(self, method, url, headers=None, data=None, timeout=60):
        self.calls.append((method, url, headers or {}, data))
        if url.startswith("https://s3.example"):
            rows = [{"campaignId": "c1", "campaignName": "Brand", "impressions": 1000, "clicks": 20,
                     "cost": 12.5, "sales14d": 80, "purchases14d": 3}]
            return 200, {}, gzip.compress(json.dumps(rows).encode())
        path = "/" + url.split("/", 3)[3].split("?")[0]
        status, body = self.routes[(method, path)]
        return status, {}, json.dumps(body).encode()


class ChecksTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(ads, "LAST_CHECK_PATH", tmp / "last.json"),
                        mock.patch.object(ads.time, "sleep", lambda s: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_with(self, fake, cfg=CFG, **kw):
        with mock.patch.object(ads, "http", fake):
            return ads.run_checks(dict(cfg), log=lambda m: None, **kw)

    def statuses(self, record):
        return {r["name"]: r["status"] for r in record["results"]}

    def test_full_pass(self):
        fake = FakeAmazon()
        record = self.run_with(fake, report=True, write_test=True)
        self.assertEqual(record["overall"], "PASS", record)
        s = self.statuses(record)
        self.assertEqual(s["Performance metrics report"], "PASS")
        self.assertEqual(s["Edit access (no-op write)"], "PASS")
        put = next(c for c in fake.calls if c[0] == "PUT")
        self.assertEqual(json.loads(put[3]), {"campaigns": [{"campaignId": "c1", "state": "ENABLED"}]})
        self.assertEqual(put[2]["Amazon-Advertising-API-Scope"], "111")
        s3 = next(c for c in fake.calls if "s3.example" in c[1])
        self.assertNotIn("Authorization", s3[2])

    def test_missing_creds_stops_early(self):
        fake = FakeAmazon()
        record = self.run_with(fake, cfg={**CFG, "AMAZON_ADS_REFRESH_TOKEN": ""})
        self.assertEqual(record["overall"], "FAIL")
        self.assertEqual(fake.calls, [])

    def test_bad_refresh_token_explained(self):
        fake = FakeAmazon({("POST", "/auth/o2/token"): (400, {"error": "invalid_grant", "error_description": "bad"})})
        record = self.run_with(fake)
        last = record["results"][-1]
        self.assertEqual(last["status"], "FAIL")
        self.assertIn("Sign in with Amazon", last["detail"])

    def test_not_approved_for_ads_api(self):
        fake = FakeAmazon({("GET", "/v2/profiles"): (401, {"code": "UNAUTHORIZED", "details": "Not authorized"})})
        record = self.run_with(fake)
        self.assertIn("not been approved", record["results"][-1]["detail"])

    def test_no_profiles(self):
        record = self.run_with(FakeAmazon({("GET", "/v2/profiles"): (200, [])}))
        self.assertIn("region is wrong", record["results"][-1]["detail"])

    def test_duplicate_report_reuses_existing(self):
        rid = "12345678-1234-1234-1234-123456789abc"
        fake = FakeAmazon({
            ("POST", "/reporting/reports"): (425, {"code": "425", "detail": f"The Request is a duplicate of : {rid}"}),
            ("GET", f"/reporting/reports/{rid}"): (200, {"status": "COMPLETED", "url": "https://s3.example/x"}),
        })
        record = self.run_with(fake, report=True)
        self.assertEqual(self.statuses(record)["Performance metrics report"], "PASS")

    def test_rate_limit_retried(self):
        fake = FakeAmazon()
        responses = iter([(429, {}), (200, [PROFILE])])
        original = fake.__call__

        def flaky(method, url, headers=None, data=None, timeout=60):
            if url.endswith("/v2/profiles"):
                status, body = next(responses)
                return status, {}, json.dumps(body).encode()
            return original(method, url, headers, data, timeout)

        record = self.run_with(flaky)
        self.assertEqual(record["overall"], "PASS")

    def test_saved_profile_id_text_still_finds_campaigns(self):
        # Profile ID from the form/.env is a string; Amazon returns it as a number.
        record = self.run_with(FakeAmazon(), cfg={**CFG, "AMAZON_ADS_PROFILE_ID": "111"},
                               profile_id="111", report=True, write_test=True)
        s = self.statuses(record)
        self.assertEqual(s["Edit access (no-op write)"], "PASS", record)
        self.assertEqual(s["Performance metrics report"], "PASS", record)

    def test_report_works_without_timezone_database(self):
        # Stock Windows Python has no IANA time-zone data.
        def missing(name):
            raise ads.ZoneInfoNotFoundError(name)
        with mock.patch.object(ads, "ZoneInfo", missing):
            record = self.run_with(FakeAmazon(), report=True)
        self.assertEqual(self.statuses(record)["Performance metrics report"], "PASS", record)


class ConfigTest(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        tmp = Path(tempfile.mkdtemp()) / ".env"
        with mock.patch.object(ads, "ENV_PATH", tmp), mock.patch.dict("os.environ", {}, clear=True):
            ads.save_config({"AMAZON_ADS_CLIENT_ID": " abc ", "AMAZON_ADS_REGION": "eu"})
            cfg = ads.load_config()
        self.assertEqual(cfg["AMAZON_ADS_CLIENT_ID"], "abc")
        self.assertEqual(cfg["AMAZON_ADS_REGION"], "EU")
        self.assertEqual(oct(tmp.stat().st_mode & 0o777), "0o600")



class WebJobTest(unittest.TestCase):
    def test_crash_is_shown_not_blank(self):
        import check
        with mock.patch.object(ads, "run_checks", side_effect=RuntimeError("boom")), \
             mock.patch("traceback.print_exc"):
            check.start_job(dict(CFG), True, False)
            check.JOB["thread"].join()
        self.assertIn("RuntimeError: boom", check.render_job())


if __name__ == "__main__":
    unittest.main()
