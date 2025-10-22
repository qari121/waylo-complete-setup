#!/usr/bin/env python3
"""
Wailo Custom GATT Server with MAC Address Characteristic
Based on working BlueZ example-gatt-server implementation
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
        logging.FileHandler('/tmp/wailo_gatt_server.log')
    ]
)
log = logging.getLogger(__name__)

mainloop = None
SHUTDOWN_TIMER = None
HANDOFF_COMPLETED = False

BLUEZ_SERVICE_NAME = 'org.bluez'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'

GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHRC_IFACE = 'org.bluez.GattCharacteristic1'
GATT_DESC_IFACE = 'org.bluez.GattDescriptor1'

# Wailo Service UUIDs
WAILO_SVC_UUID = '12345678-1234-1234-1234-123456789abc'
MAC_CHAR_UUID = '11111111-2222-3333-4444-555555555555'

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
    """Get device MAC address"""
    try:
        out = subprocess.run(
            ['bluetoothctl', 'show'],
            capture_output=True, text=True, check=True
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('Controller '):
                return line.split()[1].upper()
    except Exception as e:
        log.warning(f"get_device_mac(): fallback due to {e}")
    return "00:00:00:00:00:00"

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
    global mainloop, SHUTDOWN_TIMER
    
    try:
        if SHUTDOWN_TIMER:
            SHUTDOWN_TIMER.cancel()
        
        if mainloop and mainloop.is_running():
            mainloop.quit()
            
        # Stop BLE advertising
        try:
            subprocess.run(['bluetoothctl', 'advertise', 'off'], check=True, capture_output=True)
        except:
            pass
            
        log.info("✅ BLE server shutdown complete")
        
    except Exception as e:
        log.error(f"Error during cleanup: {e}")
    
    os._exit(0)

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    log.info(f"Received signal {signum}, shutting down gracefully...")
    cleanup_and_exit()

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
        global HANDOFF_COMPLETED
        
        log.info(f'📖 MAC Characteristic Read by iOS App: {self.value}')
        log.info(f'✅ Data exchange completed! iOS has verified MAC address.')
        
        HANDOFF_COMPLETED = True
        log.info(f'🔄 Shutting down BLE server and starting AI_Chat service...')
        
        # Mark handoff complete
        try:
            with open('/home/orangepi/Waylo_AI/.bluetooth_handoff_complete', 'w') as f:
                f.write('BLE handoff completed\n')
            log.info('✅ Handoff marked as complete')
        except Exception as e:
            log.error(f'⚠️ Could not mark handoff complete: {e}')
        
        # Schedule shutdown after a short delay
        threading.Timer(2.0, cleanup_and_exit).start()
        
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

def register_app_cb():
    """Callback when GATT application is successfully registered"""
    log.info('✅ Wailo GATT application registered successfully!')
    log.info('🔑 Service UUID: %s', WAILO_SVC_UUID)
    log.info('📱 MAC Characteristic UUID: %s', MAC_CHAR_UUID)
    
    log.info('🚀 Starting BLE advertising...')
    # Start BLE advertising using the working printf method
    try:
        # Use printf to send commands to bluetoothctl (this method works!)
        cmd = "printf 'advertise on\\nquit\\n' | bluetoothctl"
        result = subprocess.run(
            cmd, 
            shell=True,
            check=True, 
            capture_output=True, 
            text=True,
            timeout=15
        )
        
        log.info('✅ BLE advertising command executed successfully')
        log.info('📤 Command output: %s', result.stdout.strip())
        
        # Wait a moment for advertising to start
        time.sleep(3)
        
        # Verify advertising is active
        verify_result = subprocess.run(
            ['bluetoothctl', 'show'], 
            capture_output=True, 
            text=True, 
            check=True
        )
        
        if 'ActiveInstances: 0x0' in verify_result.stdout and 'ActiveInstances: 0x00' not in verify_result.stdout:
            log.info('✅ BLE advertising is ACTIVE and working!')
            log.info('📱 Device is now discoverable by iPhone in PAIRING MODE!')
            log.info('🔐 Device name: Wailo')
            log.info('🔑 Custom service UUID: %s', WAILO_SVC_UUID)
            log.info('⏰ Pairing mode active for %d seconds', PAIRING_MODE_DURATION)
            log.info('📖 iOS App should read MAC address to verify device')
            
            # Schedule shutdown timer
            schedule_shutdown()
        else:
            log.error('❌ BLE advertising verification failed')
            log.error('📋 bluetoothctl show output: %s', verify_result.stdout)
            cleanup_and_exit()
        
    except subprocess.CalledProcessError as e:
        log.error('❌ Failed to start BLE advertising: %s', e)
        log.error('📋 Error output: %s', e.stderr)
        cleanup_and_exit()
    except subprocess.TimeoutExpired:
        log.error('❌ BLE advertising command timed out')
        cleanup_and_exit()

def register_app_error_cb(error):
    """Callback when GATT application registration fails"""
    log.error('❌ Failed to register application: %s', str(error))
    cleanup_and_exit()

def find_adapter(bus):
    """Find Bluetooth adapter with GATT manager support"""
    remote_om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, '/'), DBUS_OM_IFACE)
    objects = remote_om.GetManagedObjects()

    for o, props in objects.items():
        if GATT_MANAGER_IFACE in props.keys():
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
            log.error('❌ GattManager1 interface not found')
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
