## OrangePi Wi‑Fi Manager with Waylo AI Integration

This project provides automatic Wi‑Fi connectivity management with seamless integration to the Waylo AI system. It automatically connects to known Wi‑Fi networks on boot, and if no known network is reachable, starts a hotspot with a captive portal for credential entry.

### High‑level Behavior
- **Boot**: Try client mode first. If known SSIDs are in range, connect. If not, start AP + portal.
- **AP mode**: SSID `OrangePi-AP`, PSK `orangepi123` (configurable). Portal page accepts SSID/PSK.
- **After submit**: Portal writes credentials to `/etc/wpa_supplicant/wpa_supplicant.conf`, monitor stops AP, starts client, obtains DHCP.
- **AI Integration**: When network connectivity is established, automatically triggers the complete Waylo AI startup sequence.

---

## System Architecture

### WiFi Management (wifi-setup)
- **WiFi Monitor**: Manages network connectivity and AP/client mode switching
- **Captive Portal**: Web interface for entering new Wi-Fi credentials
- **Network Detection**: Automatically detects and connects to known networks

### Waylo AI Integration (Waylo_AI)
- **BLE Handoff Service**: Handles device pairing and MAC address exchange with iOS app
- **AI Chat Service**: Main conversational AI application for children
- **Startup Orchestration**: Manages the complete startup sequence
- **Secure Authentication**: All backend API calls use Bearer token authentication

### Integration Flow
```
WiFi Monitor → Network Connected → Complete Startup Sequence → BLE Handoff → AI Chat
```

### Security Architecture
- **Authentication Required**: All backend API calls require valid Bearer tokens
- **Credential Exchange**: iOS app sends user credentials via BLE to OrangePi
- **Token Management**: Automatic token refresh and expiration handling
- **Secure Communication**: No unauthorized access to backend APIs possible

---

## Components

### Systemd Services
- **`hotspot.service`** → orchestrator, runs `wifi-monitor.sh`
- **`captive-portal.service`** → Flask app serving portal (started during AP)
- **`wailo-ble-handoff.service`** → handles BLE pairing and MAC exchange
- **`wailo.service`** → AI Chat application (started after BLE handoff)
- **`audio-controller.service`** → REST API for audio volume control (independent service)

### Core Scripts
- **`/usr/local/bin/wifi-monitor.sh`** → main state machine (client ↔ AP, triggers AI startup)
- **`/usr/local/bin/start-ap.sh`** → brings up hostapd + dnsmasq on `wlan0`
- **`/usr/local/bin/portal-apply-creds.sh`** → safely updates `wpa_supplicant.conf` and signals monitor
- **`/home/orangepi/Waylo_AI/complete_startup_sequence.sh`** → orchestrates complete AI startup
- **`/home/orangepi/Waylo_AI/wailo_gatt_server.py`** → BLE GATT server for device pairing
- **`/home/orangepi/Waylo_AI/audio_controller.py`** → Flask API for audio volume control

### Configs
- **`/etc/wpa_supplicant/wpa_supplicant.conf`** → known networks
- **`/etc/dnsmasq.conf`** → DHCP/DNS for AP clients (serves 192.168.4.0/24)
- **`/etc/NetworkManager/conf.d/ignore-wlan0.conf`** → prevents NM from managing `wlan0`
- **`/etc/sudoers.d/99-wifi-portal`** → allow portal to run `portal-apply-creds.sh` as root

### Logs
- **`/tmp/wifi-monitor.log`** → monitor
- **`/tmp/wpa_supplicant.log`** → client association
- **`/tmp/wailo_gatt_server.log`** → BLE handoff
- **`journalctl -u hotspot`**, **`-u captive-portal`**, **`-u wailo-ble-handoff`**, **`-u wailo`**

---

## Requirements

### Base System Packages
```bash
sudo apt-get update
sudo apt-get install -y hostapd dnsmasq iw wireless-tools wpasupplicant resolvconf
# Optional: if Avahi interferes (we auto-kill avahi-autoipd in scripts)
sudo systemctl disable --now avahi-daemon.service avahi-daemon.socket avahi-autoipd.service || true
```

### Portal Prerequisites
```bash
# Create and use a venv for the portal
python3 -m venv /home/orangepi/wailo-env
/home/orangepi/wailo-env/bin/pip install --upgrade pip
/home/orangepi/wailo-env/bin/pip install flask

# Your portal app should be at
#   /home/orangepi/captive-portal/app.py
# and listen on 0.0.0.0 (common Flask pattern: app.run(host="0.0.0.0", port=5000))
```

