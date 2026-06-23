#!/usr/bin/env python3
"""
5G Modem AT Tool (oFono backend)
================================
PyGTK (PyGObject/GTK3) application that talks to a 5G modem through oFono's
DBus interface, using the FuriLabs AT passthrough plugin:

    busctl call org.ofono /ril_0 org.ofono.FuriLabs.AT SendCommand s "AT+ECSQ"

Designed to run on a Linux phone (PinePhone, Mobian, FuriOS, etc.) where oFono
is the canonical modem stack.

Requirements:
    sudo apt install python3-gi python3-dbus gir1.2-gtk-3.0 ofono
    sudo systemctl enable --now ofono

Run:
    python3 5g-at-tool.py
"""

import concurrent.futures
import re
import sys
import threading
from collections import deque

import dbus
from dbus.mainloop.glib import DBusGMainLoop

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

DBusGMainLoop(set_as_default=True)

OFONO_BUS = "org.ofono"
OFONO_MANAGER_PATH = "/"
OFONO_MANAGER_IFACE = "org.ofono.Manager"
OFONO_MODEM_IFACE = "org.ofono.Modem"
OFONO_NETWORK_REG_IFACE = "org.ofono.NetworkRegistration"
OFONO_SIM_IFACE = "org.ofono.SimManager"
OFONO_RADIO_IFACE = "org.ofono.RadioSettings"
OFONO_AT_IFACE = "org.ofono.FuriLabs.AT"
OFONO_PROP_IFACE = "org.freedesktop.DBus.Properties"

ACCESS_TECH = {
    0: "GSM", 1: "GSM Compact", 2: "UTRAN", 3: "GSM w/EGPRS",
    4: "UTRAN w/HSDPA", 5: "UTRAN w/HSUPA", 6: "UTRAN w/HSDPA+HSUPA",
    7: "E-UTRAN (LTE)", 8: "EC-GSM-IoT", 9: "LTE cat-M1",
    10: "LTE cat-NB1", 11: "NR (5G)", 12: "NG-RAN (5G)", 13: "NR (5G)",
}

CEREG_STATUS = {
    0: "not registered", 1: "registered", 2: "searching",
    3: "registration denied", 4: "unknown", 5: "registered, roaming",
}


def parse_cesq(text: str) -> dict:
    """Parse +CESQ: <rxlev>,<ber>,<rscp>,<ecno>,<rsrq>,<rsrp>[,...].

    Some modems (e.g. MediaTek) append vendor-specific trailing values.
    The first six values follow 3GPP TS 27.007.
    """
    out: dict = {}
    m = re.search(r"\+CESQ:\s*([\d,]+)", text or "")
    if not m:
        return out
    vals = m.group(1).split(",")
    std_keys = ("rxlev", "ber", "rscp", "ecno", "rsrq", "rsrp",
                "rssi", "rsrp2", "sinr")
    for i, v in enumerate(vals):
        try:
            vi = int(v)
        except ValueError:
            continue
        if i < len(std_keys):
            out[std_keys[i]] = vi
        else:
            out[f"v{i+1}"] = vi
    if "rsrq" in out and out["rsrq"] != 255:
        out["rsrq_db"] = out["rsrq"] / 2.0 - 19.5
    if "rsrp" in out and out["rsrp"] != 255:
        out["rsrp_dbm"] = out["rsrp"] - 141
    if "rssi" in out and out["rssi"] != 255:
        out["rssi_dbm"] = out["rssi"] - 110
    if "sinr" in out and out["sinr"] != 255:
        out["sinr_db"] = out["sinr"] / 2.0 - 20.0
    if "rscp" in out and out["rscp"] != 255:
        out["rscp_dbm"] = out["rscp"] - 121
    if "rxlev" in out and out["rxlev"] != 99:
        out["rxlev_dbm"] = -110 + out["rxlev"]
    return out


def parse_cereg(text: str) -> dict:
    """Parse +CEREG/+CREG: <n>,<stat>[,<tac>,<ci>,<AcT>[,<cause_type>,<reject_cause>]]."""
    out: dict = {}
    m = re.search(r"\+C[5GE]?REG:\s*([\d,\"a-fA-F]+)", text or "")
    if not m:
        return out
    parts = [p.strip().strip('"') for p in m.group(1).split(",") if p.strip()]
    if len(parts) >= 1:
        try:
            out["mode"] = int(parts[0])
        except ValueError:
            pass
    if len(parts) >= 2:
        try:
            out["status"] = int(parts[1])
        except ValueError:
            pass
    if len(parts) >= 3:
        out["tac"] = parts[2]
    if len(parts) >= 4:
        out["cell_id"] = parts[3]
    if len(parts) >= 5:
        try:
            out["act"] = int(parts[4])
        except ValueError:
            pass
    return out


