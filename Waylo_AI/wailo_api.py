#!/usr/bin/env python3
# wailo_api.py – minimal wrapper for Waylo backend with authentication

import os, uuid, logging, requests, time, json
from typing import Optional, Dict, Any

log = logging.getLogger("wailo-api")
log.setLevel(logging.INFO)  # always show POST status lines

BACKEND_URL = "https://app.waylo.ai"
TOKEN_FILE = "/home/orangepi/Waylo_AI/received_firebase_token.txt"
DEVICE_MAC_FILE = "/home/orangepi/Waylo_AI/.device_mac_address"

# ── util ───────────────────────────────────────────────────────────────
def board_mac() -> str:
    """Return device MAC address from .device_mac_address file"""
    try:
        # First try to read from the device MAC file
        if os.path.exists(DEVICE_MAC_FILE):
            with open(DEVICE_MAC_FILE, 'r') as f:
                mac = f.read().strip()
                if mac and len(mac) == 17:  # Valid MAC format
                    log.info(f"✅ Using device MAC from file: {mac}")
                    return mac.upper()
    except Exception as e:
        log.warning(f"Failed to read device MAC file: {e}")
    
    # Fallback to dynamic detection
    try:
        # Try to get Bluetooth controller MAC address first
        import subprocess
        out = subprocess.run(
            ['bluetoothctl', 'show'],
            capture_output=True, text=True, check=True
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('Controller '):  # "Controller AA:BB:CC:DD:EE:FF (public)..."
                mac = line.split()[1].upper()
                log.info(f"✅ Using Bluetooth MAC: {mac}")
                return mac
    except Exception as e:
        log.warning(f"board_mac(): Bluetooth MAC failed, falling back to network MAC: {e}")
    
    # Fallback to network interface MAC if Bluetooth fails
    try:
        for nic in os.listdir("/sys/class/net"):
            if nic != "lo":
                with open(f"/sys/class/net/{nic}/address") as f:
                    mac = f.read().strip().upper()
                    log.info(f"✅ Using network MAC: {mac}")
                    return mac
    except Exception as e:
        log.warning(f"board_mac(): Network MAC failed: {e}")
    
    # Final fallback to uuid-based MAC
    mac = ":".join(f"{uuid.getnode() >> (i * 8) & 0xff:02X}" for i in range(5, -1, -1))
    log.warning(f"⚠️ Using UUID-based MAC: {mac}")
    return mac


MAC_ADDRESS = board_mac()

# ── client ─────────────────────────────────────────────────────────────
class WailoAPI:
    def __init__(self, base_url: str = None):
        self.base = (base_url or BACKEND_URL).rstrip("/")
        self.hdrs = {"Content-Type": "application/json", "accept": "*/*"}
        # Keep connections warm on constrained devices
        import requests as _requests
        self.sess = _requests.Session()
        
        # Authentication state
        self.bearer_token = None
        self.token_expires_at = None
        self.email = None
        self.password = None
        self.auth_failed = False  # Flag to prevent retry loops
        
        # Try to authenticate on initialization
        self._load_credentials()
        if self.email and self.password:
            self.authenticate()

    def _load_credentials(self):
        """Load email and password from received_firebase_token.txt"""
        try:
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, 'r') as f:
                    content = f.read()
                
                # Parse the file content to extract email and password
                # The file contains: {"type":"credentials","total":4}{"email":"waqarsaeed533@gmail.com","password":"waqar121"}
                lines = content.split('\n')
                for line in lines:
                    if '"email"' in line and '"password"' in line:
                        # Extract email and password from the line
                        # Format: {"type":"credentials","total":4}{"email":"waqarsaeed533@gmail.com","password":"waqar121"}
                        try:
                            # Find the second JSON object (the one with email/password)
                            json_objects = []
                            brace_count = 0
                            start_idx = -1
                            
                            for i, char in enumerate(line):
                                if char == '{':
                                    if brace_count == 0:
                                        start_idx = i
                                    brace_count += 1
                                elif char == '}':
                                    brace_count -= 1
                                    if brace_count == 0 and start_idx != -1:
                                        json_objects.append(line[start_idx:i+1])
                                        start_idx = -1
                            
                            # Get the second JSON object (should contain email/password)
                            if len(json_objects) >= 2:
                                creds_json = json_objects[1]  # Second object
                                creds = json.loads(creds_json)
                                self.email = creds.get('email')
                                self.password = creds.get('password')
                                log.info(f"✅ Loaded credentials for: {self.email}")
                                break
                        except json.JSONDecodeError as e:
                            log.warning(f"Failed to parse credentials JSON: {e}")
                            continue
                            
        except Exception as e:
            log.warning(f"Failed to load credentials: {e}")

    def authenticate(self) -> bool:
        """Authenticate with backend and get Bearer token"""
        if not self.email or not self.password:
            log.warning("No credentials available for authentication")
            return False
            
        try:
            url = f"{self.base}/token/signUp"
            payload = {
                "email": self.email,
                "password": self.password
            }
            
            start = time.time()
            r = self.sess.post(url, json=payload, headers=self.hdrs, timeout=30)
            duration = time.time() - start
            
            if r.status_code == 201:
                response_data = r.json()
                self.bearer_token = response_data.get("token")
                if self.bearer_token:
                    # JWT tokens typically expire in 1 hour, set expiration
                    self.token_expires_at = time.time() + 3600  # 1 hour from now
                    log.info(f"✅ Authentication successful in {duration:.2f}s")
                    return True
                else:
                    log.error("❌ No token in response")
                    return False
            elif r.status_code == 400 and "already exists" in r.text:
                # User already exists, try login endpoint instead
                log.info("🔄 User already exists, trying login endpoint...")
                return self._try_login()
            else:
                log.error(f"❌ Authentication failed: {r.status_code} - {r.text}")
                return False
                
        except requests.exceptions.Timeout:
            log.warning("⚠️ Authentication timeout - API endpoint may be unavailable")
            log.warning("⚠️ Continuing without authentication - API calls may fail")
            self.auth_failed = True  # Set flag to prevent retries
            return False
        except Exception as e:
            log.error(f"❌ Authentication error: {e}")
            self.auth_failed = True  # Set flag to prevent retries
            return False

    def _try_login(self) -> bool:
        """Try login endpoint when user already exists"""
        try:
            url = f"{self.base}/token"
            payload = {
                "email": self.email,
                "password": self.password
            }
            
            start = time.time()
            r = self.sess.post(url, json=payload, headers=self.hdrs, timeout=30)
            duration = time.time() - start
            
            if r.status_code == 201:
                response_data = r.json()
                self.bearer_token = response_data.get("token")
                if self.bearer_token:
                    # JWT tokens typically expire in 1 hour, set expiration
                    self.token_expires_at = time.time() + 3600  # 1 hour from now
                    log.info(f"✅ Login successful in {duration:.2f}s")
                    return True
                else:
                    log.error("❌ No token in login response")
                    return False
            else:
                log.error(f"❌ Login failed: {r.status_code} - {r.text}")
                return False
                
        except requests.exceptions.Timeout:
            log.warning("⚠️ Login timeout - API endpoint may be unavailable")
            self.auth_failed = True
            return False
        except Exception as e:
            log.error(f"❌ Login error: {e}")
            self.auth_failed = True
            return False

    def _get_auth_headers(self) -> dict:
        """Get headers with Bearer token if available"""
        headers = self.hdrs.copy()
        if self.bearer_token and self._is_token_valid():
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _is_token_valid(self) -> bool:
        """Check if current token is still valid"""
        if not self.bearer_token or not self.token_expires_at:
            return False
        return time.time() < self.token_expires_at

    def _refresh_token_if_needed(self) -> bool:
        """Refresh token if it's expired or about to expire"""
        if self.auth_failed:
            return False  # Don't retry if auth already failed
        if not self._is_token_valid():
            log.info("🔄 Token expired, refreshing...")
            return self.authenticate()
        return True

    def _get(self, path: str):
        url = self.base + path
        start = time.time()
        try:
            # Ensure we have a valid token
            self._refresh_token_if_needed()
            headers = self._get_auth_headers()
            
            r = self.sess.get(url, headers=headers, timeout=4)
            duration = time.time() - start
            
            # Handle 401 Unauthorized - token might be invalid
            if r.status_code == 401 and not self.auth_failed:
                log.warning(f"❌ Unauthorized (401) for {path}, attempting token refresh")
                if self.authenticate():
                    # Retry with new token
                    headers = self._get_auth_headers()
                    r = self.sess.get(url, headers=headers, timeout=4)
                    duration = time.time() - start
            
            log.info(f"[GET] {path} → {r.status_code} in {duration:.2f}s")
            return r.json() if r.ok else None
        except Exception as e:
            log.warning("GET %s failed: %s", url, e)
            return None

    def _post(self, path: str, payload: dict):
        url = self.base + path
        start = time.time()
        try:
            # Ensure we have a valid token
            self._refresh_token_if_needed()
            headers = self._get_auth_headers()
            
            r = self.sess.post(url, json=payload, headers=headers, timeout=4)
            duration = time.time() - start
            
            # Handle 401 Unauthorized - token might be invalid
            if r.status_code == 401 and not self.auth_failed:
                log.warning(f"❌ Unauthorized (401) for {path}, attempting token refresh")
                if self.authenticate():
                    # Retry with new token
                    headers = self._get_auth_headers()
                    r = self.sess.post(url, json=payload, headers=headers, timeout=4)
                    duration = time.time() - start
            
            log.info(f"[POST] {path} → {r.status_code} in {duration:.2f}s | Payload: {payload}")
            return (
                r.json().get("id")
                if r.ok and r.headers.get("content-type", "").startswith("application/json")
                else None
            )
        except Exception as e:
            log.warning("POST %s failed: %s", url, e)
            return None

    # ── public GET helpers ──────────────────────────────────────────
    def toy_info(self) -> dict:
        return self._get(f"/toys/{MAC_ADDRESS}") or {}

    def parental_controls(self) -> dict:
        return self._get(f"/toys/parental_controls/{MAC_ADDRESS}") or {}

    def child_profile(self) -> dict:
        res = self._get(f"/users/{MAC_ADDRESS}") or {}
        data = res.get("data", res)
        return {
            "name":     data.get("firstName", "friend"),
            "age":      data.get("age"),
            "gender":   data.get("gender", ""),
            "language": data.get("language", "en"),
            "uid":      data.get("uid"),
            "toyname":  data.get("toyname"),
        }

    # ── public POST helpers ─────────────────────────────────────────
    def log_request(self, msg: str) -> Optional[str]:
        return self._post("/logs/addRequestLog", {
            "message": msg,
            "toy_mac_address": MAC_ADDRESS
        })

    def log_response(self, msg: str, req_id: str = None) -> Optional[str]:
        body = {
            "message": msg,
            "toy_mac_address": MAC_ADDRESS
        }
        if req_id:
            body["request_id"] = req_id
        return self._post("/logs/addResponseLog", body)

    def log_sentiment(self, msg: str, sentiment: str, intensity: float,
                      req_id: str = None, res_id: str = None):
        body = {
            "message": msg,
            "sentiment": sentiment,
            "intensity": intensity,
            "toy_mac_address": MAC_ADDRESS,
            "request_id": req_id,
            "response_id": res_id,
        }
        self._post("/sentiments/addSentimentLog", body)

    def log_interest(self, topic: str, intensity: float,
                     req_id: str = None, res_id: str = None):
        body = {
            "interest": topic,
            "intensity": intensity,
            "toy_mac_address": MAC_ADDRESS,
            "request_id": req_id,
            "response_id": res_id,
        }
        self._post("/interests/addInterestLog", body)

    def update_metadata(self, battery="100%", board_name="Unknown",
                        connection_status="connected") -> dict:
        return self._post(f"/toys/addMetaData/{MAC_ADDRESS}", {
            "Battery": battery,
            "boardName": board_name,
            "connectionStatus": connection_status
        })