### Waylo AI Prerequisites
```bash
# Install Python dependencies for AI Chat
cd /home/orangepi/Waylo_AI
/home/orangepi/wailo-env/bin/pip install -r requirements.txt

# Ensure scripts are executable
chmod +x /home/orangepi/Waylo_AI/complete_startup_sequence.sh
chmod +x /home/orangepi/Waylo_AI/wailo_gatt_server.py
chmod +x /home/orangepi/Waylo_AI/audio_controller.py
```

---

## Installation

### 1. Link from this repo
```bash
cd /home/orangepi/wifi-setup
make link           # sudo ./scripts/link.sh
make reload         # sudo ./scripts/reload-services.sh
```

### 2. Enable Required Services
```bash
# WiFi management services
sudo systemctl enable hotspot.service
sudo systemctl enable captive-portal.service

# Waylo AI services
sudo systemctl enable wailo-ble-handoff.service
sudo systemctl enable wailo.service

# Audio controller service (independent)
sudo systemctl enable audio-controller.service

# Disable the old auto-startup service (no longer needed)
sudo systemctl disable wailo-complete-startup.service
```

### 3. Start Services
```bash
sudo systemctl start hotspot.service
sudo systemctl start captive-portal.service
```

---

## How It Works

### 1. WiFi Connectivity Phase
- **WiFi Monitor** starts and attempts to connect to known networks
- If successful, obtains IP address and internet connectivity
- If no known networks or connection fails, starts AP mode with captive portal

### 2. AI Startup Trigger
- When **WiFi Monitor** detects successful network connectivity, it calls:
```bash
  /home/orangepi/Waylo_AI/complete_startup_sequence.sh
  ```

### 3. Complete Startup Sequence
The `complete_startup_sequence.sh` orchestrates:
- **Bluetooth Service**: Starts and configures Bluetooth adapter
- **BLE Handoff**: Runs GATT server for iOS app pairing
- **Credential Exchange**: iOS app sends user email/password via BLE
- **Authentication**: OrangePi authenticates with backend using received credentials
- **Token Management**: Bearer token obtained and stored for all API calls
- **MAC Exchange**: iOS app reads device MAC address for verification
- **Bluetooth Shutdown**: Automatically shuts down after successful handoff
- **AI Chat Start**: Starts the main AI Chat service with secure authentication

### 4. AI Chat Operation
- **AI Chat** runs continuously, providing voice-based interaction
- **Secure API Calls**: All backend communication uses Bearer token authentication
- **Automatic Authentication**: Token refresh handled automatically when expired
- **Parental Controls**: Respects time limits and do-not-disturb settings
- **Analytics**: Logs conversations, sentiment, and interest data with secure API calls

### 5. Audio Control (Independent Service)
- **Audio Controller** runs independently on port 5001
- **Volume Control**: REST API for microphone and speaker volume
- **Always Available**: Works regardless of WiFi or AI service status
- **iOS App Integration**: Direct control from React Native app

---

## Operation Details

- **AP network**: `192.168.4.1/24` on `wlan0` by `start-ap.sh`. `dnsmasq` serves DHCP.
- **Known networks detection**: parsed from `/etc/wpa_supplicant/wpa_supplicant.conf`.
- **Reachability check**: `iwlist scan` confirms configured SSIDs are in range before trying client.
- **Connection success**: considered successful when an IPv4 address is obtained on `wlan0`.
- **AI startup trigger**: happens automatically when `has_connectivity()` returns true.
- **State flags** (in `/tmp`):
  - `creds_ready` → portal signaled new credentials; monitor will try client
  - `ap_cooldown` → cooldown marker to avoid thrashing back to client
  - `client_connect_failed` → note for last failed attempt
  - `force_ap` → if present at service start, monitor will skip client and go AP directly
- **BLE handoff completion**: marked by `/home/orangepi/Waylo_AI/.bluetooth_handoff_complete`

---

## Using the System

### Initial Setup
1. Power on OrangePi where the known Wi‑Fi is not reachable (or clear networks per Troubleshooting).
2. Connect a phone/laptop to Wi‑Fi SSID `OrangePi-AP` (password `orangepi123`).
3. Your device should be redirected to the captive portal. If not, open:
   - http://192.168.4.1 or http://192.168.4.1:5000
4. Enter SSID and password, submit.
5. The AP will shut down and the device will connect to the provided network.

### Audio Control API
The audio controller provides REST API endpoints for controlling microphone and speaker volume:

