#!/usr/bin/env python3
import dbus, dbus.exceptions, dbus.mainloop.glib, dbus.service
from gi.repository import GLib
import sys, subprocess, os, threading, signal, logging

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler('/tmp/wailo_gatt_server.log')]
)
log = logging.getLogger("wailo-gatt")

# ---------- BlueZ constants ----------
BLUEZ_SERVICE_NAME = 'org.bluez'
DBUS_OM_IFACE      = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE    = 'org.freedesktop.DBus.Properties'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
LE_ADV_MGR_IFACE   = 'org.bluez.LEAdvertisingManager1'

GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHRC_IFACE    = 'org.bluez.GattCharacteristic1'
LE_ADVERTISEMENT_IFACE = 'org.bluez.LEAdvertisement1'

# ---------- UUIDs ----------
WAILO_SVC_UUID = '12345678-1234-1234-1234-123456789abc'
MAC_CHAR_UUID  = '11111111-2222-3333-4444-555555555555'

# ---------- Globals ----------
mainloop = None
ADAPTER_PATH = None
adv_obj = None

# ---------- Exceptions ----------
class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.freedesktop.DBus.Error.InvalidArgs'

# ---------- Helpers ----------
def get_device_mac():
    try:
        out = subprocess.run(['bluetoothctl', 'show'], capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('Controller '):
                return line.split()[1].upper()  # AA:BB:CC:DD:EE:FF
    except Exception as e:
        log.warning(f"get_device_mac(): {e}")
    return "00:00:00:00:00:00"

def find_adapter(bus):
    om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, '/'), DBUS_OM_IFACE)
    for path, props in om.GetManagedObjects().items():
        if GATT_MANAGER_IFACE in props and LE_ADV_MGR_IFACE in props:
            return path
    return None

def cleanup_and_exit():
    global adv_obj, mainloop, ADAPTER_PATH
    try:
        if adv_obj and ADAPTER_PATH:
            bus = dbus.SystemBus()
            mgr = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH), LE_ADV_MGR_IFACE)
            try:
                mgr.UnregisterAdvertisement(adv_obj.get_path())
                log.info("✅ Advertising unregistered")
            except Exception as e:
                log.warning(f"UnregisterAdvertisement warning: {e}")
            adv_obj = None
    finally:
        if mainloop and mainloop.is_running():
            mainloop.quit()
        log.info("✅ GATT server shutdown complete")
        os._exit(0)

def signal_handler(signum, frame):
    log.info(f"Received signal {signum}, shutting down…")
    cleanup_and_exit()

# ---------- GATT characteristic ----------
class MacCharacteristic(dbus.service.Object):
    IFACE = GATT_CHRC_IFACE
    UUID  = MAC_CHAR_UUID

    def __init__(self, bus, service, index=0):
        self.path = service.get_path() + f'/char{index}'
        super().__init__(bus, dbus.ObjectPath(self.path))
        self.service = service
        self.flags = ['read']  # change to ['encrypt-read'] if you want to force pairing sheet

    def get_path(self): return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {
            self.IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.UUID,
                'Flags': dbus.Array(self.flags, signature='s'),
                'Descriptors': dbus.Array([], signature='o'),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != self.IFACE:
            raise InvalidArgsException()
        return self.get_properties()[self.IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        # BlueZ may include 'offset' in options
        try:
            offset = int(options.get('offset', 0))
        except Exception:
            offset = 0
        mac = get_device_mac()
        data = [dbus.Byte(int(b,16)) for b in mac.split(':')]  # 6 bytes
        log.info(f"📖 ReadValue(offset={offset}) -> {mac}")
        return data[offset:]  # BlueZ accepts list[dbus.Byte] for 'ay'

# ---------- GATT service ----------
class WailoService(dbus.service.Object):
    PATH_BASE = '/org/bluez/wailo/service'

    def __init__(self, bus, index):
        self.path = self.PATH_BASE + str(index)
        super().__init__(bus, dbus.ObjectPath(self.path))
        self.uuid = WAILO_SVC_UUID
        self.primary = True
        self.chars = [MacCharacteristic(bus, self, 0)]

    def get_path(self): return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                'UUID': self.uuid,
                'Primary': dbus.Boolean(self.primary),
                'Characteristics': dbus.Array([c.get_path() for c in self.chars], signature='o')
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_SERVICE_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_SERVICE_IFACE]

# ---------- Application (ObjectManager root) ----------
class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = '/'
        super().__init__(bus, dbus.ObjectPath(self.path))
        self.services = [WailoService(bus, 0)]

    def get_path(self): return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        resp = {}
        for svc in self.services:
            resp[svc.get_path()] = svc.get_properties()
            for ch in svc.chars:
                resp[ch.get_path()] = ch.get_properties()
        return resp

# ---------- LE Advertisement ----------
class Advertisement(dbus.service.Object):
    PATH_BASE = '/org/bluez/wailo/advertisement'

    def __init__(self, bus, index):
        self.path = self.PATH_BASE + str(index)
        super().__init__(bus, dbus.ObjectPath(self.path))

    def get_path(self): return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {
            LE_ADVERTISEMENT_IFACE: {
                'Type': 'peripheral',
                'LocalName': 'Wailo',
                'ServiceUUIDs': dbus.Array([WAILO_SVC_UUID], signature='s'),
                'Includes': dbus.Array(['tx-power'], signature='s'),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        pass

# ---------- Register / Advertise ----------
def _register_advertising():
    """Called via GLib.idle_add AFTER mainloop is running."""
    global adv_obj, ADAPTER_PATH
    try:
        bus = dbus.SystemBus()
        mgr = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH), LE_ADV_MGR_IFACE)
        adv_obj = Advertisement(bus, 0)
        mgr.RegisterAdvertisement(adv_obj.get_path(), {})
        log.info("📣 LE advertising registered (D-Bus)")
    except Exception as e:
        log.error(f"❌ RegisterAdvertisement failed: {e}")
    return False  # run once

def register_app_cb():
    log.info('✅ GATT application registered')
    # Defer advertising registration until the GLib loop is pumping
    GLib.idle_add(_register_advertising)

def register_app_error_cb(error):
    log.error(f"❌ Failed to register application: {error}")
    cleanup_and_exit()

# ---------- Main ----------
def main():
    global mainloop, ADAPTER_PATH

    if os.geteuid() != 0:
        log.error("Run as root (sudo).")
        sys.exit(1)

    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    ADAPTER_PATH = find_adapter(bus)
    if not ADAPTER_PATH:
        log.error("No LEAdvertisingManager1 / GattManager1 found (enable bluetoothd -E).")
        sys.exit(1)
    log.info(f"Adapter: {ADAPTER_PATH}")

    # Prime adapter (nice-to-have)
    try:
        props = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH), DBUS_PROP_IFACE)
        props.Set('org.bluez.Adapter1', 'Powered', dbus.Boolean(1))
        props.Set('org.bluez.Adapter1', 'Discoverable', dbus.Boolean(1))
        props.Set('org.bluez.Adapter1', 'Pairable', dbus.Boolean(1))
        props.Set('org.bluez.Adapter1', 'Alias', 'Wailo')
    except Exception as e:
        log.warning(f"Adapter prime warning: {e}")

    app = Application(bus)
    mgr = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH), GATT_MANAGER_IFACE)

    mainloop = GLib.MainLoop()
    log.info("🔄 Main loop starting")
    mgr.RegisterApplication(app.get_path(), {}, reply_handler=register_app_cb, error_handler=register_app_error_cb)
    mainloop.run()

if __name__ == '__main__':
    main()
