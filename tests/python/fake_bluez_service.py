"""A fake org.bluez for the D-Bus integration test.

Runs on a PRIVATE dbus-daemon started by the test (never the system bus).
Implements just enough of BlueZ for pixelbuds_bridge.BluezGio: the object
manager, Device1 properties and ConnectProfile, and ProfileManager1. On
ConnectProfile it creates a socketpair, serves the Maestro protocol on one end
with FakeBuds and hands the other end to the registered Profile1 through
NewConnection, exactly as bluetoothd hands over an RFCOMM socket.

Test hooks on the device object (interface test.Control):
  Disconnect()            emit Connected=false like a user disconnect
  Connect()               emit Connected=true like a (re)connect
  SetAlias(s)             emit an Alias change, as any local user can cause
  RequestDisconnection()  call the profile's RequestDisconnection
"""
import os
import socket
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi  # noqa: E402
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from support import FakeBuds, m  # noqa: E402

ADAPTER = "/org/bluez/hci0"
DEV = ADAPTER + "/dev_AA_BB_CC_DD_EE_FF"
XML = """
<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
  </interface>
  <interface name="org.bluez.ProfileManager1">
    <method name="RegisterProfile"><arg type="o" direction="in"/><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/></method>
    <method name="UnregisterProfile"><arg type="o" direction="in"/></method>
  </interface>
  <interface name="org.bluez.Device1">
    <method name="ConnectProfile"><arg type="s" direction="in"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
    <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  </interface>
  <interface name="org.bluez.Adapter1">
  </interface>
  <interface name="test.Control">
    <method name="Disconnect"/>
    <method name="Connect"/>
    <method name="SetAlias"><arg type="s" direction="in"/></method>
    <method name="RequestDisconnection"/>
    <method name="Stats"><arg type="a{si}" direction="out"/></method>
  </interface>
</node>
"""

state = {"connected": True, "resolved": True, "profile": None, "connects": 0, "buds": None}


def adapter_props():
    return {
        "Address": GLib.Variant("s", "00:11:22:33:44:55"),
        "Alias": GLib.Variant("s", "fake"),
        "Name": GLib.Variant("s", "fake"),
        "Powered": GLib.Variant("b", True),
        "PowerState": GLib.Variant("s", "on"),
        "Discoverable": GLib.Variant("b", False),
        "DiscoverableTimeout": GLib.Variant("u", 180),
        "Pairable": GLib.Variant("b", True),
        "PairableTimeout": GLib.Variant("u", 0),
        "Discovering": GLib.Variant("b", False),
    }


def props():
    return {
        "Adapter": GLib.Variant("o", ADAPTER),
        "Address": GLib.Variant("s", "AA:BB:CC:DD:EE:FF"),
        "Alias": GLib.Variant("s", "Pixel Buds Pro 2"),
        "Name": GLib.Variant("s", "Pixel Buds Pro 2"),
        "Icon": GLib.Variant("s", "audio-headset"),
        "Paired": GLib.Variant("b", True),
        "Bonded": GLib.Variant("b", True),
        "Trusted": GLib.Variant("b", True),
        "Blocked": GLib.Variant("b", False),
        "WakeAllowed": GLib.Variant("b", False),
        "Class": GLib.Variant("u", 0x244404),
        "Connected": GLib.Variant("b", state["connected"]),
        "ServicesResolved": GLib.Variant("b", state["resolved"]),
        "UUIDs": GLib.Variant("as", ["0000fe2c-0000-1000-8000-00805f9b34fb", m.MAESTRO_UUID]),
    }


