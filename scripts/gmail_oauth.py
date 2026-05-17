#!/usr/bin/env python3
"""One-shot Gmail OAuth bootstrap.

Opens a browser, handles the redirect on a random localhost port
(matching Google's `installed` app flow), exchanges the code for tokens,
and writes ~/.gmail-mcp/credentials.json.

Run once:  .venv/bin/python scripts/gmail_oauth.py
"""

import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow

KEYS_PATH = os.path.expanduser("~/.gmail-mcp/gcp-oauth.keys.json")
CREDS_PATH = os.path.expanduser("~/.gmail-mcp/credentials.json")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]


def main():
    print("Gmail OAuth bootstrap")
    print(f"Keys:  {KEYS_PATH}")
    print(f"Creds: {CREDS_PATH}")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(KEYS_PATH, scopes=SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)

    os.makedirs(os.path.dirname(CREDS_PATH), exist_ok=True)
    token_data = {
        "type": "authorized_user",
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
    }
    with open(CREDS_PATH, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nSUCCESS: credentials written to {CREDS_PATH}")
    print("Restart the bot to use Gmail.")


if __name__ == "__main__":
    main()
