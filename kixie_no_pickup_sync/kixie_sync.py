import os
import csv
import time
import tempfile
from datetime import datetime, timezone
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from pymongo import MongoClient

load_dotenv()

KIXIE_EMAIL    = os.environ["KIXIE_EMAIL"]
KIXIE_PASSWORD = os.environ["KIXIE_PASSWORD"]
MONGO_URI      = os.environ["MONGO_URI"]
POWERLIST_URL  = "https://app.kixie.com/manage/powerlists/360347/view-contacts"


def normalize_phone(raw):
    """Strip everything except digits, then take last 10."""
    digits = "".join(c for c in str(raw) if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def download_csv(playwright) -> list[dict]:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    print("Logging in to Kixie...")
    page.goto("https://app.kixie.com/login", wait_until="networkidle")
    page.fill('input[type="email"], input[name="email"]', KIXIE_EMAIL)
    page.fill('input[type="password"], input[name="password"]', KIXIE_PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    time.sleep(3)

    print(f"Navigating to PowerList: {POWERLIST_URL}")
    page.goto(POWERLIST_URL, wait_until="networkidle")
    time.sleep(3)

    # Check if empty
    content = page.content()
    if "No records to display" in content:
        print("PowerList is empty — nothing to sync.")
        browser.close()
        return []

    print("Clicking CSV export button...")
    with page.expect_download() as download_info:
        # Click the button containing "CSV" text
        page.locator("button:has-text('CSV')").click()
    download = download_info.value

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tmp:
        tmp_path = tmp.name

    download.save_as(tmp_path)
    browser.close()

    print(f"CSV downloaded to {tmp_path}")
    rows = []
    with open(tmp_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    os.unlink(tmp_path)
    print(f"CSV contains {len(rows)} rows")
    return rows


def update_mongo(rows: list[dict]):
    client = MongoClient(MONGO_URI)
    db = client["acquisitions"]
    contacts = db["contacts"]

    updated = 0
    skipped_connected = 0
    not_found = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        # Find phone number column — Kixie exports as "Phone Number"
        raw_phone = row.get("Phone Number") or row.get("phone_number") or row.get("phoneNumber") or ""
        phone = normalize_phone(raw_phone)

        if not phone:
            print(f"Could not parse phone from row: {row}")
            continue

        # Never overwrite a connected record
        contact = contacts.find_one(
            {"phone": phone},
            {"_id": 1, "connection_status": 1}
        )

        if not contact:
            not_found += 1
            print(f"No MongoDB record found for {phone}")
            continue

        if contact.get("connection_status") == "connected":
            skipped_connected += 1
            print(f"Skipping {phone} — already connected")
            continue

        contacts.update_one(
            {"_id": contact["_id"]},
            {"$set": {
                "connection_status": "not_connected",
                "updated_at": {"$date": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"}
            }}
        )
        updated += 1
        print(f"Flagged {phone} as not_connected")

    client.close()
    print(f"\nDone. Updated: {updated} | Skipped (already connected): {skipped_connected} | Not found: {not_found}")
    return updated


def main():
    print(f"Starting Kixie No-Pickup sync at {datetime.now(timezone.utc).isoformat()}")
    with sync_playwright() as playwright:
        rows = download_csv(playwright)

    if not rows:
        print("No records to process. Exiting.")
        return

    update_mongo(rows)


if __name__ == "__main__":
    main()
