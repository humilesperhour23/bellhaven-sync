"""Read-only connection test: calls GET /me and GET /accounts/{id}. Changes nothing."""
import json
import os

import requests
from dotenv import load_dotenv

BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"
ACCOUNT_ID = "001U6RW32TY0WSXZZB"

load_dotenv()
token = os.getenv("CRM_TOKEN")
if not token:
    raise SystemExit("CRM_TOKEN not found. Check your .env file.")

headers = {"Authorization": f"Bearer {token}"}

for path in ["/me", f"/accounts/{ACCOUNT_ID}"]:
    resp = requests.get(BASE_URL + path, headers=headers, timeout=30)
    print(f"\n=== GET {path} -> HTTP {resp.status_code} ===")
    try:
        data = resp.json()
        print(json.dumps(data, indent=2))
    except ValueError:
        print(resp.text)
        continue
    if path.startswith("/accounts") and isinstance(data, dict):
        print("\nAccount field names:")
        for key in data:
            print(f"  - {key}")
