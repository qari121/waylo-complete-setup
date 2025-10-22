#!/usr/bin/env python3
"""
Wailo Custom GATT Server with MAC Address Characteristic
Based on working BlueZ example-gatt-server implementation
WITH PROPER BLE ADVERTISING
"""

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
import array
from gi.repository import GLib
import sys
import os
import time
import threading
import signal
import logging
import subprocess

# Set up logging for systemd service context
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/home/orangepi/Waylo_AI/wailo_gatt_server.log')
    ]
)
log = logging.getLogger(__name__)

mainloop = None
SHUTDOWN_TIMER = None
HANDOFF_COMPLETED = False
adv_obj = None  # Global reference to prevent garbage collection

BLUEZ_SERVICE_NAME = 'org.bluez'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
LE_ADVERTISING_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'
LE_ADVERTISEMENT_IFACE = 'org.bluez.LEAdvertisement1'
DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'

GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHRC_IFACE = 'org.bluez.GattCharacteristic1'
GATT_DESC_IFACE = 'org.bluez.GattDescriptor1'

# Wailo Service UUIDs
WAILO_SVC_UUID = '12345678-1234-1234-1234-123456789abc'
MAC_CHAR_UUID = '11111111-2222-3333-4444-555555555555'
TOKEN_CHAR_UUID = '33333333-4444-5555-6666-777777777777'

# Configuration
PAIRING_MODE_DURATION = 300  # 5 minutes

class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.freedesktop.DBus.Error.InvalidArgs'

class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.NotSupported'

class NotPermittedException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.NotPermitted'

class InvalidValueLengthException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.InvalidValueLength'

class FailedException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.Failed'

def get_device_mac():
    """Get device MAC address - persistent across reboots"""
    MAC_FILE = '/home/orangepi/Waylo_AI/.device_mac_address'
    
    # First, try to read from persistent storage
    if os.path.exists(MAC_FILE):
        try:
            with open(MAC_FILE, 'r') as f:
                stored_mac = f.read().strip()
                if stored_mac and len(stored_mac) == 17:  # Valid MAC format XX:XX:XX:XX:XX:XX
                    log.info(f"📱 Using stored MAC address: {stored_mac}")
                    return stored_mac
        except Exception as e:
            log.warning(f"Failed to read stored MAC: {e}")
    
    # If no stored MAC or invalid, get from device and store it
    try:
        log.info("🔍 Querying device for actual MAC address...")
        out = subprocess.run(
            ['bluetoothctl', 'show'],
            capture_output=True, text=True, check=True
        ).stdout
        
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('Controller '):
                actual_mac = line.split()[1].upper()
                
                # Validate MAC format
                if len(actual_mac) == 17 and ':' in actual_mac:
                    # Store the MAC address permanently
                    try:
                        with open(MAC_FILE, 'w') as f:
                            f.write(actual_mac)
                        log.info(f"💾 Stored new MAC address: {actual_mac}")
                        return actual_mac
                    except Exception as e:
                        log.error(f"Failed to store MAC address: {e}")
                        return actual_mac
                    break
        
        # Fallback if parsing failed
        log.warning("Could not parse MAC from bluetoothctl, using fallback")
        fallback_mac = "00:00:00:00:00:00"
        
        # Try to store the fallback
        try:
            with open(MAC_FILE, 'w') as f:
                f.write(fallback_mac)
        except Exception:
            pass
            
        return fallback_mac
        
    except Exception as e:
        log.error(f"get_device_mac(): fallback due to {e}")
        
        # Last resort: try to read from stored file even if invalid
        if os.path.exists(MAC_FILE):
            try:
                with open(MAC_FILE, 'r') as f:
                    stored = f.read().strip()
                    if stored:
                        log.warning(f"Using potentially invalid stored MAC: {stored}")
                        return stored
            except Exception:
                pass
        
        return "00:00:00:00:00:00"