#### **Get Current Volume Levels**
```bash
GET http://YOUR_ORANGEPI_IP:5001/api/audio/volume
```
Response: `{"microphone": 80, "speaker": 60}`

#### **Set Microphone Volume**
```bash
POST http://YOUR_ORANGEPI_IP:5001/api/audio/volume
Content-Type: application/json
{"microphone": 80}
```

#### **Set Speaker Volume**
```bash
POST http://YOUR_ORANGEPI_IP:5001/api/audio/volume
Content-Type: application/json
{"speaker": 60}
```

#### **Toggle Mute**
```bash
POST http://YOUR_ORANGEPI_IP:5001/api/audio/mute
Content-Type: application/json
{"microphone": true}  # or {"speaker": true}
```

#### **Get Detailed Audio Status**
```bash
GET http://YOUR_ORANGEPI_IP:5001/api/audio/status
```
Response: `{"microphone": {"volume": 80, "muted": false}, "speaker": {"volume": 60, "muted": false}}`

### AI System Activation
1. Once WiFi connects, the system automatically starts the complete startup sequence
2. Bluetooth activates and enters pairing mode
3. iOS app connects and verifies device MAC address
4. Bluetooth automatically shuts down
5. AI Chat service starts and is ready for interaction

---

## Security Features

### Backend API Security
- **🔐 Authentication Required**: All API calls to `https://app.waylo.ai` require valid Bearer tokens
- **🚫 No Unauthorized Access**: Random users cannot send POST/GET requests without proper authentication
- **🔄 Automatic Token Management**: Tokens are refreshed automatically when expired
- **📱 Device-Specific**: Each OrangePi device authenticates with unique user credentials

### Authentication Flow
1. **iOS App** → Sends user email/password via BLE to OrangePi
2. **OrangePi** → Authenticates with backend using `POST /token/signUp` or `POST /token`
3. **Backend** → Returns Bearer token for authenticated user
4. **OrangePi** → Uses Bearer token in all subsequent API calls
5. **Backend** → Validates token on every request, rejects unauthorized calls

### API Endpoints (All Require Authentication)
- `GET /toys/{MAC}` → Toy information
- `GET /toys/parental_controls/{MAC}` → Parental control settings
- `GET /users/{MAC}` → Child profile data
- `POST /logs/addRequestLog` → Log user requests
- `POST /logs/addResponseLog` → Log AI responses
- `POST /sentiments/addSentimentLog` → Log sentiment analysis
- `POST /interests/addInterestLog` → Log interest tracking
- `POST /toys/addMetaData/{MAC}` → Update device metadata

### Security Benefits
- **✅ User Isolation**: Each device only accesses its own user's data
- **✅ Credential Protection**: User credentials never stored permanently on device
- **✅ Token Expiration**: Tokens expire automatically for security
- **✅ No Public Access**: Backend APIs completely protected from unauthorized access

---

## Day‑to‑day Commands

### Status and Logs
```bash
# WiFi and portal status
systemctl status hotspot.service | cat
systemctl status captive-portal.service | cat
journalctl -u hotspot -f -n 60 | cat
journalctl -u captive-portal -f -n 60 | cat

# Waylo AI status
systemctl status wailo-ble-handoff.service | cat
systemctl status wailo.service | cat
journalctl -u wailo-ble-handoff -f -n 60 | cat
journalctl -u wailo -f -n 60 | cat

# Audio controller status
systemctl status audio-controller.service | cat
journalctl -u audio-controller -f -n 60 | cat

# Log files
tail -n 100 /tmp/wifi-monitor.log | cat
tail -n 100 /tmp/wpa_supplicant.log | cat
tail -n 100 /tmp/wailo_gatt_server.log | cat
```

### Manual Control
```bash
# Manually force AP on next restart
sudo touch /tmp/force_ap
sudo systemctl restart hotspot.service

# Manually trigger complete startup sequence
sudo /home/orangepi/Waylo_AI/complete_startup_sequence.sh

# Show Wi‑Fi state
ip -4 addr show wlan0
iw dev wlan0 info
iw dev wlan0 link
sudo iwlist wlan0 scan | sed -n '1,80p'
```

---

## Troubleshooting

### 1. AP never appears
- Check driver supports AP: `iw list` (look for AP in Supported interface modes).
- Logs: `journalctl -u hotspot -n 200 | cat` and `/tmp/wifi-monitor.log`.
- Try manual AP bring‑up:
```bash
sudo /usr/local/bin/start-ap.sh wlan0 OrangePi-AP orangepi123
```

