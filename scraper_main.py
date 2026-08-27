"""
Singapore F&B Lead Generation Pipeline
---------------------------------------
Discovers restaurants, catering companies, and central kitchens in Singapore
via the Google Places API, then visits each business's OWN public website
(their normal contact/about page) to extract published email addresses and
WhatsApp numbers. Results are deduplicated and appended to a Google Sheet.

This script intentionally does NOT attempt to bypass any site's bot
protection. If a target site cannot be reached with a normal HTTP request
(e.g. it actively blocks automated clients), it is skipped and logged —
not defeated. Discovery uses Google's official, licensed Places API rather
than scraping search result pages.

Required environment variables (set as GitHub Actions secrets):
    GOOGLE_PLACES_API_KEY   - Google Cloud API key with Places API enabled
    GOOGLE_SHEET_ID         - The target Google Sheet's ID (from its URL)
    GCP_SERVICE_ACCOUNT_JSON - Full JSON contents of a Google service
                                account key with edit access to the sheet
"""

import os
import re
import sys
import time
import random
import logging
import json
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sg_fb_leads")

PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")

SHEET_TAB_NAME = "Leads"
SHEET_HEADERS = [
    "Business Name",
    "Business Type",
    "Website",
    "Address",
    "Email(s)",
    "WhatsApp Link(s)",
    "Source Query",
    "Date Added",
]

# Discovery queries. Each targets a segment relevant to dedicated dishwasher
# / kitchen-steward staffing demand. Expand this list over time.
SEARCH_QUERIES = [
    ("restaurants in Bugis Singapore", "Restaurant"),
    ("cafes in Bugis Singapore", "Cafe"),
    ("restaurants in Clarke Quay", "Restaurant"),
    ("restaurants in Tanjong Pagar", "Restaurant"),
    ("cafes in Tiong Bahru", "Cafe"),
    ("restaurants in Orchard Road", "Restaurant"),
    ("restaurants in Dhoby Ghaut", "Restaurant"),
    ("seafood restaurant in Chinatown Singapore", "Restaurant"),
    ("central kitchen in Kallang", "Central Kitchen"),
    ("catering companies in Bendemeer", "Catering"),
    
    # New CBD & Surrounding Areas
    ("restaurants in CBD Singapore", "Restaurant"),
    ("restaurants in Telok Ayer", "Restaurant"),
    ("cafes in Telok Ayer", "Cafe"),
    ("restaurants in Marina Bay", "Restaurant"),
    ("restaurants in Tanglin", "Restaurant"),
    ("restaurants in City Hall Singapore", "Restaurant"),
    ("restaurants in Raffles Place", "Restaurant"),
    ("cafes in Raffles Place", "Cafe"),
    ("restaurants in Boat Quay", "Restaurant"),
]

# Businesses whose name/type/website text contains these terms are dropped —
# they are likely competitors (cleaning/manpower vendors) rather than F&B
# end-users who would hire dishwashing staff.
EXCLUDE_KEYWORDS = [
    "cleaning company",
    "cleaning services",
    "cleaning service",
    "facility management",
    "manpower agency",
    "manpower supply",
    "janitorial",
    "pest control",
]

CONTACT_PAGE_PATHS = [
    "", "contact", "contact-us", "contactus", "about", "about-us", "reach-us",
]

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# Matches Singapore mobile/landline numbers, with or without +65, allowing
# spaces or hyphens between digit groups (e.g. "+65 9123 4567", "91234567").
SG_PHONE_REGEX = re.compile(
    r"(?:\+?65[\s\-]?)?([689]\d{3}[\s\-]?\d{4})"
)

# Generic addresses that are not useful outreach targets even if found.
GENERIC_EMAIL_PREFIXES = {"noreply", "no-reply", "donotreply", "webmaster", "postmaster"}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

WHATSAPP_MESSAGE = (
    "Hi, I represent a specialized dishwashing manpower supplier in "
    "Singapore. Are you currently looking for reliable kitchen staff?"
)


# --------------------------------------------------------------------------
# Google Sheets helpers
# --------------------------------------------------------------------------

