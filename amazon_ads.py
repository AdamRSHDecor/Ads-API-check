"""Minimal Amazon Ads API client and access checks. Standard library only."""

import gzip
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
LAST_CHECK_PATH = HERE / "last_check.json"

SCOPE = "advertising::campaign_management"
REDIRECT_URI = "http://localhost:8765/callback"

REGIONS = {
    "NA": {
        "label": "North America (US, CA, MX, BR)",
        "auth": "https://www.amazon.com/ap/oa",
        "token": "https://api.amazon.com/auth/o2/token",
        "api": "https://advertising-api.amazon.com",
    },
    "EU": {
        "label": "Europe / Middle East / India",
        "auth": "https://eu.account.amazon.com/ap/oa",
        "token": "https://api.amazon.co.uk/auth/o2/token",
        "api": "https://advertising-api-eu.amazon.com",
    },
    "FE": {
        "label": "Far East (JP, AU, SG)",
        "auth": "https://apac.account.amazon.com/ap/oa",
        "token": "https://api.amazon.co.jp/auth/o2/token",
        "api": "https://advertising-api-fe.amazon.com",
    },
}

CONFIG_KEYS = [
    "AMAZON_ADS_CLIENT_ID",
    "AMAZON_ADS_CLIENT_SECRET",
    "AMAZON_ADS_REFRESH_TOKEN",
    "AMAZON_ADS_REGION",
    "AMAZON_ADS_PROFILE_ID",
]
SECRET_KEYS = {"AMAZON_ADS_CLIENT_SECRET", "AMAZON_ADS_REFRESH_TOKEN"}

SP_CAMPAIGN_TYPE = "application/vnd.spCampaign.v3+json"
REPORT_TYPE = "application/vnd.createasyncreportrequest.v3+json"


# ---------------------------------------------------------------- config


def load_config():
    cfg = {k: "" for k in CONFIG_KEYS}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in cfg:
                cfg[key.strip()] = value.strip().strip('"').strip("'")
    for key in CONFIG_KEYS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    cfg["AMAZON_ADS_REGION"] = (cfg["AMAZON_ADS_REGION"] or "NA").upper()
    return cfg


def save_config(updates):
    cfg = load_config()
    cfg.update({k: v.strip() for k, v in updates.items() if k in CONFIG_KEYS})
    lines = ["# Amazon Ads API credentials. Never commit this file."]
    lines += [f"{k}={cfg[k]}" for k in CONFIG_KEYS]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)
    return cfg


def missing_keys(cfg):
    required = ["AMAZON_ADS_CLIENT_ID", "AMAZON_ADS_CLIENT_SECRET", "AMAZON_ADS_REFRESH_TOKEN"]
    return [k for k in required if not cfg.get(k)]


# ------------------------------------------------------------------ http