### 2. Connected to AP but portal doesn't load
- Try `http://192.168.4.1` or `http://192.168.4.1:5000` directly.
- Check service: `systemctl status captive-portal | cat` and its logs.

### 3. WiFi connects but AI doesn't start
- Check if complete startup sequence is running: `ps aux | grep complete_startup_sequence`
- Check BLE handoff status: `ls -la /home/orangepi/Waylo_AI/.bluetooth_handoff_complete`
- Check service status: `systemctl status wailo-ble-handoff.service | cat`

### 4. Audio controller issues
- Check service status: `systemctl status audio-controller.service | cat`
- Check if port 5001 is accessible: `curl http://localhost:5001/api/audio/volume`
- Check logs: `journalctl -u audio-controller -f`
- Verify Flask is installed: `python3 -c "import flask; print('Flask OK')"`

### 5. BLE handoff never completes
- Check Bluetooth adapter: `bluetoothctl show`
- Check BLE service logs: `journalctl -u wailo-ble-handoff -f`
- Verify iOS app is attempting to connect
- Check if device is discoverable: `bluetoothctl discoverable on`

### 6. AI Chat service fails to start
- Check if BLE handoff completed: `ls -la /home/orangepi/Waylo_AI/.bluetooth_handoff_complete`
- Check service logs: `journalctl -u wailo -f`
- Verify Python environment: `/home/orangepi/wailo-env/bin/python --version`

### 7. Authentication issues
- Check if credentials were received: `cat /home/orangepi/Waylo_AI/received_firebase_token.txt`
- Verify Bearer token: `python3 -c "from wailo_api import WailoAPI; api = WailoAPI(); print('Token:', api.bearer_token[:20] if api.bearer_token else 'None')"`
- Test API authentication: `python3 -c "from wailo_api import WailoAPI; api = WailoAPI(); print('API Test:', api.toy_info())"`
- Check backend connectivity: `curl -I https://app.waylo.ai/token`

### 8. Reset Wi‑Fi interface manually
```bash
sudo pkill -x hostapd dnsmasq wpa_supplicant || true
sudo dhclient -r wlan0 || true
sudo ip addr flush dev wlan0
sudo iw dev wlan0 set type managed
sudo ip link set wlan0 up
```

### 9. Start fresh with no known networks
```bash
sudo cp /etc/wpa_supplicant/wpa_supplicant.conf{,.bak}
sudo sed -i '/^network[[:space:]]*=\{/,/^}/d' /etc/wpa_supplicant/wpa_supplicant.conf
sudo systemctl restart hotspot.service
```

---

## Makefile Targets

```bash
make link     # symlink repo → system (uses sudo)
make reload   # daemon-reload + restart affected services
make unlink   # remove symlinks, restore backups where available
```

---

## File Map (repo → system)

```
dnsmasq.conf                 → /etc/dnsmasq.conf
wifi-monitor.sh              → /usr/local/bin/wifi-monitor.sh
start-ap.sh                  → /usr/local/bin/start-ap.sh
portal-apply-creds.sh        → /usr/local/bin/portal-apply-creds.sh
switch-to-client.sh          → /usr/local/bin/switch-to-client.sh
hotspot.service              → /etc/systemd/system/hotspot.service
captive-portal.service       → /etc/systemd/system/captive-portal.service
ignore-wlan0.conf            → /etc/NetworkManager/conf.d/ignore-wlan0.conf
wpa_supplicant.conf          → /etc/wpa_supplicant/wpa_supplicant.conf
sudoers.d/99-wifi-portal     → /etc/sudoers.d/99-wifi-portal
```

---

## Support

If something deviates from the above behavior, collect these and share:

```bash
# WiFi and portal logs
journalctl -u hotspot -b -n 300 | cat
journalctl -u captive-portal -b -n 200 | cat
tail -n 300 /tmp/wifi-monitor.log | cat
tail -n 300 /tmp/wpa_supplicant.log | cat

# Waylo AI logs
journalctl -u wailo-ble-handoff -b -n 200 | cat
journalctl -u wailo -b -n 200 | cat
tail -n 300 /tmp/wailo_gatt_server.log | cat

# Audio controller logs
journalctl -u audio-controller -b -n 200 | cat

# System state
iw dev wlan0 info; iw dev wlan0 link; ip -4 addr show wlan0
sudo iwlist wlan0 scan | sed -n '1,200p'
sudo sed -n '1,200l' /etc/wpa_supplicant/wpa_supplicant.conf
ls -la /home/orangepi/Waylo_AI/.bluetooth_handoff_complete
```