def update_stored_mac(new_mac):
    """Manually update the stored MAC address"""
    MAC_FILE = '/home/orangepi/Waylo_AI/.device_mac_address'
    
    # Validate MAC format
    if not new_mac or len(new_mac) != 17 or ':' not in new_mac:
        log.error(f"Invalid MAC format: {new_mac}. Expected format: XX:XX:XX:XX:XX:XX")
        return False
    
    try:
        with open(MAC_FILE, 'w') as f:
            f.write(new_mac.upper())
        log.info(f"✅ Updated stored MAC address to: {new_mac.upper()}")
        return True
    except Exception as e:
        log.error(f"Failed to update MAC address: {e}")
        return False

def validate_mac_format(mac):
    """Validate MAC address format"""
    if not mac:
        return False
    # Check format: XX:XX:XX:XX:XX:XX
    if len(mac) != 17:
        return False
    if mac.count(':') != 5:
        return False
    # Check each byte is valid hex
    parts = mac.split(':')
    if len(parts) != 6:
        return False
    for part in parts:
        if len(part) != 2 or not all(c in '0123456789ABCDEFabcdef' for c in part):
            return False
    return True

def setup_bluetooth_adapter():
    """Setup Bluetooth adapter for BLE advertising"""
    try:
        subprocess.run(['bluetoothctl', 'power', 'on'], check=True, capture_output=True)
        time.sleep(1)
        subprocess.run(['bluetoothctl', 'discoverable', 'on'], check=True, capture_output=True)
        subprocess.run(['bluetoothctl', 'pairable', 'on'], check=True, capture_output=True)
        subprocess.run(['bluetoothctl', 'system-alias', 'Wailo'], check=True, capture_output=True, text=True)
        log.info("Bluetooth adapter configured for BLE advertising")
        return True
    except subprocess.CalledProcessError as e:
        log.error(f"Failed to configure Bluetooth adapter: {e}")
        return False

def schedule_shutdown():
    """Schedule shutdown after pairing mode duration"""
    global SHUTDOWN_TIMER
    if SHUTDOWN_TIMER:
        SHUTDOWN_TIMER.cancel()
    
    def shutdown_worker():
        if not HANDOFF_COMPLETED:
            log.info(f"Pairing mode timeout ({PAIRING_MODE_DURATION}s) - shutting down...")
            cleanup_and_exit()
    
    SHUTDOWN_TIMER = threading.Timer(PAIRING_MODE_DURATION, shutdown_worker)
    SHUTDOWN_TIMER.daemon = True
    SHUTDOWN_TIMER.start()

def cleanup_and_exit():
    """Clean up resources and exit gracefully"""
    global mainloop, SHUTDOWN_TIMER, adv_obj
    
    try:
        if SHUTDOWN_TIMER:
            SHUTDOWN_TIMER.cancel()
        
        # Unregister BLE advertisement
        if adv_obj:
            try:
                bus = dbus.SystemBus()
                adapter = find_adapter(bus)
                if adapter:
                    ad_manager = dbus.Interface(
                        bus.get_object(BLUEZ_SERVICE_NAME, adapter),
                        LE_ADVERTISING_MANAGER_IFACE
                    )
                    ad_manager.UnregisterAdvertisement(adv_obj.get_path())
                    log.info("✅ BLE advertisement unregistered")
            except Exception as e:
                log.warning(f"UnregisterAdvertisement warning: {e}")
            adv_obj = None
        
        if mainloop and mainloop.is_running():
            mainloop.quit()
            
        log.info("✅ BLE server shutdown complete")
        
    except Exception as e:
        log.error(f"Error during cleanup: {e}")
    
    os._exit(0)

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    log.info(f"Received signal {signum}, shutting down gracefully...")
    cleanup_and_exit()

class Advertisement(dbus.service.Object):
    """BLE Advertisement using BlueZ D-Bus API"""
    PATH_BASE = '/org/bluez/example/advertisement'
    
    def __init__(self, bus, index):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        dbus.service.Object.__init__(self, bus, self.path)
        
    def get_properties(self):
        return {
            LE_ADVERTISEMENT_IFACE: {
                'Type': 'peripheral',
                'LocalName': 'Wailo',
                'ServiceUUIDs': dbus.Array([WAILO_SVC_UUID], signature='s'),
            }
        }
        
    def get_path(self): 
        return dbus.ObjectPath(self.path)
        
    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]
        
    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self): 
        log.info('Advertisement released')