def get_sheet():
    """Authenticate and return the target worksheet, creating headers if new."""
    if not SERVICE_ACCOUNT_JSON:
        log.error("GCP_SERVICE_ACCOUNT_JSON is not set.")
        sys.exit(1)
    if not SHEET_ID:
        log.error("GOOGLE_SHEET_ID is not set.")
        sys.exit(1)

    try:
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as exc:
        log.error("GCP_SERVICE_ACCOUNT_JSON is not valid JSON: %s", exc)
        sys.exit(1)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
    except Exception as exc:
        log.error("Failed to authenticate or open the Google Sheet: %s", exc)
        sys.exit(1)

    try:
        worksheet = spreadsheet.worksheet(SHEET_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=SHEET_TAB_NAME, rows=1000, cols=len(SHEET_HEADERS)
        )
        worksheet.append_row(SHEET_HEADERS)
        log.info("Created new '%s' tab with headers.", SHEET_TAB_NAME)

    if worksheet.row_count == 0 or not worksheet.row_values(1):
        worksheet.append_row(SHEET_HEADERS)

    return worksheet


def load_existing_keys(worksheet):
    """Return a set of (business_name, website) pairs already in the sheet."""
    try:
        records = worksheet.get_all_values()
    except Exception as exc:
        log.error("Could not read existing sheet rows for dedup: %s", exc)
        return set()

    existing = set()
    for row in records[1:]:  # skip header
        if len(row) >= 3:
            name = row[0].strip().lower()
            website = row[2].strip().lower()
            existing.add((name, website))
    return existing


def append_leads(worksheet, rows):
    if not rows:
        log.info("No new leads to append.")
        return
    try:
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")
        log.info("Appended %d new lead(s) to the sheet.", len(rows))
    except Exception as exc:
        log.error("Failed to append rows to sheet: %s", exc)


# --------------------------------------------------------------------------
# Google Places API (discovery) — official, licensed, no scraping involved
# --------------------------------------------------------------------------

def places_text_search(query):
    """Yield place results for a text search query, following pagination."""
    if not PLACES_API_KEY:
        log.error("GOOGLE_PLACES_API_KEY is not set.")
        return

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "region": "sg", "key": PLACES_API_KEY}
    next_page_token = None
    pages_fetched = 0
    max_pages = 3  # Places API caps at 60 results (3 pages of 20)

    while True:
        try:
            if next_page_token:
                # Google requires a short delay before a page token becomes valid.
                time.sleep(2)
                resp = requests.get(
                    url,
                    params={"pagetoken": next_page_token, "key": PLACES_API_KEY},
                    timeout=15,
                )
            else:
                resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            log.warning("Places API request failed for query '%s': %s", query, exc)
            return
        except ValueError as exc:
            log.warning("Places API returned invalid JSON for '%s': %s", query, exc)
            return

        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            log.warning("Places API status '%s' for query '%s': %s",
                        status, query, data.get("error_message", ""))
            return

        for result in data.get("results", []):
            yield result

        next_page_token = data.get("next_page_token")
        pages_fetched += 1
        if not next_page_token or pages_fetched >= max_pages:
            break


def places_details(place_id):
    """Fetch website and formatted phone number for a place."""
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "name,website,formatted_phone_number,formatted_address,types",
        "key": PLACES_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.warning("Places details request failed for %s: %s", place_id, exc)
        return {}
    except ValueError as exc:
        log.warning("Places details returned invalid JSON for %s: %s", place_id, exc)
        return {}

    if data.get("status") != "OK":
        return {}
    return data.get("result", {})


# --------------------------------------------------------------------------
# Contact extraction — plain HTTP requests to businesses' own public pages
# --------------------------------------------------------------------------

def is_excluded(*texts):
    combined = " ".join(t.lower() for t in texts if t)
    return any(keyword in combined for keyword in EXCLUDE_KEYWORDS)


def normalize_sg_number(match_group):
    digits = re.sub(r"[\s\-]", "", match_group)
    return digits


def format_whatsapp_link(sg_number_8_digits):
    encoded_message = quote(WHATSAPP_MESSAGE)
    return f"https://wa.me/65{sg_number_8_digits}?text={encoded_message}"


