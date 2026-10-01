# Ads API Check

Confirms your Amazon Ads API setup works end to end and pulls live data from your account.
No installs needed beyond Python 3.9+. Your keys stay on your computer in a `.env` file, which is never committed.

## What it checks (in order, stops at the first failure and tells you the fix)

| # | Check | Proves |
|---|-------|--------|
| 1 | Credentials present | Client ID, secret, refresh token are filled in |
| 2 | Login with Amazon token | Your refresh token + client ID/secret are valid |
| 3 | Ads API access (profiles) | Your app is approved for the Ads API and sees your ad accounts (seller / vendor / agency) |
| 4 | Live campaigns | Pulls your Sponsored Products campaigns right now (name, state, serving status, budget) |
| 5 | Performance report *(optional)* | Pulls the last 7 days of impressions, clicks, spend, sales |
| 6 | Edit access *(optional)* | Sends a campaign update that re-saves a campaign's state with the value it already has. **Nothing changes.** |

## Step by step

### Step 0 - Get the code and Python
1. Install Python 3 if you don't have it: https://www.python.org/downloads/ (Windows: tick **"Add Python to PATH"**).
2. Download this repo (green **Code** button → **Download ZIP**) and unzip it, or:
   ```bash
   git clone https://github.com/AdamRSHDecor/Ads-API-check.git
   cd Ads-API-check
   ```

### Step 1 - Allow the sign-in return address (one time, 1 minute)
1. Go to https://developer.amazon.com/loginwithamazon/console/site/lwa/overview.html
2. Pick the security profile you use for the Ads API → **Web Settings** → **Edit**.
3. Under **Allowed Return URLs** add exactly:
   ```
   http://localhost:8765/callback
   ```
4. Save. Copy the **Client ID** and **Client Secret** from the same page.

### Step 2 - Start the checker
- **Windows:** double-click `start-windows.bat`
- **Mac:** double-click `start-mac.command` (first time: right-click → Open)
- **Or any terminal:**
  ```bash
  python3 check.py
  ```
A page opens at http://localhost:8765.

### Step 3 - Fill the form
1. Paste Client ID + Client Secret, pick your region (US = NA), click **Save**.
2. Click **Sign in with Amazon** and log in with the Amazon account that has access to your Advertising Console. Approve. The refresh token is saved automatically.
3. Click **Run checks**. Tick the boxes to also pull 7-day metrics and test edit access.

All green = you have API access and live data. Results are also saved to `last_check.json`.

## Terminal mode (for scripts / scheduled checks)
```bash
python3 check.py check                         # basic checks
python3 check.py check --report --write-test   # everything
python3 check.py check --profile 1234567890    # one profile only
```
Exit code 0 = all passed, 1 = something failed.

## Common failures
| Message | Fix |
|---|---|
| `invalid_client` | Client ID / secret wrong - recopy from the security profile |
| `invalid_grant` | Refresh token bad or from another app/region - click **Sign in with Amazon** again |
| Profiles `HTTP 401` | Your client ID isn't approved for the Amazon Ads API yet (check your Ads API application status / email from Amazon) |
| `No advertising accounts` | Signed in with the wrong Amazon login, or wrong region |
| Amazon sign-in page says redirect URI mismatch | Step 1 not done, or URL typed differently |

## Run the tests
```bash
python3 -m unittest -v
```
