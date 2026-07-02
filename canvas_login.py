"""
Canvas Login Helper — writes canvas_cookies.json for the piazza bot.
Run this script once to store your Canvas session cookies locally.
Standard library only; no third-party packages required at import time.
"""
import json
from pathlib import Path


def main():
    print("Canvas Login Helper — writes canvas_cookies.json for the piazza bot")
    print()
    print("Step 1: Open your browser and go to https://canvas.ucsd.edu")
    print("Step 2: Log in with your UCSD credentials (SSO + Duo 2FA)")
    print("Step 3: Open DevTools (F12 or right-click > Inspect)")
    print("Step 4: Go to Application > Cookies > https://canvas.ucsd.edu")
    print("Step 5: Copy the value of the 'canvas_session' cookie")
    print()

    _try_automated_login()
    canvas_session = input("Paste canvas_session cookie value: ").strip()
    csrf = input("Paste _csrf_token cookie value (press Enter to skip): ").strip()

    cookies = {"canvas_session": canvas_session}
    if csrf:
        cookies["_csrf_token"] = csrf

    output_path = Path(__file__).parent / "canvas_cookies.json"
    output_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")

    print()
    print(f"Saved cookies to: {output_path}")
    print("The piazza bot will now use these cookies to access Canvas.")


def _try_automated_login():
    try:
        import playwright  # noqa: F401
        print("Playwright detected. Automated login not yet implemented — use manual paste above.")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
