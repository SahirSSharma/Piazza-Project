"""
Canvas Login Helper — writes canvas_cookies.json for the piazza bot.

Preferred flow (automated): opens a real browser window at canvas.ucsd.edu, you
complete UCSD SSO + Duo, and the script captures the session cookies itself.
Requires playwright (uses the system Python 3.13 install if the venv lacks it):
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 canvas_login.py

Fallback flow (manual): if playwright is unavailable, prints instructions and
lets you paste the canvas_session cookie from DevTools.
"""
import json
import sys
import time
from pathlib import Path

CANVAS_URL = "https://canvas.ucsd.edu"
LOGIN_TIMEOUT_S = 300  # generous window for SSO + Duo
OUTPUT_PATH = Path(__file__).parent / "canvas_cookies.json"


def save_cookies(cookies: dict):
    OUTPUT_PATH.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    print(f"\nSaved cookies to: {OUTPUT_PATH}")
    print("The piazza bot will now use these cookies to access Canvas.")


def automated_login() -> bool:
    """Open a headed browser, wait for the user to finish logging in, capture cookies."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    print("Opening a browser window at canvas.ucsd.edu ...")
    print("Log in with your UCSD credentials (SSO + Duo). I'll grab the cookies once you're in.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(CANVAS_URL)

        deadline = time.time() + LOGIN_TIMEOUT_S
        session_cookies = None
        while time.time() < deadline:
            jar = {c["name"]: c["value"] for c in context.cookies(CANVAS_URL)}
            # canvas_session appears pre-login too; require the dashboard to confirm auth
            if "canvas_session" in jar and page.url.startswith(CANVAS_URL) and "/login" not in page.url:
                try:
                    if page.locator("#dashboard, #DashboardCard_Container, #dashboard-activity").count() > 0:
                        session_cookies = jar
                        break
                except Exception:
                    pass  # page mid-navigation; retry
            time.sleep(2)

        if not session_cookies:
            print(f"Timed out after {LOGIN_TIMEOUT_S}s without a completed login.")
            browser.close()
            return False

        browser.close()

    save_cookies(session_cookies)
    return True


def manual_login():
    print("Step 1: Open your browser and go to https://canvas.ucsd.edu")
    print("Step 2: Log in with your UCSD credentials (SSO + Duo 2FA)")
    print("Step 3: Open DevTools (F12 or right-click > Inspect)")
    print("Step 4: Go to Application > Cookies > https://canvas.ucsd.edu")
    print("Step 5: Copy the value of the 'canvas_session' cookie")
    print()
    canvas_session = input("Paste canvas_session cookie value: ").strip()
    csrf = input("Paste _csrf_token cookie value (press Enter to skip): ").strip()

    cookies = {"canvas_session": canvas_session}
    if csrf:
        cookies["_csrf_token"] = csrf
    save_cookies(cookies)


def main():
    print("Canvas Login Helper — writes canvas_cookies.json for the piazza bot\n")
    if "--manual" not in sys.argv and automated_login():
        return
    manual_login()


if __name__ == "__main__":
    main()
