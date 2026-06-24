"""Adw/GTK4 UI for FuriModem Tool.

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
    CEREG_STATUS,
    parse_cereg,
    parse_cesq,
    parse_ecell,
)


LOG_MAX = 5000
HISTORY_MAX = 200
POLL_INTERVAL_S = 2


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="FuriModem Tool")
        self.set_default_size(1100, 760)

        self.bus = dbus.SystemBus()
        self.modem: OfonoModem | None = None
        self._modem_paths: list[str] = []
        self._modem_list = Gtk.StringList()
        self._command_history: deque[str] = deque(maxlen=HISTORY_MAX)
        self._history_pos = 0
        self._cell_meas_status = False
        self._cell_meas_raw = ""
        self._neighbor_cells: list[dict] = []

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
        terminal_page = self._build_terminal_page()
        self._view_stack.add_titled(status_page, "status", "Status")
        self._view_stack.add_titled(terminal_page, "terminal", "Terminal")
        status_page.set_icon_name("network-cellular-signal-good-symbolic")
        terminal_page.set_icon_name("utilities-terminal-symbolic")

        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self._view_stack)
        switcher_bar.set_reveal(True)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Adw.WindowTitle.new("FuriModem Tool", self._subtitle_text())
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

    @staticmethod
    def _make_prop_grid(keys: tuple[str, ...], cols: int = 2) -> tuple[Gtk.Grid, dict[str, Gtk.Label]]:
        grid = Gtk.Grid()
        grid.set_column_spacing(16)
        grid.set_row_spacing(4)
        grid.set_margin_top(8)
        grid.set_margin_bottom(4)
        grid.set_margin_start(8)
        grid.set_margin_end(8)
        labels: dict[str, Gtk.Label] = {}
        for i, key in enumerate(keys):
            col = i % cols
            row_pos = i // cols
            lbl_key = Gtk.Label(label=key)
            lbl_key.set_halign(Gtk.Align.START)
            lbl_key.add_css_class("caption")
            lbl_key.set_opacity(0.7)
            lbl_val = Gtk.Label(label="-")
            lbl_val.set_halign(Gtk.Align.END)
            lbl_val.set_margin_start(8)
            labels[key] = lbl_val
            grid.attach(lbl_key, col * 2, row_pos, 1, 1)
            grid.attach(lbl_val, col * 2 + 1, row_pos, 1, 1)
        return grid, labels

    def _build_status_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Status")

        signal_group = Adw.PreferencesGroup()
        signal_group.set_title("Network & Signal")
        signal_group.set_description("Auto-refresh every 2 s")
        sig_grid, self._sig_labels = self._make_prop_grid(
            ("Registration", "RAT", "Operator", "MCC/MNC", "TAC", "Cell ID",
             "Strength", "Online", "Powered", "Tech",
             "RSRP (dBm)", "RSRQ (dB)", "RSSI (dBm)", "SINR (dB)",
             "RXLEV", "RSCP (dBm)", "ECNO (dB)"),
        )
        signal_group.add(sig_grid)
        refresh_sig_btn = Gtk.Button(label="Refresh signal")
        refresh_sig_btn.set_halign(Gtk.Align.START)
        refresh_sig_btn.connect("clicked", lambda *_: self._refresh_signal_info())
        signal_group.add(refresh_sig_btn)
        page.add(signal_group)

        cell_group = Adw.PreferencesGroup()
        cell_group.set_title("Serving Cell (AT+ECELL)")
        cell_grid, self._cell_labels = self._make_prop_grid(
            ("Status", "RAT", "ARFCN", "PCI",
             "RSRP (dBm)", "RSRQ (dB)", "SNR (dB)", "Cell ID",
             "PLMNs", "Band"),
        )
        cell_row = Adw.ActionRow()
        cell_row.set_activatable_widget(cell_grid)
        cell_row.add_suffix(cell_grid)
        cell_group.add(cell_row)
        cell_btns = (
            ("Start meas", self._on_start_cell_meas),
            ("Stop meas", self._on_stop_cell_meas),
            ("Refresh cell", self._refresh_cell_meas),
        )
        flow = Gtk.FlowBox()
        flow.set_max_children_per_line(4)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_column_spacing(6)
        flow.set_row_spacing(6)
        flow.set_homogeneous(True)
        for label, handler in cell_btns:
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.connect("clicked", handler)
            flow.append(btn)
        btn_row = Adw.ActionRow()
        btn_row.set_activatable_widget(flow)
        btn_row.add_suffix(flow)
        cell_group.add(btn_row)
        page.add(cell_group)

        neighbor_group = Adw.PreferencesGroup()
        neighbor_group.set_title("Neighbouring Cells")
        neighbor_group.set_description("Refresh cell info above first")
        neighbor_stack = Adw.ViewStack()
        neighbor_stack.set_vexpand(False)
        self._neighbor_stack = neighbor_stack
        neighbor_switcher = Adw.ViewSwitcherBar()
        neighbor_switcher.set_stack(neighbor_stack)
        neighbor_switcher.set_reveal(True)
        ns_row = Adw.ActionRow()
        ns_row.set_activatable_widget(neighbor_stack)
        ns_row.add_suffix(neighbor_stack)
        neighbor_group.add(ns_row)
        ns_sw_row = Adw.ActionRow()
        ns_sw_row.set_activatable_widget(neighbor_switcher)
        ns_sw_row.add_suffix(neighbor_switcher)
        neighbor_group.add(ns_sw_row)
        page.add(neighbor_group)

        modem_group = Adw.PreferencesGroup()
        modem_group.set_title("Identifiers")
        self._info_long_rows: dict[str, Adw.ActionRow] = {}
        for key in ("Revision", "IMEI", "IMSI"):
            row = Adw.ActionRow()
            row.set_title(key)
            row.set_subtitle("-")
            self._info_long_rows[key] = row
            modem_group.add(row)
        refresh_info_btn = Gtk.Button(label="Refresh modem info")
        refresh_info_btn.set_halign(Gtk.Align.START)
        refresh_info_btn.connect("clicked", lambda *_: self._refresh_info())
        modem_group.add(refresh_info_btn)
        page.add(modem_group)

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

        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_box.set_margin_top(8)
        action_box.set_margin_bottom(8)
        send_btn = Gtk.Button(label="Send")
        send_btn.add_css_class("suggested-action")
        send_btn.connect("clicked", self._on_send)
        action_box.append(send_btn)
        clear_btn = Gtk.Button(label="Clear log")
        clear_btn.connect("clicked", lambda *_: self.log_buffer.set_text("", -1))
        action_box.append(clear_btn)
        entry = Adw.ActionRow()
        entry.set_activatable_widget(action_box)
        entry.set_title("Actions")
        entry.add_suffix(action_box)
        cmd_group.add(entry)
        page.add(cmd_group)

        presets_group = Adw.PreferencesGroup()
        presets_group.set_title("AT Presets")
        DISRUPTIVE = ("AT+EPOF", "AT+EPON", "AT+ESLP=1")
        presets = (
            ("ATI", "Terminal info", "ATI"),
            ("CGMI", "Manufacturer", "AT+CGMI"),
            ("CGMM", "Model", "AT+CGMM"),
            ("CGMR", "Firmware rev", "AT+CGMR"),
            ("CGSN", "IMEI", "AT+CGSN"),
            ("CIMI", "IMSI", "AT+CIMI"),
            ("COPS?", "Operator", "AT+COPS?"),
            ("CESQ", "3GPP signal", "AT+CESQ"),
            ("ECSQ", "Ericsson sig", "AT+ECSQ"),
            ("CREG?", "2G/3G reg", "AT+CREG?"),
            ("CEREG?", "4G reg", "AT+CEREG?"),
            ("C5GREG?", "5G reg", "AT+C5GREG?"),
            ("GCAP", "Capabilities", "AT+GCAP"),
            ("ECID", "Cell ID", "AT+ECID"),
            ("CGATT?", "Packet attach", "AT+CGATT?"),
            ("CPIN?", "SIM status", "AT+CPIN?"),
            ("EXOPL", "Full op scan", "AT+EXOPL"),
            ("EPRATL", "RAT list pref", "AT+EPRATL?"),
            ("E5GOPT?", "5G mode cfg", "AT+E5GOPT?"),
            ("ERAT?", "Query RAT", "AT+ERAT?"),
            ("ECAINFO", "Carrier agg", "AT+ECAINFO?"),
            ("ENRCABAND", "NR band info", "AT+ENRCABAND?"),
            ("ECCAUSE", "Reject cause", "AT+ECCAUSE?"),
            ("EONS?", "Op name disp", "AT+EONS?"),
            ("ELCE?", "Link capacity", "AT+ELCE?"),
            ("ECELCK?", "Cell lock qry", "AT+ECELCK?"),
            ("EPOF", "Power off !", "AT+EPOF"),
            ("ESLP?", "Sleep query", "AT+ESLP?"),
        )
        preset_flow = Gtk.FlowBox()
        preset_flow.set_max_children_per_line(6)
        preset_flow.set_min_children_per_line(3)
        preset_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        preset_flow.set_column_spacing(2)
        preset_flow.set_row_spacing(2)
        preset_flow.set_homogeneous(True)
        for label, desc, cmd in presets:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0.5)
            lbl.add_css_class("heading")
            sub = Gtk.Label(label=desc)
            sub.set_xalign(0.5)
            sub.add_css_class("caption")
            sub.set_opacity(0.6)
            box.append(lbl)
            box.append(sub)
            btn = Gtk.Button(child=box)
            btn.add_css_class("flat")
            btn.set_size_request(-1, 36)
            if cmd in DISRUPTIVE:
                btn.connect("clicked", lambda *_, c=cmd: self._confirm_and_send(c))
            else:
                btn.connect("clicked", self._make_preset_handler(cmd))
            preset_flow.append(btn)
        presets_group.add(preset_flow)
        page.add(presets_group)

        log_view = Gtk.TextView(buffer=self.log_buffer, editable=False, monospace=True,
                               cursor_visible=False, top_margin=4, bottom_margin=4,
                               left_margin=6, right_margin=6, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        log_view.add_css_class("monospace")
        self.log_view = log_view
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(log_view)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.ALWAYS)
        scrolled.set_size_request(-1, 180)
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

    @staticmethod
    def _ecell_rat_str(act: int | None) -> str:
        m = {7: "LTE", 11: "NR", 13: "LTE (ENDC)", 256: "C2K"}
        return m.get(act, str(act)) if act is not None else "-"

    @staticmethod
    def _ecell_rsrp_str(c: dict) -> str:
        act = c.get("act")
        sig1 = c.get("sig1")
        sig1_dbm = c.get("sig1_in_dbm")
        if sig1 is None or not isinstance(sig1, int):
            return str(sig1_dbm) if isinstance(sig1_dbm, int) else "-"
        if act in (7,):  # LTE: 3GPP index 0-97
            if 0 <= sig1 <= 97:
                return f"{sig1 - 141:.0f}"
            return str(sig1)
        if act in (11, 13):  # NR: SS-RSRP quarter-dBm
            return f"{sig1 / 4:.1f}"
        if act in (2, 4, 5, 6):  # UMTS: RSCP 0-96
            if 0 <= sig1 <= 96:
                return f"{sig1 - 121:.0f}"
            return str(sig1)
        return str(sig1_dbm) if isinstance(sig1_dbm, int) else str(sig1)

    @staticmethod
    def _ecell_rsrq_str(c: dict) -> str:
        act = c.get("act")
        sig2 = c.get("sig2")
        sig2_dbm = c.get("sig2_in_dbm")
        if sig2 is None or not isinstance(sig2, int):
            return str(sig2_dbm) if isinstance(sig2_dbm, int) else "-"
        if act in (7,):  # LTE: RSRQ 0-34 -> -19.5 to -3 dB
            if 0 <= sig2 <= 34:
                return f"{sig2 / 2.0 - 19.5:.1f}"
            return str(sig2)
        if act in (11, 13):  # NR: SS-RSRQ quarter-dBm
            return f"{sig2 / 4:.1f}"
        return str(sig2_dbm) if isinstance(sig2_dbm, int) else str(sig2)

    def _log(self, text: str, tag: str | None = None) -> None:
        end = self.log_buffer.get_end_iter()
        msg = text.replace("\r", "") + "\n"
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

    def _set_label(self, key: str, val: str) -> None:
        if key in self._sig_labels:
            self._sig_labels[key].set_text(val or "-")
        elif key in self._info_long_rows:
            self._info_long_rows[key].set_subtitle(val or "-")

    def _refresh_info(self) -> None:
        if not self.modem:
            return
        try:
            info = self.modem.get_modem_info()
        except dbus.DBusException as e:
            self._log(f"[oFono] info refresh failed: {e}", "err")
            return
        self._set_label("Revision", self._fmt(info.get("revision")))
        self._set_label("IMEI", self._fmt(info.get("imei")) or self._fmt(info.get("serial")))
        self._set_label("IMSI", self._fmt(info.get("imsi")))
        self._set_label("Online", "yes" if info.get("online") else "no")
        self._set_label("Powered", "yes" if info.get("powered") else "no")
        self._set_label("Tech", (info.get("technology") or "-").upper())

    @staticmethod
    def _fmt(v) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        return s if s and s.lower() != "none" else ""

    def _refresh_signal_info(self) -> None:
        if not self.modem:
            return
        info = self.modem.get_modem_info()
        self._sig_labels["Operator"].set_text(self._fmt(info.get("operator_name")) or "-")
        reg = info.get("registration") or "unknown"
        self._sig_labels["Registration"].set_text(reg)
        self._sig_labels["MCC/MNC"].set_text((info.get("mcc") or "?") + "/" + (info.get("mnc") or "?"))
        self._sig_labels["Strength"].set_text(
            f"{info['strength']}%" if info.get("strength") is not None else "-")

        tech = (info.get("technology") or "").upper()
        self._sig_labels["RAT"].set_text(tech or "-")
        cell_id = info.get("cell_id", 0)
        self._sig_labels["Cell ID"].set_text(str(cell_id) if cell_id else "-")

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

        rat_num = cereg.get("act")
        rat = ACCESS_TECH.get(rat_num, str(rat_num)) if rat_num is not None else None
        if rat:
            self._sig_labels["RAT"].set_text(rat)
        tac = self._fmt(cereg.get("tac"))
        if tac:
            self._sig_labels["TAC"].set_text(tac)
        cid = self._fmt(cereg.get("cell_id"))
        if cid:
            self._sig_labels["Cell ID"].set_text(cid)
        reg_status = cereg.get("status")
        if reg_status is not None:
            reg_text = CEREG_STATUS.get(reg_status, str(reg_status))
            self._sig_labels["Registration"].set_text(reg_text)

        sl = self._sig_labels
        sl["RSRP (dBm)"].set_text(fmt_dbm(cesq.get("rsrp_dbm")))
        sl["RSRQ (dB)"].set_text(fmt_db(cesq.get("rsrq_db")))
        sl["RSSI (dBm)"].set_text(fmt_dbm(cesq.get("rssi_dbm")))
        sl["SINR (dB)"].set_text(fmt_db(cesq.get("sinr_db")))
        sl["RXLEV"].set_text(str(cesq["rxlev"]) if "rxlev" in cesq else "-")
        sl["RSCP (dBm)"].set_text(fmt_dbm(cesq.get("rscp_dbm")))
        ecno = cesq.get("ecno")
        sl["ECNO (dB)"].set_text(f"{ecno/2 - 24.5:.1f}" if ecno is not None and ecno != 255 else "-")

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
            self._cell_labels["Status"].set_text("measuring")
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
            self._cell_labels["Status"].set_text("stopped")
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
        self._neighbor_cells = cells[1:] if len(cells) > 1 else []
        self._log(f"[AT] ECELL: {len(cells)} cells ({len(self._neighbor_cells)} neighbours)", "info")
        try:
            self._refresh_neighbor_cells()
        except Exception as e:
            self._log(f"[UI] neighbor update failed: {e}", "err")
        c = cells[0]
        cl = self._cell_labels
        cl["Status"].set_text("ok")
        cl["RAT"].set_text(self._ecell_rat_str(c.get("act")))
        cl["ARFCN"].set_text(str(c.get("ext4", "-")))
        cl["PCI"].set_text(str(c.get("psc_or_pci", "-")))
        cl["RSRP (dBm)"].set_text(self._ecell_rsrp_str(c))
        cl["RSRQ (dB)"].set_text(self._ecell_rsrq_str(c))
        ext1 = c.get("ext1")
        cl["SNR (dB)"].set_text(str(ext1) if isinstance(ext1, int) else "-")
        cl["Cell ID"].set_text(str(c.get("cid", "-")))
        cl["PLMNs"].set_text(f"{c.get('mcc', '?')}/{c.get('mnc', '?')}")
        cl["Band"].set_text(str(c.get("ext3", "-")))

    def _build_neighbor_page(self, idx: int, cell: dict) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        group = Adw.PreferencesGroup()
        group.set_title(f"Neighbour #{idx + 1}")
        grid, labels = self._make_prop_grid(
            ("RAT", "PCI", "Cell ID", "MCC/MNC",
             "RSRP (dBm)", "RSRQ (dB)"),
        )
        labels["RAT"].set_text(self._ecell_rat_str(cell.get("act")))
        labels["PCI"].set_text(str(cell.get("psc_or_pci", "-")))
        labels["Cell ID"].set_text(str(cell.get("cid", "-")))
        mcc = cell.get("mcc")
        mnc = cell.get("mnc")
        labels["MCC/MNC"].set_text(f"{mcc}/{mnc}" if mcc is not None and mnc is not None else "-")
        labels["RSRP (dBm)"].set_text(self._ecell_rsrp_str(cell))
        labels["RSRQ (dB)"].set_text(self._ecell_rsrq_str(cell))
        gr = Adw.ActionRow()
        gr.set_activatable_widget(grid)
        gr.add_suffix(grid)
        group.add(gr)
        box.append(group)
        return box

    def _rebuild_neighbor_stack(self) -> None:
        pages = self._neighbor_stack.get_pages()
        while pages.get_n_items() > 0:
            page = pages.get_item(0)
            child = page.get_child()
            self._neighbor_stack.remove(child)

    def _refresh_neighbor_cells(self, _btn=None) -> None:
        self._rebuild_neighbor_stack()
        if not self._neighbor_cells:
            lbl = Gtk.Label(label="No neighbour data — refresh cell on Status tab first")
            lbl.set_margin_top(16)
            lbl.set_margin_bottom(16)
            self._neighbor_stack.add_titled(lbl, "none", "-")
            return
        for i, cell in enumerate(self._neighbor_cells):
            page = self._build_neighbor_page(i, cell)
            self._neighbor_stack.add_titled(page, f"neighbor_{i}", f"#{i + 1}")

    # No GNSS AT commands available on this modem.
    # Location is provided by the phone framework (oFono/phosh).
    # See AT+ELOCAEN (location UI) in the Terminal tab presets.

    def _confirm_and_send(self, cmd: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Confirm destructive command",
            body=f"Are you sure?\n'{cmd}' may disrupt the modem.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("send", "Send")
        dialog.set_response_appearance("send", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.choose(self, None, lambda _dlg, result: self._do_send(cmd) if _dlg.choose_finish(result) == "send" else None)

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

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _add_history_row(self, cmd: str) -> None:
        time_str = GLib.DateTime.new_now_local().format("%H:%M:%S")
        row = Adw.ActionRow()
        row.set_title(time_str)
        row.set_subtitle(cmd)
        row.set_activatable(True)
        row.connect("activated", self._make_preset_handler(cmd))
        self._history_group.add(row)

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
        super().__init__(application_id="es.n0p.furimodem_tool")

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
