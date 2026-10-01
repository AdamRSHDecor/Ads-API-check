"""Amazon Ads API access checker.

    python check.py                 # opens the local form at http://localhost:8765
    python check.py check           # run checks in the terminal
    python check.py check --report --write-test --profile 1234567890
"""

import argparse
import html
import json
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import amazon_ads as ads

HOST, PORT = "127.0.0.1", 8765
STATE = {"oauth_state": None}
JOB = {"thread": None, "log": [], "error": None}

STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#5d6675;--line:#dde1e7;--accent:#ff9900;
--pass:#1a7f37;--fail:#cf222e;--warn:#9a6700;--skip:#6e7781}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ec;--muted:#9aa3b2;
--line:#2a2f3a;--pass:#3fb950;--fail:#f85149;--warn:#d29922;--skip:#8b949e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:900px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:16px 0}
label{display:block;font-weight:600;margin:12px 0 4px}small,.muted{color:var(--muted)}
input,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--fg);font:inherit}
input[type=checkbox]{width:auto;margin-right:8px}.inline{display:flex;align-items:center;font-weight:400}
button,.btn{display:inline-block;margin-top:14px;padding:9px 16px;border:0;border-radius:6px;
background:var(--accent);color:#111;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
code{background:var(--bg);padding:1px 5px;border-radius:4px;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}
.s{font-weight:700}.PASS{color:var(--pass)}.FAIL{color:var(--fail)}.WARN{color:var(--warn)}.SKIP{color:var(--skip)}
.banner{padding:12px 14px;border-radius:8px;font-weight:600;border:1px solid currentColor}
details pre{white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:320px;overflow:auto}
"""


def esc(v):
    return html.escape(str(v if v is not None else ""))


def page(body, refresh=False):
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ads API Check</title>
<style>{STYLE}</style></head><body><main>{body}</main></body></html>"""


def render_results(record):
    if not record:
        return ""
    rows = []
    for r in record["results"]:
        extra = ""
        if r["data"]:
            extra = f"<details><summary>data</summary><pre>{esc(json.dumps(r['data'], indent=2))}</pre></details>"
        rows.append(
            f"<tr><td class='s {r['status']}'>{r['status']}</td><td><b>{esc(r['name'])}</b><br>"
            f"{esc(r['detail'])}{extra}</td></tr>"
        )
    overall = record["overall"]
    msg = "Access confirmed - live data received." if overall == "PASS" else "A check failed - see the first FAIL row for the fix."
    return f"""<div class="card"><h2>Results <small>checked {esc(record['checkedAt'])} UTC</small></h2>
<div class="banner {overall}">{overall}: {msg}</div><table>{''.join(rows)}</table></div>"""


def job_running():
    return JOB["thread"] is not None and JOB["thread"].is_alive()


def start_job(cfg, report, write_test):
    def work():
        try:
            ads.run_checks(cfg, profile_id=cfg["AMAZON_ADS_PROFILE_ID"] or None,
                           report=report, write_test=write_test, log=JOB["log"].append)
        except Exception as e:  # show any unexpected crash on the page instead of a blank screen
            traceback.print_exc()
            JOB["error"] = f"{type(e).__name__}: {e}"

    JOB.update(log=[], error=None, thread=threading.Thread(target=work, daemon=True))
    JOB["thread"].start()


def render_job():
    if not JOB["thread"]:
        return ""
    log = esc("\n".join(JOB["log"]))
    if job_running():
        return f"""<div class="card"><h2>Running checks...</h2><p class="muted">This page refreshes by itself.
The 7-day report can take 1-5 minutes while Amazon builds it.</p><pre>{log}</pre></div>"""
    if JOB["error"]:
        return f"""<div class="card"><div class="banner FAIL">Unexpected error: {esc(JOB['error'])}</div>
<p class="muted">Copy this message to Claude.</p><pre>{log}</pre></div>"""
    return ""


def home(message=""):
    cfg = ads.load_config()
    record = None
    if ads.LAST_CHECK_PATH.exists() and not job_running() and not JOB["error"]:
        record = json.loads(ads.LAST_CHECK_PATH.read_text())

    def field(key, label, help_text, secret=False):
        value = cfg.get(key, "")
        if secret:
            placeholder = "saved - leave blank to keep" if value else "not set"
            return (f"<label>{label}</label><input type='password' name='{key}' placeholder='{placeholder}' "
                    f"autocomplete='off'><small>{help_text}</small>")
        return f"<label>{label}</label><input name='{key}' value='{esc(value)}'><small>{help_text}</small>"

    regions = "".join(
        f"<option value='{k}' {'selected' if cfg['AMAZON_ADS_REGION'] == k else ''}>{k} - {esc(v['label'])}</option>"
        for k, v in ads.REGIONS.items()
    )
    has_client = cfg["AMAZON_ADS_CLIENT_ID"] and cfg["AMAZON_ADS_CLIENT_SECRET"]
    signin = (
        "<a class='btn' href='/auth/start'>Sign in with Amazon</a>" if has_client
        else "<p class='FAIL'>Save your Client ID and Client Secret in step 1 first.</p>"
    )
    token_state = "<span class='PASS'>Refresh token saved.</span>" if cfg["AMAZON_ADS_REFRESH_TOKEN"] else "<span class='WARN'>No refresh token yet.</span>"
    note = f"<div class='card banner'>{message}</div>" if message else ""
    return page(f"""
<h1>Amazon Ads API access check</h1>
<p class="muted">Everything stays on this computer. Keys are saved to <code>.env</code> next to this script.</p>
{note}
<form class="card" method="post" action="/save">
<h2>1. Your app credentials</h2>
<p class="muted">From the Amazon Developer Console → Login with Amazon → your security profile → Web Settings.</p>
{field("AMAZON_ADS_CLIENT_ID", "Client ID", "Starts with <code>amzn1.application-oa2-client.</code>")}
{field("AMAZON_ADS_CLIENT_SECRET", "Client Secret", "", secret=True)}
<label>Region</label><select name="AMAZON_ADS_REGION">{regions}</select>
<small>Where your ads run. US sellers: NA.</small>
{field("AMAZON_ADS_REFRESH_TOKEN", "Refresh token (optional - step 2 fills this in)", "Starts with <code>Atzr|</code>", secret=True)}
{field("AMAZON_ADS_PROFILE_ID", "Profile ID (optional)", "Leave blank to check every advertising profile.")}
<button>Save</button></form>

<div class="card"><h2>2. Get a refresh token</h2>
<p>{token_state}</p>
<p class="muted">One-time setup: in your security profile's <b>Web Settings → Allowed Return URLs</b> add
<code>{ads.REDIRECT_URI}</code>. Then click below and sign in with the Amazon login that has access to your
Advertising Console.</p>{signin}</div>

<form class="card" method="post" action="/run">
<h2>3. Run the checks</h2>
<label class="inline"><input type="checkbox" name="report">Also pull last 7 days of spend / clicks / sales (takes 1-5 min)</label>
<label class="inline"><input type="checkbox" name="write_test">Also test edit access (re-saves one campaign's state with its current value - changes nothing)</label>
<button {'disabled' if job_running() else ''}>Run checks</button></form>
{render_job()}
{render_results(record)}
""", refresh=job_running())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_html(self, body, status=200):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def form(self):
        length = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/":
            return self.send_html(home(esc(query.get("msg", ""))))
        if url.path == "/auth/start":
            STATE["oauth_state"] = ads.new_state()
            return self.redirect(ads.AdsClient(ads.load_config()).authorize_url(STATE["oauth_state"]))
        if url.path == "/callback":
            return self.callback(query)
        self.send_html(page("<p>Not found. <a href='/'>Home</a></p>"), 404)

    def callback(self, query):
        if "error" in query:
            return self.send_html(home(f"Amazon sign-in failed: {esc(query['error'])} - {esc(query.get('error_description', ''))}"))
        if not STATE["oauth_state"] or query.get("state") != STATE["oauth_state"]:
            return self.send_html(home("Sign-in response did not match this session. Click 'Sign in with Amazon' again."))
        STATE["oauth_state"] = None
        try:
            tokens = ads.AdsClient(ads.load_config()).exchange_code(query.get("code", ""))
        except ads.ApiError as e:
            return self.send_html(home(esc(e)))
        ads.save_config({"AMAZON_ADS_REFRESH_TOKEN": tokens["refresh_token"]})
        self.redirect("/?msg=" + urllib.parse.quote("Refresh token saved. Now run the checks (step 3)."))

    def do_POST(self):
        if self.path == "/save":
            data = self.form()
            updates = {k: data.get(k, "") for k in ads.CONFIG_KEYS if k not in ads.SECRET_KEYS}
            updates.update({k: data[k] for k in ads.SECRET_KEYS if data.get(k, "").strip()})
            ads.save_config(updates)
            return self.redirect("/?msg=Saved.")
        if self.path == "/run":
            data = self.form()
            if not job_running():
                start_job(ads.load_config(), "report" in data, "write_test" in data)
            return self.redirect("/")
        self.send_html(page("<p>Not found.</p>"), 404)


def serve(open_browser=True):
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"Ads API check running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Confirm Amazon Ads API access and pull live data.")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("check", help="run checks in the terminal")
    run.add_argument("--profile", help="only check this profile ID")
    run.add_argument("--report", action="store_true", help="also pull a 7-day performance report")
    run.add_argument("--write-test", action="store_true", help="also run a no-op campaign update")
    ui = sub.add_parser("ui", help="start the local web form (default)")
    ui.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "check":
        cfg = ads.load_config()
        record = ads.run_checks(
            cfg,
            profile_id=args.profile or cfg["AMAZON_ADS_PROFILE_ID"] or None,
            report=args.report,
            write_test=args.write_test,
        )
        print(f"\nOVERALL: {record['overall']}  (full details in {ads.LAST_CHECK_PATH.name})")
        return 0 if record["overall"] == "PASS" else 1
    serve(open_browser=not getattr(args, "no_browser", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