def main():
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    ifaces = {i.name: i for i in node.interfaces}

    def handler(conn, sender, path, iface, method, params, inv):
        if method == "GetManagedObjects":
            inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", ({
                ADAPTER: {"org.bluez.Adapter1": adapter_props()},
                DEV: {"org.bluez.Device1": props()},
            },)))
        elif method == "RegisterProfile":
            ppath, uuid, _opts = params.unpack()
            state["profile"] = (sender, ppath, uuid)
            inv.return_value(None)
        elif method == "UnregisterProfile":
            state["profile"] = None
            inv.return_value(None)
        elif method == "GetAll":
            (iface,) = params.unpack()
            table = adapter_props() if path == ADAPTER else props()
            ok = (path == ADAPTER and iface == "org.bluez.Adapter1") or (path == DEV and iface == "org.bluez.Device1")
            inv.return_value(GLib.Variant("(a{sv})", (table if ok else {},)))
        elif method == "Get":
            iface, name = params.unpack()
            table = adapter_props() if path == ADAPTER else props()
            if name not in table:
                inv.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", name)
                return
            inv.return_value(GLib.Variant("(v)", (table[name],)))
        elif method == "ConnectProfile":
            state["connects"] += 1
            if state["profile"] is None or not state["connected"]:
                inv.return_dbus_error("org.bluez.Error.Failed", "no profile")
                return
            ours, theirs = socket.socketpair()
            state["buds"] = FakeBuds(theirs)
            state["buds"].start()
            fds = Gio.UnixFDList.new()
            fds.append(ours.fileno())
            ours.close()
            owner, ppath, _ = state["profile"]

            def done(c, res):
                try:
                    c.call_with_unix_fd_list_finish(res)
                    inv.return_value(None)
                except Exception as error:
                    inv.return_dbus_error("org.bluez.Error.Failed", str(error))

            bus.call_with_unix_fd_list(owner, ppath, "org.bluez.Profile1", "NewConnection",
                                       GLib.Variant("(oha{sv})", (DEV, 0, {})), None,
                                       Gio.DBusCallFlags.NONE, 5000, fds, None, done)
        elif method == "Disconnect":
            state["connected"] = False
            state["resolved"] = False
            bus.emit_signal(None, DEV, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                            GLib.Variant("(sa{sv}as)", ("org.bluez.Device1",
                                                        {"Connected": GLib.Variant("b", False)}, [])))
            inv.return_value(None)
        elif method == "Connect":
            state["connected"] = True
            state["resolved"] = True
            bus.emit_signal(None, DEV, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                            GLib.Variant("(sa{sv}as)", ("org.bluez.Device1",
                                                        {"Connected": GLib.Variant("b", True),
                                                         "ServicesResolved": GLib.Variant("b", True)}, [])))
            inv.return_value(None)
        elif method == "SetAlias":
            (alias,) = params.unpack()
            bus.emit_signal(None, DEV, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                            GLib.Variant("(sa{sv}as)", ("org.bluez.Device1",
                                                        {"Alias": GLib.Variant("s", alias)}, [])))
            inv.return_value(None)
        elif method == "RequestDisconnection":
            owner, ppath, _ = state["profile"]
            bus.call_sync(owner, ppath, "org.bluez.Profile1", "RequestDisconnection",
                          GLib.Variant("(o)", (DEV,)), None, Gio.DBusCallFlags.NONE, 3000, None)
            inv.return_value(None)
        elif method == "Stats":
            buds = state["buds"]
            inv.return_value(GLib.Variant("(a{si})", ({
                "connects": state["connects"],
                "registered": 1 if state["profile"] else 0,
                "buds_stopped": 1 if buds is not None and buds.stopped.is_set() else 0,
            },)))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    register = getattr(bus, "register_object_with_closures2", None) or bus.register_object
    register("/", ifaces["org.freedesktop.DBus.ObjectManager"], handler, None, None)
    register("/org/bluez", ifaces["org.bluez.ProfileManager1"], handler, None, None)
    for name in ("org.bluez.Adapter1", "org.freedesktop.DBus.Properties"):
        register(ADAPTER, ifaces[name], handler, None, None)
    for name in ("org.bluez.Device1", "org.freedesktop.DBus.Properties", "test.Control"):
        register(DEV, ifaces[name], handler, None, None)
    loop = GLib.MainLoop()

    def acquired(*_):
        print("ready", flush=True)

    Gio.bus_own_name_on_connection(bus, "org.bluez", Gio.BusNameOwnerFlags.NONE, acquired, lambda *_: loop.quit())
    loop.run()


if __name__ == "__main__":
    main()
