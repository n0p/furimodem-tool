"""Adw/GTK4 UI for 5G Modem AT Tool.

Layout: Adw.OverlaySplitView with a sidebar (modem selector, log, history)
and a content area with an Adw.ViewStack (Status, Positioning, Terminal).
"""

import sys
import threading
from collections import deque

import dbus
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from ofono import (
    OFONO_BUS,
    OFONO_MANAGER_IFACE,
    OFONO_MANAGER_PATH,
    OFONO_MODEM_IFACE,
    OfonoModem,
    OfonoMonitor,
)
from parsers import (
    ACCESS_TECH,
    parse_cereg,
    parse_cesq,
    parse_ecell,
)


LOG_MAX = 5000
HISTORY_MAX = 200
POLL_INTERVAL_S = 2


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="5G Modem AT Tool (oFono)")
        self.set_default_size(1100, 760)

        self.bus = dbus.SystemBus()
        self.modem: OfonoModem | None = None
        self._modem_paths: list[str] = []
        self._modem_list = Gtk.StringList()
        self._command_history: deque[str] = deque(maxlen=HISTORY_MAX)
        self._history_pos = 0
        self._cell_meas_status = False

        self.log_buffer = Gtk.TextBuffer()
        for name, fg, weight in (
            ("tx", "#0a84ff", 700),
            ("rx", "#34c759", 400),
            ("info", "#888888", 400),
            ("err", "#ff3b30", 700),
            ("ok", "#34c759", 700),
        ):
            tag = self.log_buffer.create_tag(name)
            if fg:
                r, g, b = int(fg[1:3], 16) / 255, int(fg[3:5], 16) / 255, int(fg[5:7], 16) / 255
                tag.props.foreground_rgba = Gdk.RGBA(r, g, b, 1)
            if weight:
                tag.props.weight = weight

        self.monitor = OfonoMonitor(self.bus, self._refresh_modem_list)
        self.bus.add_signal_receiver(
            self._on_props_changed,
            signal_name="PropertiesChanged",
            dbus_interface="org.ofono.Modem",
            path="/",
            bus_name="org.ofono",
        )
        self._build_ui()
        GLib.timeout_add_seconds(POLL_INTERVAL_S, self._poll_signal)
        self._refresh_modem_list()

    def _build_ui(self) -> None:
        split_view = Adw.OverlaySplitView()
        split_view.set_show_sidebar(True)
        split_view.set_pin_sidebar(True)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar_box.set_size_request(320, -1)
        sidebar_scrolled = Gtk.ScrolledWindow()
        sidebar_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scrolled.set_child(sidebar_box)
        sidebar_scrolled.set_vexpand(True)
        sidebar_scrolled.set_hexpand(False)

        self._build_sidebar(sidebar_box)
        split_view.set_sidebar(sidebar_scrolled)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._view_stack = Adw.ViewStack()
        self._view_stack.set_vexpand(True)
        self._view_stack.set_hexpand(True)

        status_page = self._build_status_page()
        positioning_page = self._build_positioning_page()
        terminal_page = self._build_terminal_page()
        self._view_stack.add_titled(status_page, "status", "Status")
        self._view_stack.add_titled(positioning_page, "positioning", "Positioning")
        self._view_stack.add_titled(terminal_page, "terminal", "Terminal")
        status_page.set_icon_name("network-cellular-signal-good-symbolic")
        positioning_page.set_icon_name("location-symbolic")
        terminal_page.set_icon_name("utilities-terminal-symbolic")

        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self._view_stack)
        switcher_bar.set_reveal(True)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Adw.WindowTitle.new("5G Modem AT Tool", self._subtitle_text())
        header.set_title_widget(title)
        self._title = title
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh modem list")
        refresh_btn.connect("clicked", lambda *_: self._refresh_modem_list())
        header.pack_start(refresh_btn)
        sidebar_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic")
        sidebar_toggle.set_tooltip_text("Toggle sidebar")
        sidebar_toggle.bind_property(
            "active", split_view, "show-sidebar",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        header.pack_end(sidebar_toggle)
        toolbar.add_top_bar(header)
        content_box.append(self._view_stack)
        toolbar.set_content(content_box)
        toolbar.add_bottom_bar(switcher_bar)
        split_view.set_content(toolbar)

        self.set_content(split_view)
        self._split_view = split_view

    def _subtitle_text(self) -> str:
        if not self.modem:
            return "No modem"
        return self.modem.path

    def _build_sidebar(self, box: Gtk.Box) -> None:
        modem_group = Adw.PreferencesGroup()
        modem_group.set_title("Modem")
        self._modem_combo = Adw.ComboRow()
        self._modem_combo.set_title("Modem")
        self._modem_combo.set_model(self._modem_list)
        self._modem_combo.connect("notify::selected", self._on_modem_changed)
        modem_group.add(self._modem_combo)
        box.append(modem_group)

    def _build_status_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Status")

        modem_group = Adw.PreferencesGroup()
        modem_group.set_title("Modem")
        self._info_rows: dict[str, Adw.ActionRow] = {}
        for key in (
            "Manufacturer", "Model", "Revision", "IMEI", "IMSI",
            "Online", "Powered", "Type", "Operator", "Registration",
            "Tech", "Strength",
        ):
            row = Adw.ActionRow()
            row.set_title(key)
            row.set_subtitle("-")
            self._info_rows[key] = row
            modem_group.add(row)
        refresh_info_row = Adw.ButtonRow()
        refresh_info_row.set_title("Refresh modem info")
        refresh_info_row.connect("activated", lambda *_: self._refresh_info())
        modem_group.add(refresh_info_row)
        page.add(modem_group)

        signal_group = Adw.PreferencesGroup()
        signal_group.set_title("5G Signal (AT+CESQ + AT+CEREG)")
        signal_group.set_description("Auto-refresh every 2 s")
        self._signal_rows: dict[str, Adw.ActionRow] = {}
        for key in (
            "RAT", "MCC/MNC", "TAC", "Cell ID",
            "RSRP (dBm)", "RSRQ (dB)", "RSSI (dBm)", "SINR (dB)",
            "RXLEV", "RSCP (dBm)", "ECNO (dB)",
        ):
            row = Adw.ActionRow()
            row.set_title(key)
            row.set_subtitle("-")
            self._signal_rows[key] = row
            signal_group.add(row)
        refresh_sig_row = Adw.ButtonRow()
        refresh_sig_row.set_title("Refresh signal now")
        refresh_sig_row.connect("activated", lambda *_: self._refresh_signal_info())
        signal_group.add(refresh_sig_row)
        page.add(signal_group)

        return page

    def _build_terminal_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Terminal")

        cmd_group = Adw.PreferencesGroup()
        cmd_group.set_title("AT command")
        self._entry_row = Adw.EntryRow()
        self._entry_row.set_title("Command")
        self._entry_row.set_text("")
        self._entry_row.set_tooltip_text("Enter AT command and press Enter")
        self._entry_row.connect("entry-activated", self._on_send)
        self._entry_row.connect("apply", self._on_send)
        cmd_group.add(self._entry_row)

        self._timeout_row = Adw.SpinRow()
        self._timeout_row.set_title("Timeout (seconds)")
        self._timeout_row.set_adjustment(Gtk.Adjustment.new(10, 1, 600, 1, 10, 0))
        cmd_group.add(self._timeout_row)

        send_row = Adw.ButtonRow()
        send_row.set_title("Send")
        send_row.connect("activated", self._on_send)
        cmd_group.add(send_row)

        clear_row = Adw.ButtonRow()
        clear_row.set_title("Clear log")
        clear_row.connect("activated", lambda *_: self.log_buffer.set_text("", -1))
        cmd_group.add(clear_row)

        page.add(cmd_group)

        presets_group = Adw.PreferencesGroup()
        presets_group.set_title("AT Presets")
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
            row = Adw.ButtonRow()
            row.set_title(label)
            row.connect("activated", self._make_preset_handler(cmd))
            presets_group.add(row)
        page.add(presets_group)

        log_view = Gtk.TextView(buffer=self.log_buffer, editable=False, monospace=True,
                               cursor_visible=False, top_margin=4, bottom_margin=4,
                               left_margin=6, right_margin=6, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        log_view.add_css_class("monospace")
        self.log_view = log_view
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(log_view)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.ALWAYS)
        scrolled.set_size_request(-1, 240)
        scrolled.set_margin_start(8)
        scrolled.set_margin_end(8)
        scrolled.set_margin_top(4)
        scrolled.set_margin_bottom(8)
        scrolled.set_vexpand(True)
        log_group = Adw.PreferencesGroup()
        log_group.set_title("Log")
        log_group.add(scrolled)
        page.add(log_group)

        self._history_group = Adw.PreferencesGroup()
        self._history_group.set_title("History")
        page.add(self._history_group)

        return page

    def _build_positioning_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Positioning")

        cell_group = Adw.PreferencesGroup()
        cell_group.set_title("Cell measurement (AT+ECELLMEAS)")
        cell_group.set_description(
            "AT+ECELLMEAS – LTE/NR cell measurement\n"
            "AT+ECELL – Get serving + neighbouring cell info\n"
            "AT+ECELLID – Report cell ID URC"
        )
        self._cell_rows: dict[str, Adw.ActionRow] = {}
        for key in (
            "Status", "RAT", "ARFCN", "PCI",
            "RSRP (dBm)", "RSRQ (dB)", "SNR (dB)", "Cell ID",
            "PLMNs", "Band",
        ):
            row = Adw.ActionRow()
            row.set_title(key)
            row.set_subtitle("-")
            self._cell_rows[key] = row
            cell_group.add(row)
        for label, cmd, handler in (
            ("Start measurement", "AT+ECELLMEAS=1", self._on_start_cell_meas),
            ("Stop measurement", "AT+ECELLMEAS=0", self._on_stop_cell_meas),
            ("Refresh cell info", "AT+ECELL", self._refresh_cell_meas),
            ("Cell ID URC on", "AT+ECELLID=1", self._make_preset_handler("AT+ECELLID=1")),
            ("Cell ID URC off", "AT+ECELLID=0", self._make_preset_handler("AT+ECELLID=0")),
            ("Cell ID query", "AT+ECELLID?", self._make_preset_handler("AT+ECELLID?")),
        ):
            row = Adw.ButtonRow()
            row.set_title(label)
            row.connect("activated", handler)
            cell_group.add(row)
        page.add(cell_group)

        pos_group = Adw.PreferencesGroup()
        pos_group.set_title("Geolocation")
        pos_group.set_description(
            "AT+ELOCAEN – Location UI on/off\n"
            "AT+EIMSGEO – Geolocation information"
        )
        self._pos_rows: dict[str, Adw.ActionRow] = {}
        for key in (
            "Latitude", "Longitude", "Altitude", "Accuracy",
            "Method", "City", "State", "ZIP", "Country",
            "Wi-Fi MAC", "Confidence",
        ):
            row = Adw.ActionRow()
            row.set_title(key)
            row.set_subtitle("-")
            self._pos_rows[key] = row
            pos_group.add(row)
        for label, cmd in (
            ("Location UI on", "AT+ELOCAEN=1"),
            ("Location UI off", "AT+ELOCAEN=0"),
            ("Location UI query", "AT+ELOCAEN?"),
            ("Geolocation info", "AT+EIMSGEO?"),
        ):
            row = Adw.ButtonRow()
            row.set_title(label)
            row.connect("activated", self._make_preset_handler(cmd))
            pos_group.add(row)
        page.add(pos_group)

        return page

    def _log(self, text: str, tag: str | None = None) -> None:
        end = self.log_buffer.get_end_iter()
        msg = text + "\n"
        if tag:
            self.log_buffer.insert_with_tags_by_name(end, msg, tag)
        else:
            self.log_buffer.insert(end, msg)
        n_lines = self.log_buffer.get_line_count()
        if n_lines > LOG_MAX:
            start = self.log_buffer.get_start_iter()
            end_cut = self.log_buffer.get_iter_at_line(n_lines - LOG_MAX)
            self.log_buffer.delete(start, end_cut)
        mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        self.log_buffer.delete_mark(mark)

    def _refresh_modem_list(self) -> None:
        try:
            manager = self.bus.get_object(OFONO_BUS, OFONO_MANAGER_PATH)
            modems = manager.GetModems(dbus_interface=OFONO_MANAGER_IFACE)
        except dbus.DBusException as e:
            self._log(f"[oFono] enumerate failed: {e}", "err")
            return
        n = self._modem_list.get_n_items()
        self._modem_list.splice(0, n, [])
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
            self._modem_list.append(f"{label} ({path})")
        if self._modem_paths:
            self._modem_combo.set_selected(0)
        else:
            self.modem = None
            self._log("[oFono] No modems found", "info")
        self._title.set_subtitle(self._subtitle_text())

    def _on_modem_changed(self, *_args) -> None:
        idx = self._modem_combo.get_selected()
        if idx < 0 or idx >= len(self._modem_paths) or idx == Gtk.INVALID_LIST_POSITION:
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
        self._title.set_subtitle(self._subtitle_text())

    def _refresh_info(self) -> None:
        if not self.modem:
            return
        try:
            info = self.modem.get_modem_info()
        except dbus.DBusException as e:
            self._log(f"[oFono] info refresh failed: {e}", "err")
            return
        self._set_subtitle(self._info_rows["Manufacturer"], self._fmt(info.get("manufacturer")))
        self._set_subtitle(self._info_rows["Model"], self._fmt(info.get("model")))
        self._set_subtitle(self._info_rows["Revision"], self._fmt(info.get("revision")))
        self._set_subtitle(self._info_rows["IMEI"],
                           self._fmt(info.get("imei")) or self._fmt(info.get("serial")))
        self._set_subtitle(self._info_rows["IMSI"], self._fmt(info.get("imsi")))
        self._set_subtitle(self._info_rows["Online"], "yes" if info.get("online") else "no")
        self._set_subtitle(self._info_rows["Powered"], "yes" if info.get("powered") else "no")
        self._set_subtitle(self._info_rows["Type"], self._fmt(info.get("type")))
        self._set_subtitle(self._info_rows["Operator"], self._fmt(info.get("operator_name")))
        self._set_subtitle(self._info_rows["Registration"], self._fmt(info.get("registration")))
        self._set_subtitle(self._info_rows["Tech"], (info.get("technology") or "-").upper())
        self._set_subtitle(self._info_rows["Strength"],
                           f"{info['strength']}%" if info.get("strength") is not None else "-")

    @staticmethod
    def _fmt(v) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        return s if s and s.lower() != "none" else ""

    @staticmethod
    def _set_subtitle(row: Adw.ActionRow, value: str) -> None:
        row.set_subtitle(value or "-")

    def _refresh_signal_info(self) -> None:
        if not self.modem:
            return
        try:
            raw_cesq = self.modem.command("AT+CESQ", timeout=5)
        except Exception as e:
            self._log(f"[AT] CESQ failed: {e}", "err")
            raw_cesq = ""
        cesq = parse_cesq(raw_cesq)
        try:
            raw_cereg = self.modem.command("AT+CEREG?", timeout=5)
        except Exception as e:
            self._log(f"[AT] CEREG failed: {e}", "err")
            raw_cereg = ""
        cereg = parse_cereg(raw_cereg)

        def fmt_dbm(v):
            return f"{v:.1f}" if v is not None else "-"

        def fmt_db(v):
            return f"{v:.1f}" if v is not None else "-"

        info = self.modem.get_modem_info()
        rat = ACCESS_TECH.get(cereg.get("act", -1), str(cereg.get("act", "-")))
        self._set_subtitle(self._signal_rows["RAT"], rat)
        mcc_text = (info.get("mcc") or "?") + "/" + (info.get("mnc") or "?")
        self._set_subtitle(self._signal_rows["MCC/MNC"], mcc_text)
        self._set_subtitle(self._signal_rows["TAC"], self._fmt(cereg.get("tac")))
        self._set_subtitle(self._signal_rows["Cell ID"], self._fmt(cereg.get("cell_id")))
        self._set_subtitle(self._signal_rows["RSRP (dBm)"], fmt_dbm(cesq.get("rsrp_dbm")))
        self._set_subtitle(self._signal_rows["RSRQ (dB)"], fmt_db(cesq.get("rsrq_db")))
        self._set_subtitle(self._signal_rows["RSSI (dBm)"], fmt_dbm(cesq.get("rssi_dbm")))
        self._set_subtitle(self._signal_rows["SINR (dB)"], fmt_db(cesq.get("sinr_db")))
        self._set_subtitle(self._signal_rows["RXLEV"],
                           str(cesq["rxlev"]) if "rxlev" in cesq else "-")
        self._set_subtitle(self._signal_rows["RSCP (dBm)"], fmt_dbm(cesq.get("rscp_dbm")))
        self._set_subtitle(self._signal_rows["ECNO (dB)"],
                           f"{cesq['ecno']/2 - 24.5:.1f}" if "ecno" in cesq and cesq["ecno"] != 255 else "-")

    def _make_preset_handler(self, cmd: str):
        def handler(_btn):
            self._entry_row.set_text(cmd)
            self._do_send(cmd)
        return handler

    def _on_start_cell_meas(self, _btn) -> None:
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        try:
            self.modem.command("AT+ECELLMEAS=1", timeout=10)
            self._cell_meas_status = True
            self._set_subtitle(self._cell_rows["Status"], "measuring")
            self._log("[AT] ECELLMEAS started", "ok")
        except Exception as e:
            self._log(f"[AT] ECELLMEAS=1 failed: {e}", "err")

    def _on_stop_cell_meas(self, _btn) -> None:
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        try:
            self.modem.command("AT+ECELLMEAS=0", timeout=10)
            self._cell_meas_status = False
            self._set_subtitle(self._cell_rows["Status"], "stopped")
            self._log("[AT] ECELLMEAS stopped", "ok")
        except Exception as e:
            self._log(f"[AT] ECELLMEAS=0 failed: {e}", "err")

    def _refresh_cell_meas(self, _btn=None) -> None:
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        try:
            raw = self.modem.command("AT+ECELL", timeout=10)
        except Exception as e:
            self._log(f"[AT] ECELL failed: {e}", "err")
            return
        cells = parse_ecell(raw)
        if not cells:
            self._log("[AT] ECELL: no data", "err")
            return
        c = cells[0]
        rat_map = {7: "LTE", 11: "NR", 13: "LTE (ENDC)", 256: "C2K"}
        rat = c.get("act")
        self._set_subtitle(self._cell_rows["Status"], "ok")
        self._set_subtitle(self._cell_rows["RAT"], rat_map.get(rat, str(rat)) if rat is not None else "-")
        self._set_subtitle(self._cell_rows["ARFCN"], str(c.get("psc_or_pci", "-")))
        self._set_subtitle(self._cell_rows["PCI"], "-")
        self._set_subtitle(self._cell_rows["RSRP (dBm)"],
                           str(c.get("sig1_in_dbm", "-")) if c.get("sig1_in_dbm") is not None else "-")
        self._set_subtitle(self._cell_rows["RSRQ (dB)"],
                           str(c.get("sig2_in_dbm", "-")) if c.get("sig2_in_dbm") is not None else "-")
        self._set_subtitle(self._cell_rows["SNR (dB)"], "-")
        self._set_subtitle(self._cell_rows["Cell ID"], str(c.get("cid", "-")))
        self._set_subtitle(self._cell_rows["PLMNs"],
                           f"{c.get('mcc', '?')}/{c.get('mnc', '?')}")
        self._set_subtitle(self._cell_rows["Band"], "-")

    def _on_send(self, _widget=None) -> None:
        cmd = self._entry_row.get_text().strip()
        if not cmd:
            return
        self._do_send(cmd)
        self._entry_row.set_text("")

    def _do_send(self, cmd: str) -> None:
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        timeout = int(self._timeout_row.get_value())
        self._log(f"--> {cmd}  (timeout {timeout}s)", "tx")
        self._command_history.append(cmd)
        self._add_history_row(cmd)

        def worker():
            try:
                resp = self.modem.command(cmd, timeout=timeout)
            except Exception as e:
                GLib.idle_add(self._log, f"[!] error: {e}", "err")
                return
            GLib.idle_add(self._log, f"<-- {resp}", "rx")

        threading.Thread(target=worker, daemon=True).start()

    def _add_history_row(self, cmd: str) -> None:
        time_str = GLib.DateTime.new_now_local().format("%H:%M:%S")
        row = Adw.ActionRow()
        row.set_title(time_str)
        row.set_subtitle(cmd)
        row.set_activatable(True)
        row.connect("activated", self._make_preset_handler(cmd))
        self._history_group.prepend(row)
        children = self._history_group.observe_children()
        while children.get_n_items() > HISTORY_MAX:
            last = children.get_item(children.get_n_items() - 1)
            self._history_group.remove(last)

    def _on_entry_key(self, _widget, event) -> bool:
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
            self._entry_row.set_text(self._command_history[idx])
            self._entry_row.set_position(-1)
        else:
            self._entry_row.set_text("")
        return True

    def _poll_signal(self) -> bool:
        if self.modem:
            self._refresh_info()
            self._refresh_signal_info()
        return True

    def _on_props_changed(self, path, interface, changed, _invalidated) -> None:
        if not self.modem or path != self.modem.path:
            return
        if interface in (OFONO_MODEM_IFACE, "org.ofono.NetworkRegistration",
                         "org.ofono.SimManager"):
            GLib.idle_add(self._refresh_info)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="es.n0p.fiveg_at_tool")

    def do_activate(self):
        win = MainWindow(self)
        self.hold()
        win.connect("destroy", lambda *_: self.release())
        win.present()


def main():
    app = App()
    return app.run(sys.argv)


if __name__ == "__main__":
    main()