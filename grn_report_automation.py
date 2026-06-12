import logging
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("grn_report")

# ── SupplyNote constants ──────────────────────────────────────────────────────
LOGIN_URL   = "https://www.supplynote.in/signin"
USERS_ME    = "https://www.supplynote.in/api/users/me"
OUTLETS_API = "https://www.supplynote.in/api/outlets"
REPORT_API  = "https://supplynote.in/api/reports/generate/itemWiseGRN"

API_HEADERS = {
    "Content-Type": "application/json",
    "Accept":       "application/json, text/plain, */*",
    "Origin":       "https://sn-ims-v2-angular.web.app",
    "Referer":      "https://sn-ims-v2-angular.web.app/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

def parse_credentials(creds_text: str) -> list:
    """Parses the custom multiline credentials format into a list of dicts."""
    accounts = []
    current_account = {}
    
    for line in creds_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
            
        if line.endswith(':'):
            if current_account.get("username") and current_account.get("password"):
                accounts.append(current_account)
            current_account = {"name": line[:-1]}
        elif line.lower().startswith("username:"):
            current_account["username"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith("password:"):
            current_account["password"] = line.split(":", 1)[1].strip()
            
    if current_account.get("username") and current_account.get("password"):
        accounts.append(current_account)
        
    return accounts

def login_with_playwright(username: str, password: str) -> dict:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    log.info(f"Starting Playwright login for {username}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=API_HEADERS["User-Agent"]
        )
        page = context.new_page()

        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1_000)

        page.fill('input[name="username"], input[name="email"], input[placeholder*="username" i], input[placeholder*="email" i]', username)
        page.fill('input[type="password"]', password)
        page.click('button[type="submit"]')

        try:
            page.wait_for_url(lambda url: "/signin" not in url and "/login" not in url, timeout=20_000)
        except PWTimeout:
            log.error(f"Playwright login failed for {username}.")
            browser.close()
            return None

        page.wait_for_timeout(2_000)
        raw_cookies = context.cookies()
        cookies_dict = {c["name"]: c["value"] for c in raw_cookies}
        log.info(f"Captured {len(cookies_dict)} session cookies for {username}.")
        browser.close()

    return cookies_dict

def fetch_user_data(cookies: dict) -> tuple:
    resp = requests.get(USERS_ME, headers=API_HEADERS, cookies=cookies, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    business_obj = data.get("buisness", {})
    business_id = business_obj.get("_id", "") if isinstance(business_obj, dict) else str(business_obj)
    
    email = data.get("email") or data.get("profile", {}).get("email") or "arahanth.yadav@curefoods.in"
    
    allowed_ids = []
    for p in data.get("permissions", []):
        if "outlet" in p and "_id" in p["outlet"]:
            allowed_ids.append(p["outlet"]["_id"])
            
    return business_id, allowed_ids, email

def fetch_outlets(cookies: dict, business_id: str, allowed_ids: list) -> list:
    url = f"{OUTLETS_API}?buisness={business_id}"
    resp = requests.get(url, headers=API_HEADERS, cookies=cookies, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    outlets = []
    allowed_set = set(allowed_ids)
    
    for o in data:
        if o.get("_id") in allowed_set:
            outlets.append({
                "id": o.get("_id"),
                "outletName": o.get("name", "")
            })
            
    return outlets

def generate_grn_report(cookies: dict, outlets: list, email: str) -> bool:
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    
    # Yesterday 00:00:00 to Yesterday 23:59:59 (in IST, then format to UTC as required by API)
    # The API payload had fromDate and toDate in UTC format.
    yesterday = now - timedelta(days=1)
    
    from_dt_ist = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    to_dt_ist = yesterday.replace(hour=23, minute=59, second=59, microsecond=999000)
    
    from_dt_utc = from_dt_ist.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to_dt_utc = to_dt_ist.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    log.info(f"Generating report for Date Range: {from_dt_utc} to {to_dt_utc}")
    
    # We include all possible filters as seen in the HAR payload
    filters = ["Sku Code", "Category", "Sub Category", "Item Name", "Unit", "GRN No.", "HSN No.", "PO No.", 
               "Remarks", "Created By", "GRN Created At", "Seller Invoice No", "Supplier Invoice Date", 
               "Supplier", "Concerned Person", "Pickup Location", "Pickup GSTIN", "Pickup Code", "Pickup City", 
               "Pickup State", "Delivery Location", "Delivery GSTIN", "Delivery Code", "Delivery City", 
               "Delivery State", "Price", "Received Qty", "Returned Qty", "Discount", "Tax", "SGST Tax", 
               "SGST Tax Amount", "CGST Tax", "CGST Tax Amount", "IGST Tax", "IGST Tax Amount", "cess", 
               "SubTotal", "VAT(%)", "VAT(Amount)", "Item TCS(%)", "Item TCS(Amount)", "Tax Amount", 
               "SubTotal", "Bill TCS", "Delivery Charges", "Delivery Charges Tax(%)", "Additional Charges", 
               "INV Discount", "RoundOff", "Total"]
               
    filter_list = [{"name": f, "selected": True} for f in filters]

    payload = {
        "fromDate": from_dt_utc,
        "toDate": to_dt_utc,
        "filterList": filter_list,
        "outletListId": outlets,
        "fromReport": True,
        "interStockTransfer": True,
        "intraStockTransfer": False,
        "sendAll": True,
        "skip": 0,
        "limit": 15,
        "timeZone": "Asia/Calcutta",
        "job": {},
        "action": "mail",
        "emails": [email],
        "outlets": [] # Note: The API might expect the full outlets objects here, but often it just relies on outletListId. If it fails, we will need to fetch the full outlet objects instead of just id/name.
    }

    resp = requests.post(REPORT_API, json=payload, headers=API_HEADERS, cookies=cookies, timeout=120)
    log.info(f"Generate Report Response: {resp.status_code} - {resp.text[:200]}")
    
    return resp.status_code in (200, 201)

def run():
    creds_text = os.environ.get("SUPPLYNOTE_CREDENTIALS")
    if not creds_text:
        log.error("SUPPLYNOTE_CREDENTIALS environment variable is not set.")
        return

    accounts = parse_credentials(creds_text)
    log.info(f"Found {len(accounts)} accounts to process.")

    for account in accounts:
        username = account["username"]
        password = account["password"]
        acc_name = account.get("name", username)
        
        log.info("-" * 50)
        log.info(f"Processing Account: {acc_name} ({username})")
        
        try:
            cookies = login_with_playwright(username, password)
            if not cookies:
                continue
                
            business_id, allowed_ids, email = fetch_user_data(cookies)
            log.info(f"Logged in. Business ID: {business_id}. Target Email: {email}. Assigned Outlets: {len(allowed_ids)}")
            
            if not allowed_ids:
                log.warning("No outlets assigned to this user. Skipping report generation.")
                continue
                
            outlets = fetch_outlets(cookies, business_id, allowed_ids)
            log.info(f"Fetched {len(outlets)} accessible outlets.")
            
            success = generate_grn_report(cookies, outlets, email)
            if success:
                log.info(f"Successfully triggered GRN report for {acc_name}.")
            else:
                log.error(f"Failed to trigger GRN report for {acc_name}.")
                
        except Exception as e:
            log.error(f"Error processing account {username}: {e}")

if __name__ == "__main__":
    run()