class Application(dbus.service.Object):
    """
    org.bluez.GattApplication1 interface implementation
    Based on working BlueZ example
    """
    def __init__(self, bus):
        self.path = '/'
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)
        self.add_service(WailoService(bus, 0))

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        response = {}
        log.info('GetManagedObjects called')

        for service in self.services:
            response[service.get_path()] = service.get_properties()
            chrcs = service.get_characteristics()
            for chrc in chrcs:
                response[chrc.get_path()] = chrc.get_properties()
                descs = chrc.get_descriptors()
                for desc in descs:
                    response[desc.get_path()] = desc.get_properties()

        return response

class WailoService(dbus.service.Object):
    """
    Wailo Custom Service with MAC Address
    Based on working BlueZ example Service class
    """
    PATH_BASE = '/org/bluez/example/wailo'

    def __init__(self, bus, index):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.uuid = WAILO_SVC_UUID
        self.primary = True
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)

        self.add_characteristic(MacCharacteristic(bus, 0, self))
        self.add_characteristic(TokenCharacteristic(bus, 1, self))

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                'UUID': self.uuid,
                'Primary': self.primary,
                'Characteristics': dbus.Array(
                    self.get_characteristic_paths(),
                    signature='o')
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)

    def get_characteristic_paths(self):
        result = []
        for chrc in self.characteristics:
            result.append(chrc.get_path())
        return result

    def get_characteristics(self):
        return self.characteristics

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_SERVICE_IFACE:
            raise InvalidArgsException()

        return self.get_properties()[GATT_SERVICE_IFACE]

class MacCharacteristic(dbus.service.Object):
    """
    MAC Address Characteristic
    Based on working BlueZ example Characteristic class
    """
    def __init__(self, bus, index, service):
        self.path = service.path + '/char' + str(index)
        self.bus = bus
        self.uuid = MAC_CHAR_UUID
        self.service = service
        self.flags = ['read']
        self.descriptors = []
        dbus.service.Object.__init__(self, bus, self.path)
        
        mac = get_device_mac()
        self.value = [dbus.Byte(int(b, 16)) for b in mac.split(':')]
        log.info(f"MAC Address: {mac} -> {self.value}")
        log.info(f"📱 iOS App can now read MAC: {mac}")
        log.info(f"⏰ Pairing mode active for {PAIRING_MODE_DURATION} seconds")

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
                'Descriptors': dbus.Array(
                    self.get_descriptor_paths(),
                    signature='o')
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_descriptor(self, descriptor):
        self.descriptors.append(descriptor)

    def get_descriptor_paths(self):
        result = []
        for desc in self.descriptors:
            result.append(desc.get_path())
        return result

    def get_descriptors(self):
        return self.descriptors

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise InvalidArgsException()

        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        log.info(f'📖 MAC Characteristic Read by iOS App: {self.value}')
        log.info(f'📱 iOS App has read MAC address: {get_device_mac()}')
        log.info(f'⏳ Waiting for Firebase token via TokenCharacteristic...')
        
        # Don't mark handoff complete yet - wait for token exchange
        return self.value

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        log.info('WriteValue called, returning error')
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        log.info('StartNotify called, returning error')
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        log.info('StopNotify called, returning error')
        raise NotSupportedException()

    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