def parse_cops(text: str) -> dict:
    """Parse +COPS: <mode>[,<format>,<op>,<AcT>]."""
    out: dict = {}
    m = re.search(r"\+COPS:\s*([\d,\"]+)", text or "")
    if not m:
        return out
    parts = [p.strip().strip('"') for p in m.group(1).split(",") if p.strip()]
    if not parts:
        return out
    try:
        out["mode"] = int(parts[0])
    except ValueError:
        pass
    if len(parts) >= 3:
        out["operator"] = parts[2]
    if len(parts) >= 4:
        try:
            out["act"] = int(parts[3])
        except ValueError:
            pass
    return out


def parse_ecellmeas(text: str) -> dict:
    """Parse +ECELLMEAS: <rat>,<arfcn>,<pci>,<rsrp>,<rsrq>,<snr>,<cid>,<num_of_plmn>,<plmn_id>,<plmn_name>[,...]."""
    out: dict = {}
    m = re.search(r"\+ECELLMEAS:\s*(.*?)(?:\nOK|$)", text or "", re.DOTALL)
    if not m:
        return out
    parts = [p.strip().strip('"') for p in m.group(1).split(",") if p.strip()]
    if not parts:
        return out
    keys = ("rat", "arfcn", "pci", "rsrp", "rsrq", "snr", "cid", "num_plmn")
    for i, key in enumerate(keys):
        if i < len(parts):
            try:
                out[key] = int(parts[i]) if parts[i].lstrip("-").isdigit() else parts[i]
            except ValueError:
                out[key] = parts[i]
    if len(parts) > 8:
        out["plmn"] = []
        for i in range(8, len(parts), 2):
            if i + 1 < len(parts):
                out["plmn"].append((parts[i], parts[i + 1]))
    return out


def parse_ecell(text: str) -> list[dict]:
    """Parse +ECELL: <num_of_cell>,<Act>,<cell1>,<cell2>,...
    where each cell has 16 fields: <cid>,<lac_or_tac>,<mcc>,<mnc>,<psc_or_pci>,<sig1>,<sig2>,<sig1_in_dbm>,<sig2_in_dbm>,<ta>,<ext1>,<ext2>,<ext3>,<ext4>,<ext5>,<ext6>."""
    cells = []
    m = re.search(r"\+ECELL:\s*([^\n\r]+)", text or "")
    if not m:
        return cells
    raw = m.group(1).rstrip(",").strip()
    parts: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == '"':
            j = raw.find('"', i + 1)
            if j == -1:
                break
            parts.append(raw[i + 1:j])
            i = j + 2
        else:
            j = raw.find(',', i)
            if j == -1:
                parts.append(raw[i:].strip())
                break
            parts.append(raw[i:j].strip())
            i = j + 1
    if not parts:
        return cells
    try:
        num = int(parts[0])
    except ValueError:
        return cells
    act = None
    if len(parts) > 1:
        try:
            act = int(parts[1])
        except ValueError:
            act = parts[1]
    keys = ("cid", "lac_or_tac", "mcc", "mnc", "psc_or_pci",
            "sig1", "sig2", "sig1_in_dbm", "sig2_in_dbm",
            "ta", "ext1", "ext2", "ext3", "ext4", "ext5", "ext6")
    idx = 2
    for _ in range(num):
        cell = {"act": act}
        for key in keys:
            if idx < len(parts):
                val = parts[idx].strip()
                if val == "" or val.lower() == "null":
                    cell[key] = None
                elif val.lstrip("-").isdigit():
                    try:
                        cell[key] = int(val)
                    except ValueError:
                        cell[key] = val
                else:
                    cell[key] = val
                idx += 1
        cells.append(cell)
    return cells


def parse_eimsgeo(text: str) -> dict:
    """Parse +EIMSGEO: <account_id>,<broadcast_flag>,<latitude>,<longitude>,<accurate>,<method>,<city>,<state>,<zip>,<country>,<ue_wifi_mac>,<Confidence>,<altitude>,<accuracy_semiMajorAxis>,<accuracy_semiMinorAxis>,<accuracy_verticalAxis>."""
    out: dict = {}
    m = re.search(r"\+EIMSGEO:\s*(.*?)(?:\nOK|$)", text or "", re.DOTALL)
    if not m:
        return out
    parts = [p.strip() for p in m.group(1).split(",")]
    keys = ("account_id", "broadcast_flag", "latitude", "longitude", "accurate",
            "method", "city", "state", "zip", "country", "ue_wifi_mac",
            "confidence", "altitude", "accuracy_semiMajorAxis",
            "accuracy_semiMinorAxis", "accuracy_verticalAxis")
    for i, key in enumerate(keys):
        if i < len(parts):
            out[key] = parts[i]
    return out


def strip_at_response(text: str) -> str:
    """Strip AT response prefix (+CMD: ) and trailing OK/ERROR."""
    if not text:
        return text
    text = text.replace("\r", "")
    text = re.sub(r"^\+[A-Z0-9_]+:\s*", "", text.strip())
    text = re.sub(r"\nOK\s*$", "", text)
    text = re.sub(r"\n(ERROR\s*\d*|CME ERROR:.*)\s*$", "", text)
    return text.strip()


