# Reviews → Google Sheets Scraper

Automatically scrapes all ratings (1–5) from Loop Subscriptions Shopify app
and saves new reviews to Google Sheets via GitHub Actions.

---

## 📁 Files

```
reviews-scraper/
├── scraper.py                        # Main scraper
├── requirements.txt
└── .github/
    └── workflows/
        └── scraper.yml               # GitHub Actions workflow
```

---

## ⚙️ One-Time Setup

### 1. Google Service Account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project
3. Enable these APIs:
   - **Google Sheets API**
   - **Google Drive API**
4. Go to **IAM & Admin → Service Accounts → Create Service Account**
5. Click on the account → **Keys → Add Key → JSON** → Download
6. Open your Google Sheet → **Share** → paste the service account email → **Editor**

### 2. Get your Sheet ID

From your sheet URL:
```
https://docs.google.com/spreadsheets/d/  <<THIS_PART>>  /edit
```

---

## 🔐 GitHub Secrets Setup

Go to your repo → **Settings → Secrets and variables → Actions → New secret**

Add these 3 secrets:

| Secret Name               | Value                                      |
|---------------------------|--------------------------------------------|
| `SHEET_ID`                | Your Google Sheet ID                       |
| `GOOGLE_CREDENTIALS_JSON` | Entire contents of the downloaded JSON key |
| `WORKSHEET_NAME`          | Tab name e.g. `Reviews`                    |

> **GOOGLE_CREDENTIALS_JSON** — open the downloaded JSON file,
> select all text, paste as the secret value.

---

## 🚀 How to Run

### First time — Full Scrape (all pages, all ratings)

Go to **Actions → Loop Reviews Scraper → Run workflow → mode: full**

This scrapes everything and fills your sheet.

### Automatic Updates (every 6 hours)

The workflow runs automatically via cron `0 */6 * * *`.
It checks pages 1–3 per rating and adds only NEW reviews.

### Manual Update Run

**Actions → Loop Reviews Scraper → Run workflow → mode: update**

---

## 📊 Google Sheet Columns

| Column       | Description                        |
|--------------|------------------------------------|
| review_id    | Unique review ID (dedup key)       |
| rating       | Star rating (1–5)                  |
| store_name   | Reviewer's store name              |
| country      | Reviewer's country                 |
| duration     | How long they've used the app      |
| date         | Review date                        |
| review       | Full review text                   |
| scraped_at   | UTC timestamp when saved           |
