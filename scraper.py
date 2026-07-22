"""
Loop Subscriptions - All Ratings Reviews Scraper (v2 — with archive reconcile)
- Saves new reviews to Google Sheets (duplicate-safe)
- Enriches each review with Shopify Domain from Salesforce Account
- Captures Loop's reply (if any) for each review
- NEW: tracks per-review 'status' (Live / Archived) + 'status_checked_at'.
  Every run scrapes the FULL live listing, then reconciles: any review_id in
  the sheet that is no longer live on the app store is marked "Archived".
  A completeness guard ensures we NEVER mark rows Archived off an incomplete
  scrape (a failed page or broken page-detection skips marking entirely).
"""

import requests
from bs4 import BeautifulSoup
import threading
import time
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
THREADS  = 10

# Reconcile safety: if the full live scrape yields fewer unique live IDs than
# this, OR any page hard-fails / page-detection breaks, we DO NOT mark anything
# Archived (an incomplete scrape must never wipe live reviews to "Archived").
MIN_EXPECTED_LIVE = 500

SITE_ROOT = "https://apps.shopify.com"
BASE_URL  = f"{SITE_ROOT}/loop-subscriptions/reviews"
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
    "scraped_at", "review_link",
    "status", "status_checked_at",
]
# Column letters for the two status columns (must match header order above)
STATUS_COL     = "M"   # 13th column
STATUS_TS_COL  = "N"   # 14th column

# Locks
sheet_lock    = threading.Lock()
counter_lock  = threading.Lock()
seen_lock     = threading.Lock()
sf_cache_lock = threading.Lock()
live_lock     = threading.Lock()

total_added     = 0
seen_ids        = set()
sf_domain_cache = {}

# Reconcile state
live_ids_this_run = set()               # every review_id seen LIVE during this run
scrape_incomplete = threading.Event()   # set if any page hard-fails / detection breaks
incomplete_reasons = []                 # human-readable reasons (for the log)
incomplete_lock   = threading.Lock()


def mark_incomplete(reason):
    """Flag the run as incomplete so archive-marking is skipped."""
    with incomplete_lock:
        incomplete_reasons.append(reason)
    scrape_incomplete.set()


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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

        print(f"[SF] Loaded {len(sf_domain_cache)} store -> domain mappings.")

    except Exception as e:
        print(f"[SF] Error: {e} - domain column will be empty.")
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
        # Named args => works on both gspread 5.x and 6.x (signature order changed).
        ws.update(range_name="A1", values=[SHEET_HEADERS], value_input_option="USER_ENTERED")
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


def reconcile_status(ws):
    """
    After a FULL live scrape, mark every sheet row Live/Archived by comparing
    its review_id against the set of IDs seen live this run.

    Guard: if the scrape was incomplete (a page hard-failed, page-detection
    broke, or too few live IDs were collected) we skip marking entirely so an
    incomplete run can never flip live reviews to "Archived".
    """
    live_count = len(live_ids_this_run)
    print(f"\n[RECONCILE] Live IDs seen this run: {live_count}")

    if scrape_incomplete.is_set():
        print("[RECONCILE] SKIPPED - scrape flagged incomplete:")
        for r in incomplete_reasons:
            print(f"            - {r}")
        print("[RECONCILE] No status columns were written (safety guard).")
        return

    if live_count < MIN_EXPECTED_LIVE:
        print(f"[RECONCILE] SKIPPED - only {live_count} live IDs "
              f"(< MIN_EXPECTED_LIVE={MIN_EXPECTED_LIVE}). Refusing to mark archived.")
        return

    try:
        sheet_ids = ws.col_values(1)   # includes header at index 0
    except Exception as e:
        print(f"[RECONCILE] Could not read review_id column: {e} - skipped.")
        return

    data_ids = sheet_ids[1:]           # drop header
    if not data_ids:
        print("[RECONCILE] No data rows to reconcile.")
        return

    ts = now_utc()
    values = []           # one [status, status_checked_at] pair per data row
    n_live = n_arch = 0
    for rid in data_ids:
        rid = (rid or "").strip()
        if not rid:
            values.append(["", ""])    # keep blank rows untouched-ish
            continue
        if rid in live_ids_this_run:
            values.append(["Live", ts])
            n_live += 1
        else:
            values.append(["Archived", ts])
            n_arch += 1

    last_row = 1 + len(values)         # header is row 1, data starts row 2
    cell_range = f"{STATUS_COL}2:{STATUS_TS_COL}{last_row}"

    with sheet_lock:
        for attempt in range(1, 4):
            try:
                ws.update(range_name=cell_range, values=values,
                          value_input_option="USER_ENTERED")
                break
            except Exception as e:
                print(f"[RECONCILE] Write error (attempt {attempt}): {e}")
                time.sleep(5 * attempt)
        else:
            print("[RECONCILE] FAILED to write status columns after retries.")
            return

    print(f"[RECONCILE] Done -> Live: {n_live} | Archived: {n_arch} "
          f"(range {cell_range})")


# ════════════════════════════════════════════
#  SCRAPING
# ════════════════════════════════════════════

