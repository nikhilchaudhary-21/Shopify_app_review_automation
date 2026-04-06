"""
Loop Subscriptions - All Ratings Reviews Scraper
- Saves new reviews to Google Sheets (duplicate-safe)
- Enriches each review with Shopify Domain from Salesforce Account
- Captures Loop's reply (if any) for each review
"""

import requests
from bs4 import BeautifulSoup
import threading
import time
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json

import gspread
from google.oauth2.service_account import Credentials
from simple_salesforce import Salesforce

# ── Config from GitHub Secrets ──────────────────────────────
SHEET_ID          = os.environ["SHEET_ID"]
WORKSHEET_NAME    = os.environ.get("WORKSHEET_NAME", "Reviews")
CREDS_JSON        = os.environ["GOOGLE_CREDENTIALS_JSON"]

SF_USERNAME       = os.environ["SF_USERNAME"]
SF_PASSWORD       = os.environ["SF_PASSWORD"]
SF_SECURITY_TOKEN = os.environ["SF_SECURITY_TOKEN"]
SF_INSTANCE_URL   = os.environ["SF_INSTANCE_URL"]
# ────────────────────────────────────────────────────────────

RATINGS  = [5, 4, 3, 2, 1]
THREADS  = 20

BASE_URL = "https://apps.shopify.com/loop-subscriptions/reviews"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SHEET_HEADERS = [
    "review_id", "rating", "store_name", "shopify_domain",
    "country", "duration", "date", "review",
    "loop_reply", "loop_reply_date",
    "scraped_at"
]

# Locks
sheet_lock    = threading.Lock()
counter_lock  = threading.Lock()
seen_lock     = threading.Lock()
sf_cache_lock = threading.Lock()

total_added     = 0
seen_ids        = set()
sf_domain_cache = {}


# ════════════════════════════════════════════
#  SALESFORCE
# ════════════════════════════════════════════

def load_sf_domains():
    global sf_domain_cache
    print("[SF] Connecting to Salesforce...")
    try:
        sf = Salesforce(
            username=SF_USERNAME,
            password=SF_PASSWORD,
            security_token=SF_SECURITY_TOKEN,
            instance_url=SF_INSTANCE_URL,
        )
        query   = "SELECT Name, Shopify_Domain__c FROM Account WHERE Shopify_Domain__c != null"
        result  = sf.query_all(query)
        records = result.get("records", [])

        with sf_cache_lock:
            for rec in records:
                name   = (rec.get("Name") or "").strip()
                domain = (rec.get("Shopify_Domain__c") or "").strip()
                if name and domain:
                    sf_domain_cache[name.lower()] = domain

        print(f"[SF] Loaded {len(sf_domain_cache)} store → domain mappings.")

    except Exception as e:
        print(f"[SF] Error: {e} — domain column will be empty.")
        sf_domain_cache = {}


def get_domain(store_name):
    with sf_cache_lock:
        return sf_domain_cache.get(store_name.strip().lower(), "")


# ════════════════════════════════════════════
#  GOOGLE SHEETS
# ════════════════════════════════════════════

def connect_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(CREDS_JSON)
    creds      = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client     = gspread.authorize(creds)
    sheet      = client.open_by_key(SHEET_ID)

    try:
        ws = sheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=WORKSHEET_NAME, rows="10000", cols="20")

    existing = ws.row_values(1) if ws.row_count > 0 else []
    if existing != SHEET_HEADERS:
        ws.update("A1", [SHEET_HEADERS])
        print("[SHEET] Header row updated.")

    return ws


def load_existing_ids(ws):
    global seen_ids
    try:
        all_ids  = ws.col_values(1)[1:]
        seen_ids = set(filter(None, all_ids))
        print(f"[SHEET] {len(seen_ids)} existing review IDs loaded.")
    except Exception as e:
        print(f"[SHEET] Could not load IDs: {e}")
        seen_ids = set()


def append_rows(ws, rows):
    if not rows:
        return 0
    with sheet_lock:
        for attempt in range(1, 4):
            try:
                ws.append_rows(rows, value_input_option="USER_ENTERED")
                return len(rows)
            except Exception as e:
                print(f"[SHEET] Write error (attempt {attempt}): {e}")
                time.sleep(5 * attempt)
    return 0


# ════════════════════════════════════════════
#  SCRAPING
# ════════════════════════════════════════════

def get_total_pages(rating):
    try:
        resp  = requests.get(BASE_URL, params={"ratings[]": rating, "page": 1},
                             headers=HEADERS, timeout=20)
        soup  = BeautifulSoup(resp.text, "html.parser")
        pages = soup.find_all("a", attrs={"aria-label": re.compile(r"Page \d+")})
        if pages:
            return max(int(re.search(r"\d+", a["aria-label"]).group()) for a in pages)
        reviews = soup.find_all("div", attrs={"data-merchant-review": ""})
        return 1 if reviews else 0
    except Exception as e:
        print(f"[★{rating}] Page detection error: {e}")
        return 1