class OfonoModem:
    """Wraps DBus calls to an oFono modem object."""

    def __init__(self, bus: dbus.SystemBus, path: str):
        self.bus = bus
        self.path = path
        self._proxy = bus.get_object(OFONO_BUS, path)
        self._at_iface = dbus.Interface(self._proxy, OFONO_AT_IFACE)

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
            if not info.get(label):
                try:
                    info[label] = strip_at_response(self.command(cmd, command_timeout))
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


class MainWindow(Gtk.ApplicationWindow):
    """Main application window."""

    LOG_MAX = 5000

    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="5G Modem AT Tool (oFono)")
        self.set_default_size(1100, 760)
        self.set_border_width(8)

        self.bus = dbus.SystemBus()
        self.modem: OfonoModem | None = None
        self._modem_paths: list[str] = []
        self.monitor = OfonoMonitor(
            self.bus,
            on_modems_changed=self._refresh_modem_list,
        )

        self.log_buffer = Gtk.TextBuffer()
        self.log_buffer.create_tag("tx", foreground="#0a84ff", weight=700)
        self.log_buffer.create_tag("rx", foreground="#34c759")
        self.log_buffer.create_tag("info", foreground="#888888", style=2)
        self.log_buffer.create_tag("err", foreground="#ff3b30", weight=700)
        self.log_buffer.create_tag("ok", foreground="#34c759", weight=700)

        self._build_ui()
        GLib.timeout_add(2000, self._poll_signal)
        self._refresh_modem_list()

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add(root)

        self.notebook = Gtk.Notebook()
        self.notebook.set_show_border(True)
        root.pack_start(self.notebook, False, False, 0)

        status_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        status_tab.set_border_width(8)
        self.notebook.append_page(status_tab, Gtk.Label(label="Status"))
        self._build_status_tab(status_tab)

        positioning_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        positioning_tab.set_border_width(8)
        self.notebook.append_page(positioning_tab, Gtk.Label(label="Positioning"))
        self._build_positioning_tab(positioning_tab)

        network_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        network_tab.set_border_width(8)
        self.notebook.append_page(network_tab, Gtk.Label(label="Network"))
        self._build_network_tab(network_tab)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.pack_start(right, True, True, 0)

        term_frame = Gtk.Frame(label="AT command terminal")
        term_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        term_frame.add(term_box)
        right.pack_start(term_frame, True, True, 0)

        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      vscrollbar_policy=Gtk.PolicyType.ALWAYS)
        self.log_view = Gtk.TextView(buffer=self.log_buffer, editable=False,
                                     monospace=True, cursor_visible=False,
                                     top_margin=4, bottom_margin=4,
                                     left_margin=6, right_margin=6)
        scrolled.add(self.log_view)
        term_box.pack_start(scrolled, True, True, 0)

        term_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        term_box.pack_start(term_bar, False, False, 0)
        term_bar.pack_start(Gtk.Label(label="AT>"), False, False, 0)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Enter AT command and press Enter")
        self.entry.connect("activate", self._on_send)
        term_bar.pack_start(self.entry, True, True, 0)
        self.timeout_spin = Gtk.SpinButton.new(
            Gtk.Adjustment.new(10, 1, 600, 1, 10, 0), 1, 0)
        self.timeout_spin.set_tooltip_text("Timeout (seconds)")
        term_bar.pack_start(self.timeout_spin, False, False, 0)
        send_btn = Gtk.Button(label="Send")
        send_btn.connect("clicked", self._on_send)
        term_box.pack_start(send_btn, False, False, 0)
        clear_btn = Gtk.Button(label="Clear log")
        clear_btn.connect("clicked", lambda *_: self.log_buffer.set_text(""))
        term_bar.pack_start(clear_btn, False, False, 0)

        hist_frame = Gtk.Frame(label="History")
        hist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        hist_frame.add(hist_box)
        right.pack_start(hist_frame, False, False, 0)
        hist_model = Gtk.ListStore(str, str)
        self.history_store = hist_model
        self.history_view = Gtk.TreeView(model=hist_model)
        for i, title in enumerate(("Time", "Command")):
            self.history_view.append_column(
                Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        self.history_view.set_size_request(-1, 120)
        sel = self.history_view.get_selection()
        sel.connect("changed", self._on_history_select)
        hist_box.pack_start(self.history_view, False, False, 0)
        hist_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        hist_box.pack_start(hist_buttons, False, False, 0)
        rerun = Gtk.Button(label="Re-run selected")
        rerun.connect("clicked", self._on_rerun_selected)
        hist_buttons.pack_start(rerun, False, False, 0)
        clear_hist = Gtk.Button(label="Clear history")
        clear_hist.connect("clicked", lambda *_: self.history_store.clear())
        hist_buttons.pack_start(clear_hist, False, False, 0)

        self._command_history: deque[str] = deque(maxlen=200)
        self._history_pos = 0
        self.entry.connect("key-press-event", self._on_entry_key)

    def _build_status_tab(self, parent: Gtk.Box):
        mod_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mod_box.pack_start(Gtk.Label(label="Modem:"), False, False, 0)
        self.modem_combo = Gtk.ComboBoxText()
        self.modem_combo.connect("changed", self._on_modem_changed)
        mod_box.pack_start(self.modem_combo, True, True, 0)
        refresh_btn = Gtk.Button(label="⟳")
        refresh_btn.connect("clicked", lambda *_: self._refresh_modem_list())
        refresh_btn.set_tooltip_text("Refresh modem list")
        mod_box.pack_start(refresh_btn, False, False, 0)
        parent.pack_start(mod_box, False, False, 0)

        info_frame = Gtk.Frame(label="Modem")
        info_grid = Gtk.Grid(column_spacing=8, row_spacing=4, border_width=8)
        info_frame.add(info_grid)
        parent.pack_start(info_frame, False, False, 0)
        self.info_labels: dict[str, Gtk.Label] = {}
        for row, key in enumerate((
            "Manufacturer", "Model", "Revision", "IMEI", "IMSI",
            "Online", "Powered", "Type", "Operator", "Registration",
            "Tech", "Strength",
        )):
            info_grid.attach(Gtk.Label(label=key + ":", xalign=0), 0, row, 1, 1)
            lbl = Gtk.Label(label="-", xalign=0, selectable=True)
            self.info_labels[key] = lbl
            info_grid.attach(lbl, 1, row, 1, 1)

        sig_frame = Gtk.Frame(label="5G Signal (AT+CESQ + AT+CEREG)")
        sig_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        sig_frame.add(sig_box)
        parent.pack_start(sig_frame, False, False, 0)
        self.signal_labels: dict[str, Gtk.Label] = {}
        sig_grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        sig_box.pack_start(sig_grid, False, False, 0)
        for row, key in enumerate((
            "RAT", "MCC/MNC", "TAC", "Cell ID",
            "RSRP (dBm)", "RSRQ (dB)", "RSSI (dBm)", "SINR (dB)",
            "RXLEV", "RSCP (dBm)", "ECNO (dB)",
        )):
            sig_grid.attach(Gtk.Label(label=key + ":", xalign=0), 0, row, 1, 1)
            lbl = Gtk.Label(label="-", xalign=0, selectable=True)
            self.signal_labels[key] = lbl
            sig_grid.attach(lbl, 1, row, 1, 1)
        refresh_sig = Gtk.Button(label="Refresh signal")
        refresh_sig.connect("clicked", lambda *_: self._refresh_signal_info())
        sig_box.pack_start(refresh_sig, False, False, 0)

        preset_frame = Gtk.Frame(label="AT Presets")
        preset_box = Gtk.FlowBox(homogeneous=False, max_children_per_line=2,
                                 border_width=8, column_spacing=4, row_spacing=4)
        preset_frame.add(preset_box)
        parent.pack_start(preset_box, False, False, 0)
        for label, cmd in (
            ("Info", "ATI"),
            ("Manufacturer", "AT+CGMI"),
            ("Model", "AT+CGMM"),
            ("Firmware", "AT+CGMR"),
            ("IMEI", "AT+CGSN"),
            ("IMSI", "AT+CIMI"),
            ("Operator", "AT+COPS?"),
            ("Signal (CESQ)", "AT+CESQ"),
            ("Signal (ECSQ)", "AT+ECSQ"),
            ("3G/4G Reg", "AT+CREG?"),
            ("EPS/5G Reg", "AT+CEREG?"),
            ("5G Reg", "AT+C5GREG?"),
            ("Capabilities", "AT+GCAP"),
            ("QENG (Quectel)", 'AT+QENG="servingcell"'),
            ("QCAInfo (Quectel)", "AT+QCAINFO"),
            ("Cell ID", "AT+ECID"),
            ("Packet Service", "AT+CGATT?"),
            ("SIM Status", "AT+CPIN?"),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self._make_preset_handler(cmd))
            preset_box.add(btn)

    def _build_positioning_tab(self, parent: Gtk.Box):
        info_frame = Gtk.Frame(label="Network positioning (MediaTek proprietary)")
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        info_frame.add(info_box)
        parent.pack_start(info_frame, False, False, 0)
        info_box.pack_start(Gtk.Label(
            label="AT+ECELLMEAS – LTE/NR cell measurement\n"
                  "AT+ECELL – Get serving + neighboring cell info\n"
                  "AT+ECELLID – Report cell ID URC\n"
                  "AT+ELOCAEN – Location UI on/off\n"
                  "AT+EIMSGEO – Geolocation information\n"
                  "AT+EREGINFO – Network registration status",
            xalign=0), False, False, 0)

        cell_frame = Gtk.Frame(label="Cell measurement (AT+ECELLMEAS)")
        cell_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        cell_frame.add(cell_box)
        parent.pack_start(cell_box, False, False, 0)
        cell_grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        cell_box.pack_start(cell_grid, False, False, 0)
        self.cell_labels: dict[str, Gtk.Label] = {}
        for row, key in enumerate((
            "Status", "RAT", "ARFCN", "PCI",
            "RSRP (dBm)", "RSRQ (dB)", "SNR (dB)", "Cell ID",
            "PLMNs", "Band",
        )):
            cell_grid.attach(Gtk.Label(label=key + ":", xalign=0), 0, row, 1, 1)
            lbl = Gtk.Label(label="-", xalign=0, selectable=True)
            self.cell_labels[key] = lbl
            cell_grid.attach(lbl, 1, row, 1, 1)

        cell_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cell_box.pack_start(cell_buttons, False, False, 0)
        self.cell_meas_buttons = {}
        for label, cmd in (
            ("Start measurement", "AT+ECELLMEAS=1"),
            ("Stop measurement", "AT+ECELLMEAS=0"),
            ("Get cell info", "AT+ECELL"),
            ("Cell ID URC on", "AT+ECELLID=1"),
            ("Cell ID URC off", "AT+ECELLID=0"),
            ("Cell ID query", "AT+ECELLID?"),
        ):
            b = Gtk.Button(label=label)
            if cmd == "AT+ECELLMEAS=1":
                b.connect("clicked", self._on_start_cell_meas)
            elif cmd == "AT+ECELLMEAS=0":
                b.connect("clicked", self._on_stop_cell_meas)
            else:
                b.connect("clicked", self._make_preset_handler(cmd))
            cell_buttons.pack_start(b, False, False, 0)
            self.cell_meas_buttons[label] = b

        pos_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        pos_box.pack_start(Gtk.Label(label="Positioning", xalign=0), False, False, 0)
        pos_grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        pos_box.pack_start(pos_grid, False, False, 0)
        self.pos_labels: dict[str, Gtk.Label] = {}
        for row, key in enumerate((
            "Latitude", "Longitude", "Altitude", "Accuracy",
            "Method", "City", "State", "ZIP", "Country",
            "Wi-Fi MAC", "Confidence",
        )):
            pos_grid.attach(Gtk.Label(label=key + ":", xalign=0), 0, row, 1, 1)
            lbl = Gtk.Label(label="-", xalign=0, selectable=True)
            self.pos_labels[key] = lbl
            pos_grid.attach(lbl, 1, row, 1, 1)

        pos_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        pos_box.pack_start(pos_buttons, False, False, 0)
        for label, cmd in (
            ("Location UI on", "AT+ELOCAEN=1"),
            ("Location UI off", "AT+ELOCAEN=0"),
            ("Location UI query", "AT+ELOCAEN?"),
            ("Geolocation info", "AT+EIMSGEO?"),
        ):
            b = Gtk.Button(label=label)
            b.connect("clicked", self._make_preset_handler(cmd))
            pos_buttons.pack_start(b, False, False, 0)

        self._positioning_box = pos_box
        self._cell_meas_status = False

    def _build_network_tab(self, parent: Gtk.Box):
        info_frame = Gtk.Frame(label="Network control (MediaTek proprietary)")
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        info_frame.add(info_box)
        parent.pack_start(info_frame, False, False, 0)
        info_box.pack_start(Gtk.Label(
            label="AT+ERAT – RAT mode lock\n"
                  "AT+ECSG – CSG network selection\n"
                  "AT+EPOL – Preferred operator list\n"
                  "AT+EPOF – Power off modem\n"
                  "AT+EPON – Reset modem\n"
                  "AT+ESLP – Sleep mode\n"
                  "AT+ECELCK – Cell lock\n"
                  "AT+ENWINFO – Network info\n"
                  "AT+ECAMP – Enable ECAMP URC",
            xalign=0), False, False, 0)

        rat_frame = Gtk.Frame(label="RAT mode lock (AT+ERAT)")
        rat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        rat_frame.add(rat_box)
        parent.pack_start(rat_box, False, False, 0)
        rat_grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        rat_box.pack_start(rat_grid, False, False, 0)
        self.rat_labels: dict[str, Gtk.Label] = {}
        for row, key in enumerate(("Mode", "Saved", "Reset")):
            rat_grid.attach(Gtk.Label(label=key + ":", xalign=0), 0, row, 1, 1)
            lbl = Gtk.Label(label="-", xalign=0, selectable=True)
            self.rat_labels[key] = lbl
            rat_grid.attach(lbl, 1, row, 1, 1)
        rat_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        rat_box.pack_start(rat_buttons, False, False, 0)
        for label, cmd in (
            ("Query", "AT+ERAT?"),
            ("Auto (0)", "AT+ERAT=0"),
            ("GSM (1)", "AT+ERAT=1"),
            ("3G (2)", "AT+ERAT=2"),
            ("4G (3)", "AT+ERAT=3"),
            ("5G NR (4)", "AT+ERAT=4"),
            ("Lock no-save (6,4,0)", "AT+ERAT=6,4,0"),
            ("Unlock (6,4,1)", "AT+ERAT=6,4,1"),
        ):
            b = Gtk.Button(label=label)
            b.connect("clicked", self._make_preset_handler(cmd))
            rat_buttons.pack_start(b, False, False, 0)

        power_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        parent.pack_start(power_box, False, False, 0)
        power_box.pack_start(Gtk.Label(label="Power & sleep", xalign=0), False, False, 0)
        power_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        power_box.pack_start(power_buttons, False, False, 0)
        for label, cmd in (
            ("Power off (AT+EPOF)", "AT+EPOF"),
            ("Reset (AT+EPON)", "AT+EPON"),
            ("Sleep (AT+ESLP=1)", "AT+ESLP=1"),
            ("Wake (AT+ESLP=0)", "AT+ESLP=0"),
        ):
            b = Gtk.Button(label=label)
            b.connect("clicked", self._make_preset_handler(cmd))
            power_buttons.pack_start(b, False, False, 0)

        csg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)
        parent.pack_start(csg_box, False, False, 0)
        csg_box.pack_start(Gtk.Label(label="CSG & operator", xalign=0), False, False, 0)
        csg_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        csg_box.pack_start(csg_buttons, False, False, 0)
        for label, cmd in (
            ("CSG auto on", "AT+ECSG=3,1"),
            ("CSG auto off", "AT+ECSG=3,0"),
            ("CSG query", "AT+ECSG=4,2,0"),
            ("POL query", "AT+EPOL?"),
            ("ENWINFO", "AT+ENWINFO?"),
            ("ENWCFGINFO", "AT+ENWCFGINFO?"),
        ):
            b = Gtk.Button(label=label)
            b.connect("clicked", self._make_preset_handler(cmd))
            csg_buttons.pack_start(b, False, False, 0)

    def _log(self, text: str, tag: str | None = None):
        end = self.log_buffer.get_end_iter()
        if tag:
            self.log_buffer.insert_with_tags_by_name(end, text + "\n", tag)
        else:
            self.log_buffer.insert(end, text + "\n")
        n_lines = self.log_buffer.get_line_count()
        if n_lines > self.LOG_MAX:
            start = self.log_buffer.get_start_iter()
            end_cut = self.log_buffer.get_iter_at_line(n_lines - self.LOG_MAX)
            self.log_buffer.delete(start, end_cut)
        mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        self.log_buffer.delete_mark(mark)

    def _refresh_modem_list(self):
        try:
            manager = self.bus.get_object(OFONO_BUS, OFONO_MANAGER_PATH)
            modems = manager.GetModems(dbus_interface=OFONO_MANAGER_IFACE)
        except dbus.DBusException as e:
            self._log(f"[oFono] enumerate failed: {e}", "err")
            return
        self.modem_combo.remove_all()
        self._modem_paths = []
        for entry in modems:
            try:
                path = str(entry[0])
                props = dict(entry[1]) if len(entry) > 1 else {}
            except (IndexError, TypeError):
                continue
            revision = props.get("Revision", "") or ""
            serial = (props.get("Serial", "") or "")[:8]
            label = f"{revision} {serial}".strip() or path
            self._modem_paths.append(path)
            self.modem_combo.append(path, f"{label} ({path})")
        if self._modem_paths:
            self.modem_combo.set_active(0)
        else:
            self.modem = None
            self._log("[oFono] No modems found", "info")

    def _on_modem_changed(self, combo):
        idx = combo.get_active()
        if idx is None or idx < 0 or idx >= len(self._modem_paths):
            self.modem = None
            return
        path = self._modem_paths[idx]
        try:
            self.modem = OfonoModem(self.bus, path)
            self._log(f"[oFono] Selected {path}", "ok")
            self._refresh_info()
            self._refresh_signal_info()
        except dbus.DBusException as e:
            self.modem = None
            self._log(f"[oFono] cannot open modem: {e}", "err")

    def _refresh_info(self):
        if not self.modem:
            return
        try:
            info = self.modem.get_modem_info()
        except dbus.DBusException as e:
            self._log(f"[oFono] info refresh failed: {e}", "err")
            return
        self.info_labels["Manufacturer"].set_text(
            self._fmt(info.get("manufacturer")) or "-")
        self.info_labels["Model"].set_text(
            self._fmt(info.get("model")) or "-")
        self.info_labels["Revision"].set_text(
            self._fmt(info.get("revision")) or "-")
        self.info_labels["IMEI"].set_text(
            self._fmt(info.get("imei")) or self._fmt(info.get("serial")) or "-")
        self.info_labels["IMSI"].set_text(
            self._fmt(info.get("imsi")) or "-")
        self.info_labels["Online"].set_text("yes" if info.get("online") else "no")
        self.info_labels["Powered"].set_text("yes" if info.get("powered") else "no")
        self.info_labels["Type"].set_text(self._fmt(info.get("type")) or "-")
        self.info_labels["Operator"].set_text(
            self._fmt(info.get("operator_name")) or "-")
        self.info_labels["Registration"].set_text(
            self._fmt(info.get("registration")) or "-")
        self.info_labels["Tech"].set_text(
            (info.get("technology") or "-").upper())
        self.info_labels["Strength"].set_text(
            f"{info['strength']}%" if info.get("strength") is not None else "-")

    @staticmethod
    def _fmt(v) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        return s if s and s.lower() != "none" else ""

    def _refresh_signal_info(self):
        if not self.modem:
            return
        self._refresh_info()
        try:
            raw_cesq = self.modem.command("AT+CESQ", timeout=5)
        except Exception as e:  # noqa: BLE001
            self._log(f"[AT] CESQ failed: {e}", "err")
            raw_cesq = ""
        cesq = parse_cesq(raw_cesq)
        try:
            raw_cereg = self.modem.command("AT+CEREG?", timeout=5)
        except Exception as e:  # noqa: BLE001
            self._log(f"[AT] CEREG failed: {e}", "err")
            raw_cereg = ""
        cereg = parse_cereg(raw_cereg)

        def fmt_dbm(v):
            return f"{v:.1f}" if v is not None else "-"

        def fmt_db(v):
            return f"{v:.1f}" if v is not None else "-"

        info = self.modem.get_modem_info() if self.modem else {}
        rat = ACCESS_TECH.get(cereg.get("act", -1)) or info.get("technology", "")
        self.signal_labels["RAT"].set_text(
            (rat or "-").upper() if rat != "-" else "-")
        mcc_text = (info.get("mcc") or "?") + "/" + (info.get("mnc") or "?")
        self.signal_labels["MCC/MNC"].set_text(mcc_text)
        self.signal_labels["TAC"].set_text(
            self._fmt(cereg.get("tac")) or "-")
        self.signal_labels["Cell ID"].set_text(
            self._fmt(cereg.get("cell_id")) or "-")
        self.signal_labels["RSRP (dBm)"].set_text(fmt_dbm(cesq.get("rsrp_dbm")))
        self.signal_labels["RSRQ (dB)"].set_text(fmt_db(cesq.get("rsrq_db")))
        self.signal_labels["RSSI (dBm)"].set_text(fmt_dbm(cesq.get("rssi_dbm")))
        self.signal_labels["SINR (dB)"].set_text(fmt_db(cesq.get("sinr_db")))
        self.signal_labels["RXLEV"].set_text(
            str(cesq["rxlev"]) if "rxlev" in cesq else "-")
        self.signal_labels["RSCP (dBm)"].set_text(fmt_dbm(cesq.get("rscp_dbm")))
        self.signal_labels["ECNO (dB)"].set_text(
            f"{cesq['ecno']/2 - 24.5:.1f}" if "ecno" in cesq and cesq["ecno"] != 255 else "-")

        def fmt_dbm(v):
            return f"{v:.1f}" if v is not None else "-"

        def fmt_db(v):
            return f"{v:.1f}" if v is not None else "-"

        self.signal_labels["RAT"].set_text(
            ACCESS_TECH.get(cereg.get("act", -1), str(cereg.get("act", "-"))))
        info = self.modem.get_modem_info() if self.modem else {}
        mcc_text = (info.get("mcc") or "?") + "/" + (info.get("mnc") or "?")
        self.signal_labels["MCC/MNC"].set_text(mcc_text)
        self.signal_labels["TAC"].set_text(
            self._fmt(cereg.get("tac")) or "-")
        self.signal_labels["Cell ID"].set_text(
            self._fmt(cereg.get("cell_id")) or "-")
        self.signal_labels["RSRP (dBm)"].set_text(fmt_dbm(cesq.get("rsrp_dbm")))
        self.signal_labels["RSRQ (dB)"].set_text(fmt_db(cesq.get("rsrq_db")))
        self.signal_labels["RSSI (dBm)"].set_text(fmt_dbm(cesq.get("rssi_dbm")))
        self.signal_labels["SINR (dB)"].set_text(fmt_db(cesq.get("sinr_db")))
        self.signal_labels["RXLEV"].set_text(
            str(cesq["rxlev"]) if "rxlev" in cesq else "-")
        self.signal_labels["RSCP (dBm)"].set_text(fmt_dbm(cesq.get("rscp_dbm")))
        self.signal_labels["ECNO (dB)"].set_text(
            f"{cesq['ecno']/2 - 24.5:.1f}" if "ecno" in cesq and cesq["ecno"] != 255 else "-")

    def _make_preset_handler(self, cmd: str):
        def handler(_btn):
            self.entry.set_text(cmd)
            self._do_send(cmd)
        return handler

    def _on_start_cell_meas(self, _btn):
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        try:
            self.modem.command("AT+ECELLMEAS=1", timeout=10)
            self._cell_meas_status = True
            self.cell_labels["Status"].set_text("measuring")
            self._log("[AT] ECELLMEAS started", "ok")
        except Exception as e:  # noqa: BLE001
            self._log(f"[AT] ECELLMEAS=1 failed: {e}", "err")

    def _on_stop_cell_meas(self, _btn):
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        try:
            self.modem.command("AT+ECELLMEAS=0", timeout=10)
            self._cell_meas_status = False
            self.cell_labels["Status"].set_text("stopped")
            self._log("[AT] ECELLMEAS stopped", "ok")
        except Exception as e:  # noqa: BLE001
            self._log(f"[AT] ECELLMEAS=0 failed: {e}", "err")

    def _refresh_cell_meas(self):
        if not self.modem:
            return
        try:
            raw = self.modem.command("AT+ECELLMEAS=1", timeout=10)
        except Exception as e:  # noqa: BLE001
            self._log(f"[AT] ECELLMEAS failed: {e}", "err")
            return
        data = parse_ecellmeas(raw)
        if not data:
            self._log("[AT] ECELLMEAS: no data", "err")
            return
        rat = data.get("rat")
        rat_str = {7: "LTE", 11: "NR", 13: "LTE (ENDC)", 256: "C2K"}.get(rat, str(rat)) if rat is not None else "-"
        self.cell_labels["Status"].set_text("measuring")
        self.cell_labels["RAT"].set_text(rat_str)
        self.cell_labels["ARFCN"].set_text(str(data.get("arfcn", "-")))
        self.cell_labels["PCI"].set_text(str(data.get("pci", "-")))
        rsrp = data.get("rsrp")
        if rsrp is not None:
            try:
                rsrp_int = int(rsrp)
                rsrp_dbm = rsrp_int - 141 if 0 <= rsrp_int <= 97 else (rsrp_int - 141)
                self.cell_labels["RSRP (dBm)"].set_text(f"{rsrp_dbm}")
            except (ValueError, TypeError):
                self.cell_labels["RSRP (dBm)"].set_text(str(rsrp))
        else:
            self.cell_labels["RSRP (dBm)"].set_text("-")
        rsrq = data.get("rsrq")
        if rsrq is not None:
            try:
                rsrq_int = int(rsrq)
                rsrq_db = rsrq_int / 2 - 19.5 if 0 <= rsrq_int <= 34 else str(rsrq_int)
                self.cell_labels["RSRQ (dB)"].set_text(str(rsrq_db))
            except (ValueError, TypeError):
                self.cell_labels["RSRQ (dB)"].set_text(str(rsrq))
        else:
            self.cell_labels["RSRQ (dB)"].set_text("-")
        snr = data.get("snr")
        if snr is not None:
            self.cell_labels["SNR (dB)"].set_text(str(snr))
        else:
            self.cell_labels["SNR (dB)"].set_text("-")
        self.cell_labels["Cell ID"].set_text(str(data.get("cid", "-")))
        plmns = data.get("plmn", [])
        if plmns:
            self.cell_labels["PLMNs"].set_text(
                ", ".join(f"{p[0]}/{p[1]}" for p in plmns))
        else:
            self.cell_labels["PLMNs"].set_text("-")
        self.cell_labels["Band"].set_text(str(data.get("band", "-")))

    def _on_send(self, _widget=None):
        cmd = self.entry.get_text().strip()
        if not cmd:
            return
        self._do_send(cmd)
        self.entry.set_text("")

    def _do_send(self, cmd: str):
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        timeout = int(self.timeout_spin.get_value())
        self._log(f"--> {cmd}  (timeout {timeout}s)", "tx")
        self._command_history.append(cmd)
        self.history_store.append((GLib.DateTime.new_now_local().format("%H:%M:%S"), cmd))

        def worker():
            try:
                resp = self.modem.command(cmd, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._log, f"[!] error: {e}", "err")
                return
            GLib.idle_add(self._log, f"<-- {resp}", "rx")

        threading.Thread(target=worker, daemon=True).start()

    def _on_history_select(self, sel):
        model, _iter = sel.get_selected()
        if model:
            cmd = model.get_value(_iter, 1)
            self.entry.set_text(cmd)

    def _on_rerun_selected(self, _btn):
        sel = self.history_view.get_selection()
        model, _iter = sel.get_selected()
        if not model:
            return
        cmd = model.get_value(_iter, 1)
        self._do_send(cmd)

    def _on_entry_key(self, _widget, event):
        if not self._command_history:
            return False
        key = Gdk.keyval_name(event.keyval)
        if key == "Up":
            self._history_pos = max(0, self._history_pos - 1)
        elif key == "Down":
            self._history_pos = min(len(self._command_history), self._history_pos + 1)
        else:
            return False
        idx = len(self._command_history) - 1 - self._history_pos
        if 0 <= idx < len(self._command_history):
            self.entry.set_text(self._command_history[idx])
            self.entry.set_position(-1)
        else:
            self.entry.set_text("")
        return True

    def _poll_signal(self) -> bool:
        if self.modem:
            self._refresh_info()
        return True

    def _on_props_changed(self, path, interface, changed):
        if not self.modem or path != self.modem.path:
            return
        if interface in (OFONO_MODEM_IFACE, OFONO_NETWORK_REG_IFACE,
                         OFONO_SIM_IFACE):
            GLib.idle_add(self._refresh_info)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="es.n0p.fiveg_at_tool")

    def do_activate(self):
        win = MainWindow(self)
        self.hold()
        win.connect("destroy", lambda *_: self.release())
        win.show_all()


def main():
    app = App()
    return app.run(sys.argv)


if __name__ == "__main__":
    main()