def get_total_pages(rating, retries=4):
    """
    Robust page-count detection. On persistent failure we FLAG the run
    incomplete (instead of silently returning 1) so reconcile is skipped.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(BASE_URL, params={"ratings[]": rating, "page": 1},
                                headers=HEADERS, timeout=25)
            if resp.status_code == 429:
                time.sleep(10 * attempt)
                continue
            if resp.status_code != 200:
                time.sleep(3 * attempt)
                continue

            soup    = BeautifulSoup(resp.text, "html.parser")
            reviews = soup.find_all("div", attrs={"data-merchant-review": ""})
            if not reviews:
                return 0   # genuinely no reviews for this rating (e.g. 2-star)

            pages = soup.find_all("a", attrs={"aria-label": re.compile(r"Page \d+")})
            if pages:
                return max(int(re.search(r"\d+", a["aria-label"]).group()) for a in pages)
            return 1       # reviews exist but no pager => single page

        except Exception as e:
            print(f"[*{rating}] Page detection error (attempt {attempt}): {e}")
            time.sleep(3 * attempt)

    mark_incomplete(f"page-detection failed for rating {rating}")
    print(f"[*{rating}] Page detection FAILED after {retries} attempts - run flagged incomplete.")
    return 0


def build_review_link(div, review_id):
    """
    Per-review permalink. The working form is the share-link
    (https://apps.shopify.com/reviews/<id>); the /loop-subscriptions/reviews/<id>
    form 404s. Prefer the real data-review-share-link attr, fall back to review_id.
    """
    share_btn = div.find(attrs={"data-review-share-link": True})
    if share_btn:
        href = share_btn["data-review-share-link"].strip()
        return SITE_ROOT + href if href.startswith("/") else href
    if review_id:
        return f"{SITE_ROOT}/reviews/{review_id}"
    return ""


def parse_page(html, rating):
    soup    = BeautifulSoup(html, "html.parser")
    divs    = soup.find_all("div", attrs={"data-merchant-review": ""})
    reviews = []

    for div in divs:
        # ── Review ID (parent wrapper first, then content-id fallback) ──
        parent    = div.find_parent("div", attrs={"id": re.compile(r"review-\d+")})
        review_id = parent["id"].replace("review-", "") if parent else ""
        if not review_id:
            review_id = (div.get("data-review-content-id") or "").strip()

        # ── Review link ──
        review_link = build_review_link(div, review_id)

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

        # ── Date (restricted to the review body, NOT the reply block) ──
        reply_section_probe = div.find("div", attrs={"data-merchant-review-reply": ""})
        date = ""
        for cand in div.find_all("div", class_=lambda c: c and "tw-text-fg-tertiary" in c
                                 and "tw-text-body-xs" in c):
            # skip any date node that lives inside the reply section
            if reply_section_probe and reply_section_probe in cand.parents:
                continue
            date = cand.get_text(strip=True)
            break

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
            "review_link":     review_link,
        })

    return reviews


def scrape_page(ws, rating, page_num, retries=4):
    global total_added

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                BASE_URL,
                params={"ratings[]": rating, "page": page_num},
                headers=HEADERS, timeout=25
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

            # ── Collect LIVE ids (every review seen live this run, new or old) ──
            with live_lock:
                for r in reviews:
                    if r["review_id"]:
                        live_ids_this_run.add(r["review_id"])

            # Deduplicate against what's already in the sheet
            new_reviews = []
            with seen_lock:
                for r in reviews:
                    if r["review_id"] and r["review_id"] not in seen_ids:
                        seen_ids.add(r["review_id"])
                        new_reviews.append(r)

            if not new_reviews:
                return 0

            now  = now_utc()
            rows = [
                [
                    r["review_id"], r["rating"], r["store_name"],
                    r["shopify_domain"], r["country"], r["duration"],
                    r["date"], r["review"],
                    r["loop_reply"], r["loop_reply_date"],
                    now, r["review_link"],
                    "Live", now,                       # status, status_checked_at
                ]
                for r in new_reviews
            ]

            added = append_rows(ws, rows)
            with counter_lock:
                total_added += added
                current      = total_added

            print(f"[*{rating} P{page_num:>3}] +{added} new | Total: {current}")
            return added

        except Exception as e:
            print(f"[*{rating} P{page_num}] Error (attempt {attempt}): {e}")
            time.sleep(4 * attempt)

    # All retries exhausted -> this page's live reviews are unknown -> incomplete.
    mark_incomplete(f"page fetch failed: rating {rating}, page {page_num}")
    print(f"[*{rating} P{page_num}] FAILED after {retries} attempts - run flagged incomplete.")
    return 0


# ════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════

def main():
    print(f"{'='*55}")
    print(f"  Loop Reviews Scraper (v2) - {now_utc()}")
    print(f"  Threads: {THREADS}")
    print(f"{'='*55}")

    load_sf_domains()

    ws = connect_sheet()
    load_existing_ids(ws)

    tasks = []
    print("[FULL] Detecting total pages per rating...")
    for rating in RATINGS:
        pages = get_total_pages(rating)
        print(f"  *{rating} -> {pages} pages")
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
                print(f"[*{r} P{p}] Unhandled: {e}")

    # Reconcile Live/Archived status across the whole sheet
    reconcile_status(ws)

    elapsed = time.time() - start
    print(f"\n{'='*55}")
    print(f"  Done! New reviews added: {total_added}")
    print(f"  Live IDs this run: {len(live_ids_this_run)}")
    print(f"  Scrape complete: {not scrape_incomplete.is_set()}")
    print(f"  Time: {elapsed:.1f}s")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