def extract_contacts_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Also inspect mailto: and tel:/https://wa.me links directly, since some
    # sites only expose contact info through link hrefs rather than visible text.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("mailto:"):
            text += " " + href.replace("mailto:", "")
        elif "wa.me" in href or href.startswith("tel:"):
            text += " " + href

    emails = set()
    for match in EMAIL_REGEX.findall(text):
        local_part = match.split("@")[0].lower()
        if local_part in GENERIC_EMAIL_PREFIXES:
            continue
        if match.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg")):
            continue
        emails.add(match)

    numbers = set()
    for match in SG_PHONE_REGEX.finditer(text):
        digits = normalize_sg_number(match.group(1))
        if len(digits) == 8:
            numbers.add(digits)

    return emails, numbers


def fetch_contact_info(website):
    """Try the homepage and a few common contact-page paths on a business's
    own site. Returns (emails, whatsapp_numbers). Any failure is logged and
    treated as 'no data found' rather than raising."""
    if not website:
        return set(), set()

    parsed = urlparse(website)
    if not parsed.scheme:
        website = "https://" + website

    all_emails, all_numbers = set(), set()

    for path in CONTACT_PAGE_PATHS:
        url = urljoin(website.rstrip("/") + "/", path)
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
            if resp.status_code >= 400:
                continue
            emails, numbers = extract_contacts_from_html(resp.text)
            all_emails |= emails
            all_numbers |= numbers
        except requests.RequestException as exc:
            log.info("Could not fetch %s (%s) — skipping this page.", url, exc)
            continue

        # Randomized delay between page fetches to space out requests.
        time.sleep(random.uniform(1.5, 3.5))

        # If we've already found solid contact info, no need to check every path.
        if all_emails and all_numbers:
            break

    return all_emails, all_numbers


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def run():
    if not all([PLACES_API_KEY, SHEET_ID, SERVICE_ACCOUNT_JSON]):
        log.error(
            "Missing required environment variables. Need "
            "GOOGLE_PLACES_API_KEY, GOOGLE_SHEET_ID, GCP_SERVICE_ACCOUNT_JSON."
        )
        sys.exit(1)

    worksheet = get_sheet()
    existing_keys = load_existing_keys(worksheet)
    log.info("Loaded %d existing lead(s) for deduplication.", len(existing_keys))

    new_rows = []
    seen_this_run = set()
    today = time.strftime("%Y-%m-%d")

    for query, business_type in SEARCH_QUERIES:
        log.info("Searching: %s", query)
        try:
            results = list(places_text_search(query))
        except Exception as exc:
            log.warning("Unexpected error during search '%s': %s", query, exc)
            continue

        for place in results:
            try:
                name = place.get("name", "").strip()
                place_id = place.get("place_id")
                address = place.get("formatted_address", "")
                types = " ".join(place.get("types", []))

                if not name or not place_id:
                    continue
                if is_excluded(name, types):
                    log.info("Excluded (competitor keyword): %s", name)
                    continue

                details = places_details(place_id)
                website = details.get("website", "")
                if is_excluded(name, types, website):
                    log.info("Excluded after details lookup: %s", name)
                    continue

                dedup_key = (name.lower(), website.strip().lower())
                if dedup_key in existing_keys or dedup_key in seen_this_run:
                    continue
                seen_this_run.add(dedup_key)

                emails, numbers = fetch_contact_info(website) if website else (set(), set())

                if not emails and not numbers:
                    log.info("No public contact info found for %s — skipping.", name)
                    continue

                whatsapp_links = [format_whatsapp_link(n) for n in sorted(numbers)]

                row = [
                    name,
                    business_type,
                    website,
                    address,
                    "; ".join(sorted(emails)) if emails else "",
                    "; ".join(whatsapp_links) if whatsapp_links else "",
                    query,
                    today,
                ]
                new_rows.append(row)
                log.info("Lead captured: %s (%d email(s), %d WhatsApp)",
                         name, len(emails), len(numbers))

            except Exception as exc:
                # Never let one malformed result crash the whole run.
                log.warning("Error processing a place result, skipping it: %s", exc)
                continue

            time.sleep(random.uniform(1.0, 2.5))

        time.sleep(random.uniform(2.0, 4.0))

    append_leads(worksheet, new_rows)
    log.info("Run complete. %d new lead(s) added.", len(new_rows))


if __name__ == "__main__":
    run()