class TokenCharacteristic(dbus.service.Object):
    """
    Token Exchange Characteristic for Firebase Authentication
    Allows iOS app to send Firebase custom tokens to OrangePi
    """
    def __init__(self, bus, index, service):
        self.path = service.path + '/token' + str(index)
        self.bus = bus
        self.uuid = TOKEN_CHAR_UUID
        self.service = service
        self.flags = ['write-without-response']
        self.descriptors = []
        dbus.service.Object.__init__(self, bus, self.path)
        
        # Token reassembly state
        self.received_chunks = []
        self.total_chunks = None
        self.token_buffer = ""
        
        log.info(f"🔐 Token Characteristic initialized")
        log.info(f"📱 iOS App can now send Firebase custom tokens")

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
                'Descriptors': dbus.Array(
                    self.get_descriptor_paths(),
                    signature='o')
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_descriptor(self, descriptor):
        self.descriptors.append(descriptor)

    def get_descriptor_paths(self):
        result = []
        for desc in self.descriptors:
            result.append(desc.get_path())
        return result

    def get_descriptors(self):
        return self.descriptors

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        global HANDOFF_COMPLETED
        
        try:
            # Convert bytes to string
            chunk_data = bytes(value).decode('utf-8')
            log.info(f"🔐 Received chunk: {len(chunk_data)} bytes")
            
            # Check if this is a small chunk that might be a header or EOF
            if len(chunk_data) < 20:
                log.info(f"🔐 Small chunk detected ({len(chunk_data)} bytes), trying to parse as header")
                log.info(f"🔐 Chunk content: {repr(chunk_data)}")
                
                # Try to parse as raw JSON header first
                try:
                    import json
                    if chunk_data.startswith('{"type":"token"'):
                        header = json.loads(chunk_data)
                        self.total_chunks = header.get('total', 0)
                        self.received_chunks = []
                        self.token_buffer = ""
                        log.info(f"🔐 Raw JSON header received: expecting {self.total_chunks} chunks")
                        return
                    # Accept short header format like {"t":11}
                    elif chunk_data.startswith('{"t"'):
                        header = json.loads(chunk_data)
                        self.total_chunks = header.get('t', 0)
                        self.received_chunks = []
                        self.token_buffer = ""
                        log.info(f"🔐 Short JSON header received: expecting {self.total_chunks} chunks")
                        return
                except Exception as e:
                    log.info(f"🔐 Not a raw JSON header: {e}")
                
                # Try to decode as base64 to see if it's a header
                try:
                    import base64
                    decoded = base64.b64decode(chunk_data + '==')  # Add padding
                    decoded_str = decoded.decode('utf-8')
                    
                    # Check if it's a JSON header
                    if decoded_str.startswith('{"type":"token"'):
                        import json
                        header = json.loads(decoded_str)
                        self.total_chunks = header.get('total', 0)
                        self.received_chunks = []
                        self.token_buffer = ""
                        log.info(f"🔐 Base64 header received: expecting {self.total_chunks} chunks")
                        return
                except Exception as e:
                    log.info(f"🔐 Not a base64 header: {e}")
                
                # Check if it's EOF marker
                if chunk_data == '__EOF__':
                    log.info("🔐 EOF marker received - processing complete token")
                    self.process_complete_token()
                    return
                elif chunk_data == 'X19FT0ZfXw==':  # base64("__EOF__")
                    log.info("🔐 EOF marker received (base64) - processing complete token")
                    self.process_complete_token()
                    return
            
            # Regular data chunk
            self.received_chunks.append(chunk_data)
            log.info(f"🔐 Chunk {len(self.received_chunks)} received (total expected: {self.total_chunks or 'unknown'})")
            
            # Check if we have all chunks
            if self.total_chunks and len(self.received_chunks) >= self.total_chunks:
                log.info("🔐 All chunks received - processing complete token")
                self.process_complete_token()
                
        except Exception as e:
            log.error(f"❌ Token write error: {e}")
            raise FailedException("Token processing failed")
    
    def process_complete_token(self):
        """Process the complete token after all chunks are received"""
        global HANDOFF_COMPLETED
        
        try:
            # Combine all chunks into the complete token
            if self.received_chunks:
                complete_token = ''.join(self.received_chunks)
            else:
                complete_token = self.token_buffer
            
            log.info("🔐 TOKEN RECEIVED SUCCESSFULLY!")
            log.info(f"🔐 Token length: {len(complete_token)} characters")
            log.info(f"🔐 Token preview: {complete_token[:50]}...")
            
            # Save token to file for inspection
            try:
                with open('/home/orangepi/Waylo_AI/received_firebase_token.txt', 'w') as f:
                    f.write(f"Token received at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Token length: {len(complete_token)} characters\n")
                    f.write(f"Token content:\n{complete_token}\n")
                log.info("✅ Token saved to received_firebase_token.txt")
            except Exception as e:
                log.error(f"⚠️ Could not save token to file: {e}")
            
            # Try Firebase authentication (but don't fail if it doesn't work)
            try:
                # Add the virtual environment to Python path
                import sys
                sys.path.insert(0, '/home/orangepi/wailo-env/lib/python3.10/site-packages')
                
                from wailo_api_secure import WailoAPI
                api = WailoAPI()
                
                if api.exchange_custom_token(complete_token):
                    log.info("✅ Device authenticated with Firebase")
                    log.info("✅ Waylo backend authentication successful")
                    
                    # Mark authentication complete
                    try:
                        with open('/home/orangepi/Waylo_AI/.waylo_authenticated', 'w') as f:
                            f.write('authenticated')
                        log.info("✅ Authentication status saved")
                    except Exception as e:
                        log.error(f"⚠️ Could not save authentication status: {e}")
                    
                    HANDOFF_COMPLETED = True
                    log.info("🔄 Firebase authentication complete - shutting down BLE server...")
                    
                    # Mark handoff complete ONLY after successful token exchange
                    try:
                        with open('/home/orangepi/Waylo_AI/.bluetooth_handoff_complete', 'w') as f:
                            f.write('BLE handoff completed with Firebase authentication\n')
                            f.write(f'Device authenticated at: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
                        log.info('✅ Handoff marked as complete with authentication')
                    except Exception as e:
                        log.error(f'⚠️ Could not mark handoff complete: {e}')
                    
                    # Schedule shutdown after a short delay
                    threading.Timer(2.0, cleanup_and_exit).start()
                    
                else:
                    log.warning("⚠️ Firebase token exchange failed - but token was received successfully")
                    log.warning("⚠️ This is expected if Firebase API key is not configured")
                    
                    # Even if Firebase fails, mark handoff complete since we got the token
                    HANDOFF_COMPLETED = True
                    log.info("🔄 Token received successfully - shutting down BLE server...")
                    
                    # Mark handoff complete with token received
                    try:
                        with open('/home/orangepi/Waylo_AI/.bluetooth_handoff_complete', 'w') as f:
                            f.write('BLE handoff completed - token received (Firebase not configured)\n')
                            f.write(f'Token received at: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
                        log.info('✅ Handoff marked as complete with token received')
                    except Exception as e:
                        log.error(f'⚠️ Could not mark handoff complete: {e}')
                    
                    # Schedule shutdown after a short delay
                    threading.Timer(2.0, cleanup_and_exit).start()
                    
            except ImportError as e:
                log.warning(f"⚠️ Could not import secure API: {e}")
                log.warning("⚠️ Firebase authentication not available - but token was received")
                
                # Even if import fails, mark handoff complete since we got the token
                HANDOFF_COMPLETED = True
                log.info("🔄 Token received successfully - shutting down BLE server...")
                
                # Mark handoff complete with token received
                try:
                    with open('/home/orangepi/Waylo_AI/.bluetooth_handoff_complete', 'w') as f:
                        f.write('BLE handoff completed - token received (Firebase not available)\n')
                        f.write(f'Token received at: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
                    log.info('✅ Handoff marked as complete with token received')
                except Exception as e:
                    log.error(f'⚠️ Could not mark handoff complete: {e}')
                
                # Schedule shutdown after a short delay
                threading.Timer(2.0, cleanup_and_exit).start()
                
            except Exception as e:
                log.warning(f"⚠️ Token processing error: {e}")
                log.warning("⚠️ Firebase authentication failed - but token was received")
                
                # Even if processing fails, mark handoff complete since we got the token
                HANDOFF_COMPLETED = True
                log.info("🔄 Token received successfully - shutting down BLE server...")
                
                # Mark handoff complete with token received
                try:
                    with open('/home/orangepi/Waylo_AI/.bluetooth_handoff_complete', 'w') as f:
                        f.write('BLE handoff completed - token received (Firebase error)\n')
                        f.write(f'Token received at: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
                    log.info('✅ Handoff marked as complete with token received')
                except Exception as e:
                    log.error(f'⚠️ Could not mark handoff complete: {e}')
                
                # Schedule shutdown after a short delay
                threading.Timer(2.0, cleanup_and_exit).start()
                
        except Exception as e:
            log.error(f"❌ Token processing error: {e}")
            raise FailedException("Token processing failed")

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        log.info('ReadValue called on Token characteristic, returning error')
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        log.info('StartNotify called on Token characteristic, returning error')
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        log.info('StopNotify called on Token characteristic, returning error')
        raise NotSupportedException()

    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

def start_ble_advertising():
    """Start BLE advertising using proper BlueZ D-Bus API"""
    global adv_obj
    
    try:
        log.info('🚀 Starting BLE advertising using BlueZ D-Bus API...')
        
        # Get D-Bus connection
        bus = dbus.SystemBus()
        adapter = find_adapter(bus)
        if not adapter:
            log.error('❌ No adapter found for advertising')
            cleanup_and_exit()
            return
        
        # Get LE Advertising Manager interface
        ad_manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, adapter),
            LE_ADVERTISING_MANAGER_IFACE
        )
        
        # Create advertisement object
        adv_obj = Advertisement(bus, 0)
        
        # Register advertisement
        def adv_ok():
            log.info('✅ BLE advertising registered via BlueZ D-Bus API')
            log.info('📱 Device is now discoverable by iPhone in PAIRING MODE!')
            log.info('🔐 Device name: Wailo')
            log.info('🔑 Custom service UUID: %s', WAILO_SVC_UUID)
            log.info('⏰ Pairing mode active for %d seconds', PAIRING_MODE_DURATION)
            log.info('📖 iOS App should read MAC address to verify device')
            
            # Schedule shutdown timer
            schedule_shutdown()
        
        def adv_err(e):
            log.error('❌ RegisterAdvertisement error: %s', e)
            cleanup_and_exit()
        
        # Register the advertisement
        ad_manager.RegisterAdvertisement(
            adv_obj.get_path(), {}, 
            reply_handler=adv_ok, 
            error_handler=adv_err
        )
        
    except Exception as e:
        log.error('❌ Failed to start BLE advertising: %s', e)
        cleanup_and_exit()

def register_app_cb():
    """Callback when GATT application is successfully registered"""
    log.info('✅ Wailo GATT application registered successfully!')
    log.info('🔑 Service UUID: %s', WAILO_SVC_UUID)
    log.info('📱 MAC Characteristic UUID: %s', MAC_CHAR_UUID)
    
    # Start BLE advertising using proper BlueZ D-Bus API
    start_ble_advertising()

def register_app_error_cb(error):
    """Callback when GATT application registration fails"""
    log.error('❌ Failed to register application: %s', str(error))
    cleanup_and_exit()

def find_adapter(bus):
    """Find Bluetooth adapter with GATT manager and LE advertising support"""
    remote_om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, '/'), DBUS_OM_IFACE)
    objects = remote_om.GetManagedObjects()

    for o, props in objects.items():
        if GATT_MANAGER_IFACE in props.keys() and LE_ADVERTISING_MANAGER_IFACE in props.keys():
            return o
    return None