class ApiError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def http(method, url, headers=None, data=None, timeout=60):
    """Return (status, headers, raw_bytes). HTTP error codes are returned, not raised."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except urllib.error.URLError as e:
        raise ApiError(f"Could not reach {urllib.parse.urlparse(url).netloc}: {e.reason}") from e


def parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return {"raw": raw[:500].decode("utf-8", "replace")}


LWA_HINTS = {
    "invalid_grant": "The refresh token (or sign-in code) is invalid, expired, revoked, or was issued to a "
    "different Client ID or region. Get a new one with 'Sign in with Amazon' (step 2).",
    "invalid_client": "Client ID or Client Secret is wrong. Copy both from the Login with Amazon security "
    "profile that is linked to your Amazon Ads API access.",
    "unauthorized_client": "This Login with Amazon app is not allowed to use this grant type.",
    "invalid_request": "Request was malformed - usually a blank or mistyped value in the form.",
}


class AdsClient:
    def __init__(self, cfg):
        self.cfg = cfg
        region = cfg.get("AMAZON_ADS_REGION") or "NA"
        if region not in REGIONS:
            raise ApiError(f"Unknown region '{region}'. Use one of: {', '.join(REGIONS)}")
        self.region = REGIONS[region]
        self._access_token = None
        self._expires_at = 0

    # ---- Login with Amazon

    def _lwa(self, form):
        status, _, raw = http(
            "POST",
            self.region["token"],
            {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            urllib.parse.urlencode(form).encode(),
        )
        data = parse_json(raw)
        if status != 200:
            code = data.get("error", "")
            hint = LWA_HINTS.get(code, "")
            raise ApiError(
                f"Login with Amazon rejected the request ({status} {code}): "
                f"{data.get('error_description', '')} {hint}".strip(),
                status,
                data,
            )
        return data

    def access_token(self):
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        data = self._lwa(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.cfg["AMAZON_ADS_REFRESH_TOKEN"],
                "client_id": self.cfg["AMAZON_ADS_CLIENT_ID"],
                "client_secret": self.cfg["AMAZON_ADS_CLIENT_SECRET"],
            }
        )
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    def exchange_code(self, code, redirect_uri=REDIRECT_URI):
        return self._lwa(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.cfg["AMAZON_ADS_CLIENT_ID"],
                "client_secret": self.cfg["AMAZON_ADS_CLIENT_SECRET"],
            }
        )

    def authorize_url(self, state, redirect_uri=REDIRECT_URI):
        query = urllib.parse.urlencode(
            {
                "client_id": self.cfg["AMAZON_ADS_CLIENT_ID"],
                "scope": SCOPE,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{self.region['auth']}?{query}"

    # ---- Ads API

    def request(self, method, path, profile_id=None, body=None, content_type="application/json", accept=None):
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "Amazon-Advertising-API-ClientId": self.cfg["AMAZON_ADS_CLIENT_ID"],
            "Content-Type": content_type,
            "Accept": accept or content_type,
        }
        if profile_id:
            headers["Amazon-Advertising-API-Scope"] = str(profile_id)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(4):
            status, resp_headers, raw = http(method, self.region["api"] + path, headers, data)
            if status != 429 or attempt == 3:
                return status, parse_json(raw)
            time.sleep(float(resp_headers.get("Retry-After") or 2 ** (attempt + 1)))


def api_error_text(status, data):
    detail = data.get("details") or data.get("detail") or data.get("message") or data.get("code") or data
    hint = {
        401: "Access token was rejected. Usually your Client ID has not been approved for the Amazon Ads API "
        "yet, or the Client ID does not match the one used to get the refresh token.",
        403: "Not authorized. The Amazon user who signed in does not have access to this advertising "
        "account, or the account is not set up for this ad type.",
        404: "Endpoint or profile not found - check the region matches where your ads run.",
    }.get(status, "")
    return f"HTTP {status}: {detail}. {hint}".strip()


# ---------------------------------------------------------------- checks


@dataclass
class Check:
    name: str
    status: str  # PASS, FAIL, WARN, SKIP
    detail: str
    data: dict = field(default_factory=dict)


def run_checks(cfg, profile_id=None, report=False, write_test=False, log=print):
    """Run every access check in order. Stops at the first layer that fails."""
    results = []

    def add(name, status, detail, data=None):
        results.append(Check(name, status, detail, data or {}))
        log(f"[{status}] {name}: {detail}")
        return status != "FAIL"

    # 1. Config
    missing = missing_keys(cfg)
    if missing:
        add("Credentials present", "FAIL", f"Missing: {', '.join(missing)}. Fill them in (step 1 / step 2).")
        return finish(results, cfg)
    add("Credentials present", "PASS", f"Client ID, secret and refresh token found. Region: {cfg['AMAZON_ADS_REGION']}")

    # 2. Login with Amazon token
    try:
        client = AdsClient(cfg)
        client.access_token()
    except ApiError as e:
        add("Login with Amazon token", "FAIL", str(e))
        return finish(results, cfg)
    add("Login with Amazon token", "PASS", "Refresh token exchanged for a fresh access token.")

    # 3. Profiles (advertising accounts)
    try:
        status, profiles = client.request("GET", "/v2/profiles")
    except ApiError as e:
        add("Ads API access (profiles)", "FAIL", str(e))
        return finish(results, cfg)
    if status != 200:
        add("Ads API access (profiles)", "FAIL", api_error_text(status, profiles))
        return finish(results, cfg)
    if not profiles:
        add(
            "Ads API access (profiles)",
            "FAIL",
            "API access works but no advertising accounts were returned. Either the Amazon login used in "
            "step 2 has no access to your ad account, or the region is wrong (try EU / FE).",
        )
        return finish(results, cfg)
    summary = [
        {
            "profileId": p.get("profileId"),
            "country": p.get("countryCode"),
            "currency": p.get("currencyCode"),
            "timezone": p.get("timezone"),
            "type": (p.get("accountInfo") or {}).get("type"),
            "name": (p.get("accountInfo") or {}).get("name"),
            "valid": (p.get("accountInfo") or {}).get("validPaymentMethod"),
        }
        for p in profiles
    ]
    types = sorted({s["type"] or "?" for s in summary})
    add(
        "Ads API access (profiles)",
        "PASS",
        f"{len(profiles)} advertising profile(s) found. Account type(s): {', '.join(types)}.",
        {"profiles": summary},
    )
    if "agency" in types and len(types) == 1:
        add("Account type", "WARN", "Profiles are 'agency' type, not a direct seller/vendor account.")

    # 4. Live campaign data per profile
    campaigns_by_profile = {}
    targets = [s for s in summary if not profile_id or str(s["profileId"]) == str(profile_id)]
    if profile_id and not targets:
        add("Live campaign data", "FAIL", f"Profile {profile_id} is not in the list above. Clear it or pick one from the list.")
        return finish(results, cfg)
    any_ok = False
    for s in targets[:20]:
        pid = s["profileId"]
        label = f"{s['country']} {s['name'] or ''} ({pid})".strip()
        try:
            status, data = client.request(
                "POST",
                "/sp/campaigns/list",
                pid,
                {"maxResults": 100, "includeExtendedDataFields": True},
                SP_CAMPAIGN_TYPE,
            )
        except ApiError as e:
            add(f"Live campaigns - {label}", "FAIL", str(e))
            continue
        if status != 200:
            add(f"Live campaigns - {label}", "FAIL", api_error_text(status, data))
            continue
        any_ok = True
        camps = data.get("campaigns", [])
        campaigns_by_profile[pid] = camps
        states = {}
        for c in camps:
            states[c.get("state", "?")] = states.get(c.get("state", "?"), 0) + 1
        sample = [
            {
                "campaignId": c.get("campaignId"),
                "name": c.get("name"),
                "state": c.get("state"),
                "servingStatus": (c.get("extendedData") or {}).get("servingStatus"),
                "dailyBudget": (c.get("budget") or {}).get("budget"),
                "lastUpdated": (c.get("extendedData") or {}).get("lastUpdateDateTime"),
            }
            for c in camps[:10]
        ]
        total = data.get("totalResults", len(camps))
        state_txt = ", ".join(f"{v} {k.lower()}" for k, v in sorted(states.items())) or "none"
        add(
            f"Live campaigns - {label}",
            "PASS" if camps else "WARN",
            f"{total} Sponsored Products campaign(s) ({state_txt})." if camps
            else "Access OK but this profile has no Sponsored Products campaigns.",
            {"campaigns": sample},
        )
    if not any_ok:
        return finish(results, cfg)

    # Pick the profile for the deeper checks: configured one, else first with campaigns.
    chosen = profile_id or next((pid for pid, c in campaigns_by_profile.items() if c), None)
    chosen_profile = next((s for s in summary if str(s["profileId"]) == str(chosen)), None)

    # 5. Performance report (optional)
    if not report:
        add("Performance metrics report", "SKIP", "Not requested (tick the box / use --report).")
    elif not chosen_profile:
        add("Performance metrics report", "SKIP", "No profile with campaigns to report on.")
    else:
        try:
            add(*run_report(client, chosen_profile, log))
        except ApiError as e:
            add("Performance metrics report", "FAIL", str(e))

    # 6. Write access (optional, no-op)
    if not write_test:
        add("Edit access (no-op write)", "SKIP", "Not requested (tick the box / use --write-test).")
    else:
        camps = campaigns_by_profile.get(chosen, []) if chosen else []
        target = next((c for c in camps if c.get("state") in ("ENABLED", "PAUSED")), None)
        if not target:
            add("Edit access (no-op write)", "SKIP", "No enabled or paused campaign to test on.")
        else:
            add(*run_write_test(client, chosen, target))

    return finish(results, cfg)


def run_report(client, profile, log=print, days=7, max_wait=600):
    pid = profile["profileId"]
    tz = ZoneInfo(profile.get("timezone") or "UTC")
    end = datetime.now(tz).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    body = {
        "name": f"ads-api-check {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["campaign"],
            "columns": ["campaignId", "campaignName", "impressions", "clicks", "cost", "sales14d", "purchases14d"],
            "reportTypeId": "spCampaigns",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }
    name = "Performance metrics report"
    status, data = client.request("POST", "/reporting/reports", pid, body, REPORT_TYPE)
    if status == 425:  # identical report already requested - reuse it
        match = re.search(r"[0-9a-fA-F-]{36}", json.dumps(data))
        if not match:
            return name, "FAIL", api_error_text(status, data)
        report_id = match.group(0)
    elif status in (200, 202):
        report_id = data["reportId"]
    else:
        return name, "FAIL", api_error_text(status, data)

    log(f"       report {report_id} requested for {start}..{end}; waiting for Amazon to build it (1-5 min)...")
    deadline = time.time() + max_wait
    while True:
        status, data = client.request("GET", f"/reporting/reports/{report_id}", pid, None, REPORT_TYPE)
        if status != 200:
            return name, "FAIL", api_error_text(status, data)
        state = data.get("status")
        if state == "COMPLETED":
            break
        if state == "FAILED":
            return name, "FAIL", f"Amazon could not build the report: {data.get('failureReason')}"
        if time.time() > deadline:
            return name, "WARN", f"Report {report_id} still {state} after {max_wait}s. Access is fine; Amazon is slow. Try again later."
        time.sleep(15)

    status, _, raw = http("GET", data["url"])
    if status != 200:
        return name, "FAIL", f"Report built but download failed (HTTP {status})."
    rows = json.loads(gzip.decompress(raw))
    totals = {k: round(sum(float(r.get(k) or 0) for r in rows), 2)
              for k in ("impressions", "clicks", "cost", "sales14d", "purchases14d")}
    top = sorted(rows, key=lambda r: float(r.get("cost") or 0), reverse=True)[:10]
    detail = (
        f"{start}..{end} ({profile.get('country')}, {profile.get('currency')}): "
        f"{int(totals['impressions']):,} impressions, {int(totals['clicks']):,} clicks, "
        f"spend {totals['cost']:,}, sales {totals['sales14d']:,} across {len(rows)} campaign(s)."
    )
    return name, "PASS", detail, {"totals": totals, "topCampaigns": top}


def run_write_test(client, profile_id, campaign):
    """Re-save a campaign's state with the value it already has. Changes nothing."""
    name = "Edit access (no-op write)"
    body = {"campaigns": [{"campaignId": campaign["campaignId"], "state": campaign["state"]}]}
    status, data = client.request("PUT", "/sp/campaigns", profile_id, body, SP_CAMPAIGN_TYPE)
    if status not in (200, 207):
        return name, "FAIL", api_error_text(status, data)
    result = data.get("campaigns", {})
    if result.get("success"):
        return name, "PASS", (
            f"Write accepted on campaign '{campaign.get('name')}' (state left as {campaign['state']}). "
            "You can edit campaigns via the API."
        )
    return name, "FAIL", f"Write rejected: {json.dumps(result.get('error'))[:500]}"


def finish(results, cfg):
    record = {
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": cfg.get("AMAZON_ADS_REGION"),
        "overall": "FAIL" if any(r.status == "FAIL" for r in results) else "PASS",
        "results": [asdict(r) for r in results],
    }
    LAST_CHECK_PATH.write_text(json.dumps(record, indent=2, default=str))
    return record


def new_state():
    return secrets.token_urlsafe(16)
