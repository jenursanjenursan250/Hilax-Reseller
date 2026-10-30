"""Small client for the 3x-ui panel HTTP API (cookie-session login, auto re-login)."""
import json
import logging
import re
import threading
import uuid

import requests

log = logging.getLogger(__name__)

GB = 1024 ** 3


class XUIError(Exception):
    """Raised for any problem talking to the panel."""


class XUIClient:
    def __init__(self, base_url: str, username: str, password: str,
                 verify_ssl: bool = True, timeout: int = 15):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.verify = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()
        # Some firewalls / reverse proxies reject the default "python-requests" User-Agent with 403.
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        })
        self._logged_in = False
        self._lock = threading.RLock()
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------------ core
    @staticmethod
    def _json(resp):
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _snippet(resp, limit: int = 120) -> str:
        text = re.sub(r"<[^>]+>", " ", resp.text or "")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]

    @staticmethod
    def _hint(status: int) -> str:
        if status == 403:
            return ("The panel or a firewall in front of it BLOCKED the request. Check: the panel's "
                    "'Panel Domain' setting matches the host in XUI_URL, the VPS firewall/fail2ban "
                    "does not block this server's IP, and no proxy/WAF rejects it.")
        if status == 404:
            return "Wrong web base path in XUI_URL (it is case-sensitive)."
        if status in (301, 302, 307, 308):
            return "The panel redirected: try https:// instead of http:// (or the reverse) in XUI_URL."
        return "Check XUI_URL, the port and the web base path."

    def _login(self):
        try:
            resp = self.session.post(
                f"{self.base}/login",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout, verify=self.verify,
            )
        except requests.RequestException as exc:
            raise XUIError(f"Cannot reach the panel: {exc}") from exc
        data = self._json(resp)
        if data is None:
            snippet = self._snippet(resp)
            raise XUIError(
                f"Login got a non-JSON reply (HTTP {resp.status_code}"
                f"{', server: ' + resp.headers['Server'] if resp.headers.get('Server') else ''}"
                f"{', body: ' + repr(snippet) if snippet else ''}). {self._hint(resp.status_code)}"
            )
        if not data.get("success"):
            raise XUIError(f"Panel login failed: {data.get('msg') or 'wrong username/password?'}")
        self._logged_in = True
        log.info("Logged in to 3x-ui panel")

    def _call(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base}{path}"
        with self._lock:
            if not self._logged_in:
                self._login()
            for attempt in (1, 2):
                try:
                    resp = self.session.request(
                        method, url, timeout=self.timeout, verify=self.verify, **kwargs
                    )
                except requests.RequestException as exc:
                    raise XUIError(f"Cannot reach the panel: {exc}") from exc
                data = self._json(resp)
                # Expired session: panel answers 404/401 or returns the HTML login page.
                if data is None or resp.status_code in (401, 403, 404):
                    if attempt == 1:
                        self._logged_in = False
                        self._login()
                        continue
                    raise XUIError(f"Unexpected panel reply (HTTP {resp.status_code}) for {path}")
                if not data.get("success", False):
                    raise XUIError(data.get("msg") or "Panel reported a failure")
                return data
        raise XUIError("Panel request failed")  # pragma: no cover

    # ------------------------------------------------------------------- API
    def check(self) -> bool:
        """Force a fresh login; raises XUIError on any problem."""
        with self._lock:
            self._logged_in = False
            self._login()
        return True

    @staticmethod
    def _client_dict(client_uuid, email, sub_id, total_gb, expiry_ms,
                     limit_ip=0, flow="", enable=True) -> dict:
        return {
            "id": client_uuid,
            "flow": flow,
            "email": email,
            "limitIp": limit_ip,
            "totalGB": int(round(total_gb * GB)),
            "expiryTime": int(expiry_ms),
            "enable": enable,
            "tgId": "",
            "subId": sub_id,
            "comment": "",
            "reset": 0,
        }

    def add_client(self, inbound_id: int, email: str, total_gb: float,
                   expiry_ms: int = 0, limit_ip: int = 0, flow: str = "") -> dict:
        client_uuid = str(uuid.uuid4())
        sub_id = uuid.uuid4().hex[:16]
        client = self._client_dict(client_uuid, email, sub_id, total_gb,
                                   expiry_ms, limit_ip, flow)
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client]})}
        self._call("POST", "/panel/api/inbounds/addClient", data=payload)
        return {"uuid": client_uuid, "sub_id": sub_id}

    def update_client_total(self, inbound_id: int, client_uuid: str, email: str,
                            sub_id: str, total_gb: float, expiry_ms: int = 0,
                            limit_ip: int = 0, flow: str = ""):
        client = self._client_dict(client_uuid, email, sub_id, total_gb,
                                   expiry_ms, limit_ip, flow)
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client]})}
        self._call("POST", f"/panel/api/inbounds/updateClient/{client_uuid}", data=payload)

    def delete_client(self, inbound_id: int, client_uuid: str):
        self._call("POST", f"/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}")

    def get_stats(self, inbound_id: int) -> dict:
        """Return {email: {up, down, total, expiryTime, enable, ...}} for the inbound."""
        data = self._call("GET", f"/panel/api/inbounds/get/{inbound_id}")
        obj = data.get("obj") or {}
        return {s["email"]: s for s in (obj.get("clientStats") or []) if "email" in s}
