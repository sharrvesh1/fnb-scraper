# Singapore F&B Lead Generation Pipeline

Discovers restaurants, catering companies, and central kitchens in Singapore
via the **Google Places API** (official, licensed — not scraped search
results), then visits each business's own public website to collect
**published** email addresses and WhatsApp numbers. Results are deduplicated
and appended to a Google Sheet on a daily schedule via GitHub Actions.

## How it works

1. **Discovery** — `scraper_main.py` runs a set of Places API text searches
   (e.g. "catering companies in Singapore", "central kitchen Singapore").
2. **Filtering** — results containing competitor keywords (cleaning
   services, facility management, manpower agencies) are dropped.
3. **Extraction** — for each remaining business, the script fetches its own
   website's homepage and common contact-page paths with a normal HTTP
   request (no bot-detection bypass) and regexes out emails and SG mobile
   numbers.
4. **Formatting** — SG numbers are turned into `wa.me` links with your
   prefilled outreach message, URL-encoded.
5. **Dedup & write** — the script reads the sheet first, skips anything
   already listed by (business name, website), and appends only new rows.

Sites that block automated requests, have no listed contact info, or fail
to load are simply skipped and logged — the pipeline does not attempt to
circumvent any site's protections.

## 1. Get a Google Places API key

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and
   create (or select) a project.
2. Enable **Places API** under "APIs & Services" → "Library".
3. Create an API key under "APIs & Services" → "Credentials".
4. **Set a billing account.** Places API has a monthly free credit
   (currently enough for light use like this), but requires billing to be
   enabled — you will not be charged unless you exceed the free tier.
   Consider setting a budget alert or quota cap to guarantee zero spend.
5. Restrict the key to "Places API" only, for safety.

## 2. Create the Google Sheet & service account

1. Create a new Google Sheet. Copy its ID from the URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
2. In Google Cloud Console, go to "IAM & Admin" → "Service Accounts" →
   "Create Service Account". Any name is fine (e.g. `sg-fb-leads-bot`).
3. Open the new service account → "Keys" → "Add Key" → "Create new key" →
   JSON. This downloads a `.json` credentials file — keep it private.
4. Enable the **Google Sheets API** and **Google Drive API** for the
   project (APIs & Services → Library).
5. Open your Google Sheet → "Share" → paste the service account's email
   address (found in the JSON file as `client_email`) → give it **Editor**
   access.

## 3. Add GitHub Repository Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add three secrets:

| Secret name | Value |
|---|---|
| `GOOGLE_PLACES_API_KEY` | The API key from step 1 |
| `GOOGLE_SHEET_ID` | The Sheet ID from step 2 |
| `GCP_SERVICE_ACCOUNT_JSON` | The **entire contents** of the downloaded JSON key file, pasted as-is |

Never commit any of these values into the repository itself.

## 4. Push the code

```
sg-fb-leads/
├── requirements.txt
├── scraper_main.py
└── .github/
    └── workflows/
        └── scraper_cron.yml
```

Push this structure to a GitHub repository (public or private — Actions
free-tier minutes apply either way, and this job's runtime is minutes, not
hours, so it stays well within the free monthly allowance).

## 5. Trigger the first run manually

1. Go to your repo's **Actions** tab.
2. Select "SG F&B Lead Generation" in the left sidebar.
3. Click **Run workflow** → **Run workflow** (this uses the
   `workflow_dispatch` trigger, no need to wait for the cron schedule).
4. Watch the run logs. On completion, open your Google Sheet — a "Leads"
   tab will have been created with headers and any new rows found.

After the first successful run, it will continue automatically every day
at 02:00 SGT.

## Tuning it further

- **Add more search queries**: edit the `SEARCH_QUERIES` list in
  `scraper_main.py` — e.g. add specific cuisines, neighbourhoods
  ("restaurants in Tanjong Pagar"), or franchise names to widen coverage
  beyond the Places API's ~60-results-per-query cap.
- **Add more exclude keywords**: extend `EXCLUDE_KEYWORDS` if you notice
  unwanted business types slipping through.
- **Adjust the WhatsApp message**: edit `WHATSAPP_MESSAGE` at the top of
  the script.
- **Cost control**: Places API Text Search + Details calls are metered.
  With ~10 queries/day at up to 60 results each, you're well inside the
  monthly free credit, but you can reduce `SEARCH_QUERIES` or `max_pages`
  in `places_text_search()` if you want to scale it down further.