def main():
    global mainloop

    try:
        if os.geteuid() != 0:
            log.error("❌ This script must be run as root (sudo) for BLE operations")
            sys.exit(1)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        log.info("🔧 Setting up Bluetooth adapter for PAIRING MODE...")
        if not setup_bluetooth_adapter():
            log.error("❌ Failed to setup Bluetooth adapter")
            sys.exit(1)

        # Set up D-Bus main loop FIRST, before any D-Bus operations
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        mainloop = GLib.MainLoop()

        bus = dbus.SystemBus()
        adapter = find_adapter(bus)
        if not adapter:
            log.error('❌ No adapter with GATT manager AND LE advertising support found')
            log.error('❌ Make sure bluetoothd is running with -E flag (experimental mode)')
            return

        service_manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, adapter),
            GATT_MANAGER_IFACE)

        app = Application(bus)

        log.info('🔧 Registering Wailo GATT application...')
        log.info('🔍 About to call RegisterApplication...')
        
        service_manager.RegisterApplication(app.get_path(), {},
                                        reply_handler=register_app_cb,
                                        error_handler=register_app_error_cb)
        
        log.info('🔍 RegisterApplication call completed, waiting for callback...')

        log.info('🔄 Starting main event loop...')
        mainloop.run()
        
    except KeyboardInterrupt:
        log.info("\n🔄 Shutting down gracefully...")
        cleanup_and_exit()
    except Exception as e:
        log.error(f"❌ Unexpected error: {e}")
        cleanup_and_exit()

if __name__ == '__main__':
    main()

