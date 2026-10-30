"""Panel login diagnostic. Run:  python3 diagnose.py
Tries the login with the bot's headers and with the default python-requests headers, and prints
exactly what the panel answers. Does not print your password."""
import re
import sys

import requests

import config

url = config.XUI_URL + "/login"
print(f"Target: {url}  (verify_ssl={config.XUI_VERIFY_SSL})\n")
if not config.XUI_VERIFY_SSL:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BROWSER = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124.0 Mobile Safari/537.36",
           "Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"}

for label, headers in (("browser-like headers", BROWSER), ("default python-requests headers", {})):
    try:
        r = requests.post(url, data={"username": config.XUI_USERNAME, "password": config.XUI_PASSWORD},
                          headers=headers, timeout=15, verify=config.XUI_VERIFY_SSL, allow_redirects=False)
    except requests.RequestException as exc:
        print(f"[{label}] connection error: {exc}\n")
        continue
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text or "")).strip()[:200]
    print(f"[{label}] HTTP {r.status_code}  Server: {r.headers.get('Server', '-')}")
    if r.is_redirect:
        print(f"  redirects to: {r.headers.get('Location')}")
    print(f"  body: {body!r}\n")

print("HTTP 200 with {\"success\":true} = login works.  403 = blocked by panel domain setting / firewall / proxy."
      "  404 = wrong base path.  Connection error = wrong host/port or port closed.")
sys.exit(0)