def parse_page(html, rating):
    soup    = BeautifulSoup(html, "html.parser")
    divs    = soup.find_all("div", attrs={"data-merchant-review": ""})
    reviews = []

    for div in divs:
        # ── Review ID ──
        parent    = div.find_parent("div", attrs={"id": re.compile(r"review-\d+")})
        review_id = parent["id"].replace("review-", "") if parent else ""

        # ── Store name ──
        store_span = div.find("span", attrs={"title": True})
        store_name = store_span["title"] if store_span else ""

        # ── Country & Duration ──
        country, duration = "", ""
        sidebar = div.find("div", class_=lambda c: c and "tw-order-1" in c and "tw-space-y-1" in c)
        if sidebar:
            plain = [
                d for d in sidebar.find_all("div", recursive=False)
                if not d.find("span") and not d.find("button")
            ]
            if len(plain) >= 1: country  = plain[0].get_text(strip=True)
            if len(plain) >= 2: duration = plain[1].get_text(strip=True)

        # ── Date ──
        date_div = div.find("div", class_=lambda c: c and "tw-text-fg-tertiary" in c
                            and "tw-text-body-xs" in c)
        date = date_div.get_text(strip=True) if date_div else ""

        # ── Review text ──
        content = div.find("div", attrs={"data-truncate-content-copy": True})
        text    = content.get_text(separator=" ", strip=True) if content else ""

        # ── Loop Reply ──
        loop_reply      = ""
        loop_reply_date = ""

        reply_section = div.find("div", attrs={"data-merchant-review-reply": ""})
        if reply_section:
            reply_meta = reply_section.find(
                "div", class_=lambda c: c and "tw-text-fg-tertiary" in c and "tw-text-body-xs" in c
            )
            if reply_meta:
                meta_text = reply_meta.get_text(separator="\n", strip=True)
                lines = [l.strip() for l in meta_text.split("\n") if l.strip()]
                for line in lines:
                    if re.search(
                        r"(January|February|March|April|May|June|July|August|"
                        r"September|October|November|December)", line
                    ):
                        loop_reply_date = line
                        break
                else:
                    print(f"[WARN] Could not parse reply date for review {review_id}: {lines}")

            reply_content = reply_section.find("div", attrs={"data-truncate-content-copy": True})
            if reply_content:
                loop_reply = reply_content.get_text(separator=" ", strip=True)

        # ── Salesforce domain ──
        shopify_domain = get_domain(store_name)

        reviews.append({
            "review_id":       review_id,
            "rating":          rating,
            "store_name":      store_name,
            "shopify_domain":  shopify_domain,
            "country":         country,
            "duration":        duration,
            "date":            date,
            "review":          text,
            "loop_reply":      loop_reply,
            "loop_reply_date": loop_reply_date,
        })

    return reviews


def scrape_page(ws, rating, page_num, retries=3):
    global total_added

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                BASE_URL,
                params={"ratings[]": rating, "page": page_num},
                headers=HEADERS, timeout=20
            )

            if resp.status_code == 429:
                time.sleep(15 * attempt)
                continue
            if resp.status_code != 200:
                time.sleep(4 * attempt)
                continue

            reviews = parse_page(resp.text, rating)
            if not reviews:
                return 0

            # Deduplicate
            new_reviews = []
            with seen_lock:
                for r in reviews:
                    if r["review_id"] and r["review_id"] not in seen_ids:
                        seen_ids.add(r["review_id"])
                        new_reviews.append(r)

            if not new_reviews:
                return 0

            now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            rows = [
                [
                    r["review_id"], r["rating"], r["store_name"],
                    r["shopify_domain"], r["country"], r["duration"],
                    r["date"], r["review"],
                    r["loop_reply"], r["loop_reply_date"],
                    now
                ]
                for r in new_reviews
            ]

            added = append_rows(ws, rows)
            with counter_lock:
                total_added += added
                current      = total_added

            print(f"[★{rating} P{page_num:>3}] ✅ +{added} new | Total: {current}")
            return added

        except Exception as e:
            print(f"[★{rating} P{page_num}] Error (attempt {attempt}): {e}")
            time.sleep(4 * attempt)

    return 0


# ════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════

def main():
    print(f"{'='*55}")
    print(f"  Loop Reviews Scraper — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Threads: {THREADS}")
    print(f"{'='*55}")

    load_sf_domains()

    ws = connect_sheet()
    load_existing_ids(ws)

    tasks = []
    print("[FULL] Detecting total pages per rating...")
    for rating in RATINGS:
        pages = get_total_pages(rating)
        print(f"  ★{rating} → {pages} pages")
        for page in range(1, pages + 1):
            tasks.append((rating, page))
        time.sleep(1)
    print(f"\n[FULL] Total tasks: {len(tasks)}\n")

    start = time.time()

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {
            executor.submit(scrape_page, ws, r, p): (r, p)
            for r, p in tasks
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                r, p = futures[future]
                print(f"[★{r} P{p}] Unhandled: {e}")

    elapsed = time.time() - start
    print(f"\n{'='*55}")
    print(f"  ✅ Done! New reviews added: {total_added}")
    print(f"  ⏱  Time: {elapsed:.1f}s")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
