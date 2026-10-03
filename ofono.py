"""oFono DBus wrapper.

Thin layer over the FuriLabs AT passthrough plugin:
    busctl call org.ofono /ril_0 org.ofono.FuriLabs.AT SendCommand s "AT+ECSQ"
"""

import concurrent.futures
import re

import dbus
from dbus.mainloop.glib import DBusGMainLoop


DBusGMainLoop(set_as_default=True)


OFONO_BUS = "org.ofono"
OFONO_MANAGER_PATH = "/"
OFONO_MANAGER_IFACE = "org.ofono.Manager"
OFONO_MODEM_IFACE = "org.ofono.Modem"
OFONO_NETWORK_REG_IFACE = "org.ofono.NetworkRegistration"
OFONO_SIM_IFACE = "org.ofono.SimManager"
OFONO_AT_IFACE = "org.ofono.FuriLabs.AT"
OFONO_PROP_IFACE = "org.freedesktop.DBus.Properties"
OFONO_CONN_CTX_IFACE = "org.ofono.ConnectionContext"
OFONO_CONN_MGR_IFACE = "org.ofono.ConnectionManager"


class OfonoModem:
    """Wraps DBus calls to an oFono modem object.

    AT-via-D-Bus results that need a network round-trip (CGMI/CGMM/CGSN/CIMI)
    are cached on the instance so repeated refreshes don't spam the modem.
    """

    _AT_FALLBACK_TTL_S = 3600

    def __init__(self, bus: dbus.SystemBus, path: str):
        self.bus = bus
        self.path = path
        self._proxy = bus.get_object(OFONO_BUS, path)
        self._at_iface = dbus.Interface(self._proxy, OFONO_AT_IFACE)
        self._at_cache: dict[str, str] = {}

    def get_all(self, iface: str) -> dict:
        """Get all properties of an interface via its native GetProperties method."""
        try:
            iface_obj = dbus.Interface(self._proxy, iface)
            return dict(iface_obj.GetProperties())
        except dbus.DBusException:
            return {}

    def interfaces(self) -> list[str]:
        try:
            props = self.get_all(OFONO_MODEM_IFACE)
            ifaces = props.get("Interfaces", [])
            return [str(x) for x in ifaces]
        except dbus.DBusException:
            return []

    def has_interface(self, iface: str) -> bool:
        return iface in self.interfaces()

    # --- PDP contexts / APN -------------------------------------------------

    def list_contexts(self) -> list[dict]:
        """Return the modem's GPRS contexts: [{'path','name','type','apn'}]."""
        try:
            mgr = dbus.Interface(
                self._proxy, OFONO_CONN_MGR_IFACE).GetContexts()
        except dbus.DBusException:
            return []
        contexts = []
        for path, props in mgr:
            contexts.append({
                "path": str(path),
                "name": str(props.get("Name", "") or ""),
                "type": str(props.get("Type", "") or ""),
                "apn": str(props.get("AccessPointName", "") or ""),
            })
        contexts.sort(key=lambda c: c["path"])
        return contexts

    def set_context_apn(self, ctx_path: str, apn: str) -> None:
        """Set AccessPointName on a context via its own SetProperty(sv)."""
        obj = self.bus.get_object(OFONO_BUS, ctx_path)
        ctx = dbus.Interface(obj, OFONO_CONN_CTX_IFACE)
        ctx.SetProperty("AccessPointName", dbus.String(apn))

    def provision_context(self, ctx_path: str) -> None:
        """Ask ofono to (re)apply serviceproviders.xml provisioning."""
        obj = self.bus.get_object(OFONO_BUS, ctx_path)
        ctx = dbus.Interface(obj, OFONO_CONN_CTX_IFACE)
        ctx.ProvisionContext()

    def command(self, cmd: str, timeout: int = 10) -> str:
        """Send raw AT command via org.ofono.FuriLabs.AT.SendCommand(s) -> s."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(self._at_iface.SendCommand, cmd)
            try:
                reply = fut.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                raise TimeoutError(f"AT command timed out after {timeout}s")
        if isinstance(reply, (bytes, bytearray)):
            return reply.decode("utf-8", errors="replace")
        if isinstance(reply, dbus.Array):
            return "\n".join(str(x) for x in reply)
        return str(reply)

    def get_modem_info(self, command_timeout: int = 5) -> dict:
        info: dict = {}
        mp = self.get_all(OFONO_MODEM_IFACE)
        info["manufacturer"] = str(mp.get("Manufacturer", "") or "") or None
        info["model"] = str(mp.get("Model", "") or "") or None
        info["revision"] = str(mp.get("Revision", ""))
        info["serial"] = str(mp.get("Serial", ""))
        info["type"] = str(mp.get("Type", ""))
        info["online"] = bool(mp.get("Online", False))
        info["powered"] = bool(mp.get("Powered", False))
        ifaces = set(self.interfaces())
        if OFONO_NETWORK_REG_IFACE in ifaces:
            np = self.get_all(OFONO_NETWORK_REG_IFACE)
            info["operator_name"] = str(np.get("Name", ""))
            info["operator_code"] = str(np.get("Code", ""))
            info["registration"] = str(np.get("Status", "unknown"))
            info["technology"] = str(np.get("Technology", ""))
            info["cell_id"] = np.get("CellId", 0)
            info["mcc"] = str(np.get("MobileCountryCode", ""))
            info["mnc"] = str(np.get("MobileNetworkCode", ""))
            info["strength"] = int(np.get("Strength", 0))
        if OFONO_SIM_IFACE in ifaces:
            sp = self.get_all(OFONO_SIM_IFACE)
            info["imsi"] = str(sp.get("SubscriberIdentity", ""))
            info["iccid"] = str(sp.get("CardIdentifier", ""))
        for label, cmd in (("manufacturer", "AT+CGMI"),
                           ("model", "AT+CGMM"),
                           ("imei", "AT+CGSN"),
                           ("imsi", "AT+CIMI")):
            if info.get(label):
                continue
            cached = self._at_cache.get(cmd)
            if cached is not None:
                info[label] = cached
                continue
            try:
                value = strip_at_response(self.command(cmd, command_timeout))
                self._at_cache[cmd] = value
                info[label] = value
            except Exception:  # noqa: BLE001
                pass
        return info


class OfonoMonitor:
    """Watches oFono Manager for modem add/remove."""

    def __init__(self, bus: dbus.SystemBus, on_modems_changed):
        self.bus = bus
        self.on_modems_changed = on_modems_changed
        bus.add_signal_receiver(
            self._on_added,
            signal_name="ModemAdded",
            dbus_interface=OFONO_MANAGER_IFACE,
            bus_name=OFONO_BUS,
        )
        bus.add_signal_receiver(
            self._on_removed,
            signal_name="ModemRemoved",
            dbus_interface=OFONO_MANAGER_IFACE,
            bus_name=OFONO_BUS,
        )

    def _on_added(self, path, _props):
        self.on_modems_changed()

    def _on_removed(self, path):
        self.on_modems_changed()


def strip_at_response(text: str) -> str:
    """Strip AT response prefix (+CMD: ) and trailing OK/ERROR."""
    if not text:
        return text
    text = text.replace("\r", "")
    text = re.sub(r"^\+[A-Z0-9_]+:\s*", "", text.strip())
    text = re.sub(r"\nOK\s*$", "", text)
    text = re.sub(r"\n(ERROR\s*\d*|CME ERROR:.*)\s*$", "", text)
    return text.strip()