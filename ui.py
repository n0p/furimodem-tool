"""Adw/GTK4 UI for FuriModem Tool.

Layout: Adw.OverlaySplitView with a sidebar (modem selector, log, history)
and a content area with an Adw.ViewStack (Status, Positioning, Terminal).
"""

import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

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

# logcat tab: /usr/sbin/logcat on FuriOS is a halium-lxc-exec wrapper that
# must run as root (furios has NOPASSWD sudo). Radio logs are very verbose,
# so the in-memory ring buffer is capped by total bytes.
LOGCAT_BIN = "/usr/sbin/logcat"
LOGCAT_FILTER_TERMS = ("PDN", "CME", "RMC", "IMS")
LOGCAT_RING_MAX_BYTES = 256 * 1024
LOGCAT_RENDER_INTERVAL_MS = 250


class _LogcatRing:
    """Byte-capped ring of log lines with a monotonic generation counter.

    append() runs on the reader thread, snapshot()/clear() on the UI thread;
    a lock keeps them consistent. Evicted lines retire by generation so the
    renderer can detect when it fell behind.
    """

    def __init__(self, max_bytes: int):
        self._lines: deque[str] = deque()
        self._bytes = 0
        self._max = max_bytes
        self._head_gen = 0     # generation of _lines[0]
        self._total = 0        # generation of the next appended line
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._bytes += len(line) + 1
            self._total += 1
            while self._bytes > self._max and len(self._lines) > 1:
                self._bytes -= len(self._lines.popleft()) + 1
                self._head_gen += 1

    def snapshot(self) -> tuple[int, int, list[str]]:
        """(head_gen, total_gen, lines)"""
        with self._lock:
            return self._head_gen, self._total, list(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._bytes = 0
            self._head_gen = self._total

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)


def _grep_context_flags(lines: list[str], regex: re.Pattern | None,
                        ctx: int) -> list[bool]:
    """egrep-style: a line is shown if it matches or sits within ctx lines
    of a match."""
    if regex is None:
        return [True] * len(lines)
    hits = [bool(regex.search(l)) for l in lines]
    shown = [False] * len(lines)
    for i, hit in enumerate(hits):
        if hit:
            for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                shown[j] = True
    return shown

# mmcli presets ('-m any' crashes libmm-glib with the ofono2mm backend —
# the modem is always /Modem/0, so pin it)
MMCLI_PRESETS = (
    ("3GPP scan", "-m 0 --3gpp-scan"),
    ("Status", "-m 0 --status"),
    ("Modem list", "-L"),
    ("Signal quality", "-m 0 --signal-quality"),
    ("Simple status", "-m 0 --simple-status"),
    ("Location", "-m 0 --location-get"),
)


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

    def do_destroy(self) -> None:
        self.stop_logcat()
        super().do_destroy()

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
        apn_page = self._build_apn_page()
        net_page = self._build_networks_page()
        mmcli_page = self._build_mmcli_page()
        terminal_page = self._build_terminal_page()
        logcat_page = self._build_logcat_page()
        self._view_stack.add_titled(status_page, "status", "Status")
        self._view_stack.add_titled(apn_page, "apn", "APN")
        self._view_stack.add_titled(net_page, "networks", "Networks")
        self._view_stack.add_titled(mmcli_page, "mmcli", "mmcli")
        self._view_stack.add_titled(terminal_page, "terminal", "Terminal")
        self._view_stack.add_titled(logcat_page, "logcat", "logcat")
        status_page.set_icon_name("network-cellular-signal-good-symbolic")
        apn_page.set_icon_name("network-workgroup-symbolic")
        net_page.set_icon_name("network-cellular-connected-symbolic")
        mmcli_page.set_icon_name("system-search-symbolic")
        terminal_page.set_icon_name("utilities-terminal-symbolic")
        logcat_page.set_icon_name("text-editor-symbolic")

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
        signal_group.set_title("Network &amp; Signal")
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

    def _build_apn_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("APN")

        sim_group = Adw.PreferencesGroup()
        sim_group.set_title("Current SIM")
        self._apn_sim_rows: dict[str, Adw.ActionRow] = {}
        for key in ("Operator", "MCC/MNC", "IMSI", "ICCID"):
            row = Adw.ActionRow()
            row.set_title(key)
            row.set_subtitle("-")
            self._apn_sim_rows[key] = row
            sim_group.add(row)
        page.add(sim_group)

        ctx_group = Adw.PreferencesGroup()
        ctx_group.set_title("PDP contexts")
        ctx_group.set_description(
            "Edit the Access Point Name of the selected context. "
            "Provisioning re-applies the entry matching this SIM's "
            "MCC/MNC from /usr/share/mobile-broadband-provider-info/"
            "serviceproviders.xml")
        self._ctx_list = Gtk.StringList()
        self._ctx_combo = Adw.ComboRow()
        self._ctx_combo.set_title("Context")
        self._ctx_combo.set_model(self._ctx_list)
        self._ctx_combo.connect("notify::selected", self._on_context_changed)
        ctx_group.add(self._ctx_combo)

        self._apn_row = Adw.EntryRow()
        self._apn_row.set_title("APN")
        self._apn_row.set_show_apply_button(True)
        self._apn_row.connect("apply", self._on_apply_apn)
        ctx_group.add(self._apn_row)

        ctx_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ctx_btns.set_margin_top(8)
        ctx_btns.set_margin_bottom(8)
        refresh_ctx_btn = Gtk.Button(label="Refresh")
        refresh_ctx_btn.set_tooltip_text("Reload contexts from oFono")
        refresh_ctx_btn.connect("clicked", lambda *_: self._refresh_apn_tab())
        ctx_btns.append(refresh_ctx_btn)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.set_tooltip_text("Write the APN above to the selected context")
        apply_btn.connect("clicked", self._on_apply_apn)
        ctx_btns.append(apply_btn)
        prov_btn = Gtk.Button(label="from XML")
        prov_btn.set_tooltip_text(
            "org.ofono.ConnectionContext.ProvisionContext — matches MCC/MNC "
            "against serviceproviders.xml and overwrites the context APN")
        prov_btn.connect("clicked", self._on_provision_context)
        ctx_btns.append(prov_btn)
        btn_row = Adw.ActionRow()
        btn_row.set_activatable_widget(ctx_btns)
        btn_row.add_suffix(ctx_btns)
        ctx_group.add(btn_row)
        page.add(ctx_group)

        svc_group = Adw.PreferencesGroup()
        svc_group.set_title("Services")
        svc_group.set_description(
            "Restart the modem stack. Active data calls drop while the "
            "services restart.")
        self._apn_status_row = Adw.ActionRow()
        self._apn_status_row.set_title("Status")
        self._apn_status_row.set_subtitle("idle")
        svc_group.add(self._apn_status_row)

        svc_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        svc_btns.set_margin_top(8)
        svc_btns.set_margin_bottom(8)
        restart_ofono_btn = Gtk.Button(label="Restart oFono")
        restart_ofono_btn.add_css_class("destructive-action")
        restart_ofono_btn.set_tooltip_text("setprop vendor.ril.mtk.restart 1")
        restart_ofono_btn.connect("clicked", lambda *_: self._confirm_run(
            "Restart oFono?",
            "setprop vendor.ril.mtk.restart 1\nThe RIL daemon and oFono "
            "will restart; data drops briefly.",
            "setprop vendor.ril.mtk.restart 1"))
        svc_btns.append(restart_ofono_btn)
        restart_mm_btn = Gtk.Button(label="Restart ModemManager")
        restart_mm_btn.add_css_class("destructive-action")
        restart_mm_btn.set_tooltip_text(
            "systemctl restart ModemManager.service (sudo)")
        restart_mm_btn.connect("clicked", lambda *_: self._confirm_run(
            "Restart ModemManager?",
            "sudo systemctl restart ModemManager.service\n(ofono2mm bridge)",
            "sudo -n systemctl restart ModemManager.service"))
        svc_btns.append(restart_mm_btn)
        svc_row = Adw.ActionRow()
        svc_row.set_activatable_widget(svc_btns)
        svc_row.add_suffix(svc_btns)
        svc_group.add(svc_row)
        page.add(svc_group)

        self._apn_contexts: list[dict] = []
        return page

    # --- APN tab handlers ----------------------------------------------------

    def _set_apn_status(self, text: str) -> None:
        GLib.idle_add(self._apn_status_row.set_subtitle, text)

    def _refresh_apn_tab(self) -> None:
        if not self.modem:
            return
        info = self.modem.get_modem_info()
        rows = self._apn_sim_rows
        op = self._fmt(info.get("operator_name")) or "-"
        mcc_mnc = (self._fmt(info.get("mcc")) or "?") + "/" + \
                  (self._fmt(info.get("mnc")) or "?")
        rows["Operator"].set_subtitle(op)
        rows["MCC/MNC"].set_subtitle(mcc_mnc)
        rows["IMSI"].set_subtitle(self._fmt(info.get("imsi")) or "-")
        rows["ICCID"].set_subtitle(self._fmt(info.get("iccid")) or "-")

        try:
            contexts = self.modem.list_contexts()
        except dbus.DBusException as e:
            self._log(f"[APN] context enumeration failed: {e}", "err")
            return
        self._apn_contexts = contexts
        prev_path = None
        idx = self._ctx_combo.get_selected()
        if 0 <= idx < len(self._apn_contexts):
            prev_path = self._apn_contexts[idx]["path"]
        n = self._ctx_list.get_n_items()
        self._ctx_list.splice(0, n, [])
        for c in contexts:
            self._ctx_list.append(
                f"{c['path']}  [{c['type']}]  {c['apn'] or '(no APN)'}")
        select = 0
        if prev_path:
            for i, c in enumerate(contexts):
                if c["path"] == prev_path:
                    select = i
                    break
        if contexts:
            self._ctx_combo.set_selected(select)
            self._apn_row.set_text(contexts[select]["apn"])
        else:
            self._apn_row.set_text("")
            self._log("[APN] no contexts found", "info")

    def _on_context_changed(self, *_args) -> None:
        idx = self._ctx_combo.get_selected()
        if idx < 0 or idx >= len(self._apn_contexts) \
                or idx == Gtk.INVALID_LIST_POSITION:
            return
        self._apn_row.set_text(self._apn_contexts[idx]["apn"])

    def _selected_context(self) -> dict | None:
        idx = self._ctx_combo.get_selected()
        if idx < 0 or idx >= len(self._apn_contexts) \
                or idx == Gtk.INVALID_LIST_POSITION:
            return None
        return self._apn_contexts[idx]

    def _on_apply_apn(self, *_args) -> None:
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        ctx = self._selected_context()
        if not ctx:
            self._log("[APN] no context selected", "err")
            return
        apn = self._apn_row.get_text().strip()
        if not apn:
            self._log("[APN] empty APN — refusing", "err")
            return
        try:
            self.modem.set_context_apn(ctx["path"], apn)
        except dbus.DBusException as e:
            self._log(f"[APN] SetProperty failed on {ctx['path']}: {e}", "err")
            return
        ctx["apn"] = apn
        self._log(f"[APN] {ctx['path']} AccessPointName := '{apn}'", "ok")
        self._refresh_apn_tab()

    def _on_provision_context(self, *_args) -> None:
        if not self.modem:
            self._log("[!] No modem selected", "err")
            return
        ctx = self._selected_context()
        if not ctx:
            self._log("[APN] no context selected", "err")
            return
        try:
            self.modem.provision_context(ctx["path"])
        except dbus.DBusException as e:
            self._log(f"[APN] ProvisionContext failed: {e}", "err")
            return
        self._log(f"[APN] ProvisionContext on {ctx['path']} done — "
                  "check MCC/MNC match in serviceproviders.xml", "ok")
        self._refresh_apn_tab()

    def _confirm_run(self, heading: str, body: str, cmd: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("run", "Run")
        dialog.set_response_appearance("run", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.choose(
            self, None,
            lambda dlg, result: self._run_shell(cmd)
            if dlg.choose_finish(result) == "run" else None)

    def _run_shell(self, cmd: str) -> None:
        self._set_apn_status(f"running: {cmd}")
        self._log(f"[sh] $ {cmd}", "tx")

        def worker():
            try:
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=60)
                out = (proc.stdout + proc.stderr).strip()
                rc = proc.returncode
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._log, f"[sh] error: {e}", "err")
                GLib.idle_add(self._apn_status_row.set_subtitle, f"error: {e}")
                return
            if out:
                for line in out.splitlines():
                    GLib.idle_add(self._log, f"[sh] {line}",
                                  "ok" if rc == 0 else "err")
            GLib.idle_add(self._log, f"[sh] exit {rc}",
                          "ok" if rc == 0 else "err")
            GLib.idle_add(self._apn_status_row.set_subtitle,
                          f"last: {cmd} -> exit {rc}")

        threading.Thread(target=worker, daemon=True).start()

    def _build_networks_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Networks")

        group = Adw.PreferencesGroup()
        group.set_title("ofono scripts")
        group.set_description(
            "Runs python scripts from /usr/share/ofono/scripts against the "
            "selected modem (get-operators = scanned networks, like "
            "mmcli --3gpp-scan; a fresh scan needs Mode manual first)")
        self._script_list = Gtk.StringList()
        self._script_combo = Adw.ComboRow()
        self._script_combo.set_title("Script")
        self._script_combo.set_model(self._script_list)
        group.add(self._script_combo)

        self._script_args_row = Adw.EntryRow()
        self._script_args_row.set_title("Arguments")
        group.add(self._script_args_row)

        run_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        run_box.set_margin_top(8)
        run_box.set_margin_bottom(8)
        scan_btn = Gtk.Button(label="Scan")
        scan_btn.add_css_class("suggested-action")
        scan_btn.set_tooltip_text(
            "Mode manual (COPS=2) -> poll get-operators -> Mode auto")
        scan_btn.connect("clicked", lambda *_: self._on_network_scan())
        run_box.append(scan_btn)
        run_btn = Gtk.Button(label="Run")
        run_btn.connect("clicked", lambda *_: self._on_run_script())
        run_box.append(run_btn)
        row = Adw.ActionRow()
        row.set_activatable_widget(run_box)
        row.add_suffix(run_box)
        group.add(row)

        self._script_status_row = Adw.ActionRow()
        self._script_status_row.set_title("Status")
        self._script_status_row.set_subtitle("idle")
        group.add(self._script_status_row)
        page.add(group)

        self._script_out = Gtk.TextView(
            editable=False, monospace=True, cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._script_out.add_css_class("monospace")
        out_scrolled = Gtk.ScrolledWindow()
        out_scrolled.set_child(self._script_out)
        out_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC,
                                Gtk.PolicyType.AUTOMATIC)
        out_scrolled.set_vexpand(True)
        out_scrolled.set_size_request(-1, 300)
        out_group = Adw.PreferencesGroup()
        out_group.set_title("Output")
        out_group.add(out_scrolled)
        page.add(out_group)

        self._load_scripts()
        return page

    OFONO_SCRIPTS_DIR = "/usr/share/ofono/scripts"
    # scan: mode manual -> poll get-operators -> mode auto
    _scan_thread: threading.Thread | None = None
    _scan_cancel = False

    def _load_scripts(self) -> None:
        try:
            names = sorted(
                n for n in os.listdir(self.OFONO_SCRIPTS_DIR)
                if os.path.isfile(os.path.join(self.OFONO_SCRIPTS_DIR, n))
                and not n.endswith((".pyc", ".conf")) and "test-" not in n)
        except OSError as e:
            self._set_script_status(f"cannot list scripts: {e}")
            return
        self._script_list.splice(0, self._script_list.get_n_items(), names)
        if "get-operators" in names:
            self._script_combo.set_selected(names.index("get-operators"))

    def _set_script_status(self, text: str) -> None:
        GLib.idle_add(self._script_status_row.set_subtitle, text)

    def _set_script_output(self, text: str) -> None:
        def apply():
            self._script_out.get_buffer().set_text(text, -1)
        GLib.idle_add(apply)

    def _run_ofono_script(self, name: str, args: str):
        path = os.path.join(self.OFONO_SCRIPTS_DIR, name)
        # ofono script convention: argv[1] = modem path, rest = script args
        argv = ["python3", path]
        if self.modem:
            argv.append(self.modem.path)
        argv += shlex.split(args) if args else []
        self._set_script_status(f"running: {name} {args}".strip())
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=120)
        except Exception as e:  # noqa: BLE001
            self._set_script_status(f"error: {e}")
            self._set_script_output(str(e))
            return None
        out = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        self._set_script_status(f"{name} -> exit {proc.returncode}")
        self._set_script_output(out or "(no output)")
        return proc

    def _on_run_script(self) -> None:
        item = self._script_combo.get_selected_item()
        if item is None:
            self._set_script_status("no script selected")
            return

        def worker():
            self._run_ofono_script(
                item.get_string(), self._script_args_row.get_text().strip())
        threading.Thread(target=worker, daemon=True).start()

    def _on_network_scan(self) -> None:
        if not self.modem:
            self._set_script_status("no modem selected")
            return
        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_cancel = True
            self._set_script_status("scan stopped")
            return
        self._scan_cancel = False
        self._set_script_output("Mode manual — scanning...")

        def worker():
            m = self.modem
            try:
                m.command("AT+COPS=2", timeout=15)
                seen = ""
                for _ in range(12):
                    if self._scan_cancel:
                        break
                    time.sleep(5)
                    proc = self._run_ofono_script("get-operators", "")
                    out = (proc.stdout or "") if proc else ""
                    if len(out) > len(seen):
                        seen = out
                        GLib.idle_add(self._set_script_output, out.strip())
                if not seen:
                    GLib.idle_add(self._set_script_output,
                                  "no operators found (SIM forbidden or no "
                                  "coverage) — cached list may also be empty")
            except Exception as e:  # noqa: BLE001
                self._set_script_status(f"scan failed: {e}")
            finally:
                try:
                    m.command("AT+COPS=0", timeout=15)
                except Exception:  # noqa: BLE001
                    pass
        self._scan_thread = threading.Thread(target=worker, daemon=True)
        self._scan_thread.start()

    def _build_mmcli_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("mmcli")

        group = Adw.PreferencesGroup()
        group.set_title("mmcli commands")
        group.set_description(
            "Runs mmcli against '-m 0'. --3gpp-scan needs Mode manual and "
            "can take ~60 s; the modem is left in auto afterwards")
        self._mm_preset_list = Gtk.StringList()
        self._mm_combo = Adw.ComboRow()
        self._mm_combo.set_title("Preset")
        self._mm_combo.set_model(self._mm_preset_list)
        for label, args in MMCLI_PRESETS:
            self._mm_preset_list.append(f"{label}  ({args})")
        self._mm_combo.set_selected(0)
        self._mm_combo.connect("notify::selected", self._on_mm_preset_changed)
        group.add(self._mm_combo)

        self._mm_args_row = Adw.EntryRow()
        self._mm_args_row.set_title("mmcli args")
        self._mm_args_row.set_text(MMCLI_PRESETS[0][1])
        self._mm_args_row.set_show_apply_button(True)
        self._mm_args_row.connect("apply", self._on_run_mmcli)
        group.add(self._mm_args_row)

        run_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        run_box.set_margin_top(8)
        run_box.set_margin_bottom(8)
        run_btn = Gtk.Button(label="Run")
        run_btn.add_css_class("suggested-action")
        run_btn.connect("clicked", self._on_run_mmcli)
        run_box.append(run_btn)
        self._mm_cancel_btn = Gtk.Button(label="Cancel")
        self._mm_cancel_btn.set_visible(False)
        self._mm_cancel_btn.connect("clicked", self._on_cancel_mmcli)
        run_box.append(self._mm_cancel_btn)
        row = Adw.ActionRow()
        row.set_activatable_widget(run_box)
        row.add_suffix(run_box)
        group.add(row)

        self._mm_status_row = Adw.ActionRow()
        self._mm_status_row.set_title("Status")
        self._mm_status_row.set_subtitle("idle")
        group.add(self._mm_status_row)
        page.add(group)

        self._mm_out = Gtk.TextView(editable=False, monospace=True,
                                    cursor_visible=False,
                                    wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._mm_out.add_css_class("monospace")
        out_scrolled = Gtk.ScrolledWindow()
        out_scrolled.set_child(self._mm_out)
        out_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        out_scrolled.set_vexpand(True)
        out_scrolled.set_size_request(-1, 300)
        out_group = Adw.PreferencesGroup()
        out_group.set_title("Output")
        out_group.add(out_scrolled)
        page.add(out_group)
        return page

    _mm_proc: subprocess.Popen | None = None

    def _on_mm_preset_changed(self, *_args) -> None:
        item = self._mm_combo.get_selected_item()
        if item is None:
            return
        args = MMCLI_PRESETS[self._mm_combo.get_selected()][1]
        self._mm_args_row.set_text(args)

    def _set_mm_status(self, text: str) -> None:
        GLib.idle_add(self._mm_status_row.set_subtitle, text)

    def _set_mm_output(self, text: str) -> None:
        def apply():
            self._mm_out.get_buffer().set_text(text, -1)
        GLib.idle_add(apply)

    def _on_cancel_mmcli(self, *_args) -> None:
        if self._mm_proc and self._mm_proc.poll() is None:
            self._mm_proc.kill()
            self._set_mm_status("cancelled")

    def _on_run_mmcli(self, *_args) -> None:
        if self._mm_proc and self._mm_proc.poll() is None:
            self._set_mm_status("already running — cancel first")
            return
        args = shlex.split(self._mm_args_row.get_text().strip())
        if not args:
            self._set_mm_status("no arguments")
            return
        self._set_mm_output("$ mmcli " + " ".join(args))
        self._set_mm_status("running…")
        self._mm_cancel_btn.set_visible(True)

        def worker():
            try:
                proc = subprocess.Popen(["mmcli"] + args,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                self._mm_proc = proc
                out = proc.communicate(timeout=120)[0].strip()
                rc = proc.returncode
            except Exception as e:  # noqa: BLE001
                self._set_mm_status(f"error: {e}")
                self._set_mm_output(str(e))
                GLib.idle_add(self._mm_cancel_btn.set_visible, False)
                return
            finally:
                self._mm_proc = None
            self._set_mm_status(f"mmcli {' '.join(args)} -> exit {rc}")
            self._set_mm_output(out or "(no output)")
            GLib.idle_add(self._mm_cancel_btn.set_visible, False)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # logcat tab (radio buffer)
    # ------------------------------------------------------------------

    def _build_logcat_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("logcat")

        self._lc_ring = _LogcatRing(LOGCAT_RING_MAX_BYTES)
        self._lc_proc: subprocess.Popen | None = None
        self._lc_stop = threading.Event()
        self._lc_regex: re.Pattern | None = None
        self._lc_dirty = False
        self._lc_autoscroll = True
        self._lc_suppress_scroll_flag = False
        self._lc_last_filter = None
        self._lc_seen_head: int | None = None
        self._lc_seen_total = 0
        self._lc_appended_total: int | None = None

        ctrl_group = Adw.PreferencesGroup()
        ctrl_group.set_title("logcat -b radio")

        self._lc_toggle = Adw.SwitchRow()
        self._lc_toggle.set_title("Capture")
        self._lc_toggle.set_subtitle("stopped")
        self._lc_toggle.set_tooltip_text(
            "Runs 'sudo logcat -b radio' continuously while enabled")
        self._lc_toggle.connect("notify::active", self._on_lc_toggle)
        ctrl_group.add(self._lc_toggle)

        filter_row = Adw.ActionRow()
        filter_row.set_title("Filter")
        filter_row.set_tooltip_text(
            "Toggle terms; equivalent to egrep 'A|B|C' — none active = show all")
        fbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._lc_filter_btns: dict[str, Gtk.ToggleButton] = {}
        for term in LOGCAT_FILTER_TERMS:
            tb = Gtk.ToggleButton(label=term)
            tb.add_css_class("caption")
            tb.connect("toggled", self._on_lc_filter_toggled)
            fbox.append(tb)
            self._lc_filter_btns[term] = tb
        filter_row.add_suffix(fbox)
        filter_row.set_activatable_widget(fbox)
        ctrl_group.add(filter_row)

        self._lc_ctx_row = Adw.SpinRow.new_with_range(0, 99, 1)
        self._lc_ctx_row.set_title("Context lines (-C)")
        self._lc_ctx_row.set_value(1)
        self._lc_ctx_row.set_numeric(True)
        self._lc_ctx_row.get_adjustment().connect(
            "value-changed", self._on_lc_filter_toggled)
        ctrl_group.add(self._lc_ctx_row)

        btn_row = Adw.ActionRow()
        bb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save_btn = Gtk.Button(label="Save log")
        save_btn.add_css_class("suggested-action")
        save_btn.set_tooltip_text(
            "Save the visible (filtered) buffer to ~/logcat-radio-<date>.log")
        save_btn.connect("clicked", self._on_lc_save)
        bb.append(save_btn)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", self._on_lc_clear)
        bb.append(clear_btn)
        btn_row.add_suffix(bb)
        btn_row.set_activatable_widget(bb)
        ctrl_group.add(btn_row)
        page.add(ctrl_group)

        out_group = Adw.PreferencesGroup()
        out_group.set_title("Radio buffer (last 256 kB)")
        self._lc_view = Gtk.TextView(editable=False, monospace=True,
                                     cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._lc_view.add_css_class("monospace")
        self._lc_view.add_css_class("caption")   # small but legible font
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(self._lc_view)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.ALWAYS)
        scrolled.set_vexpand(True)
        scrolled.set_size_request(-1, 320)
        scrolled.get_vadjustment().connect("value-changed",
                                           self._on_lc_scroll_changed)
        out_group.add(scrolled)
        page.add(out_group)

        self._lc_render_tag = GLib.timeout_add(
            LOGCAT_RENDER_INTERVAL_MS, self._lc_render_tick)
        return page

    def _on_lc_scroll_changed(self, adj: Gtk.Adjustment) -> None:
        # ignore the value-changed events our own scroll-to-bottom causes
        if self._lc_suppress_scroll_flag:
            self._lc_suppress_scroll_flag = False
            return
        # remember manual position only while capture is off; with capture
        # enabled the view is always pinned to the newest line
        if not self._lc_toggle.get_active():
            self._lc_autoscroll = (adj.get_value()
                                   >= adj.get_upper() - adj.get_page_size() - 4)

    def _set_lc_status(self, text: str) -> None:
        GLib.idle_add(self._lc_toggle.set_subtitle, text)

    def _lc_active_terms(self) -> list[str]:
        return [t for t, b in self._lc_filter_btns.items() if b.get_active()]

    def _on_lc_filter_toggled(self, *_args) -> None:
        terms = self._lc_active_terms()
        n = int(self._lc_ctx_row.get_value())
        if terms:
            self._lc_regex = re.compile(
                "|".join(re.escape(t) for t in terms))
            desc = f"filter: {'|'.join(terms)}  -C{n}"
        else:
            self._lc_regex = None
            desc = ""
        self._lc_last_filter = None  # force rerender
        GLib.idle_add(self._lc_render_tick)
        if self._lc_proc and self._lc_proc.poll() is None:
            self._set_lc_status(f"running — {desc}" if desc else "running")

    def _on_lc_toggle(self, *_args) -> None:
        if self._lc_toggle.get_active():
            self._lc_autoscroll = True
            self._lc_stop.clear()
            t = threading.Thread(target=self._lc_worker, daemon=True)
            t.start()
        else:
            self._lc_stop.set()
            proc = self._lc_proc
            if proc and proc.poll() is None:
                # sudo forwards TERM to the wrapper, but the inner
                # halium-lxc-exec/logcat can linger — sweep by name too
                try:
                    subprocess.run(["sudo", "-n", "pkill", "-f",
                                    "[l]ogcat -b radio"], timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                proc.terminate()
            self._set_lc_status("stopped")

    def _lc_worker(self) -> None:
        def pump(stream):
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if self._lc_stop.is_set():
                    break
                self._lc_ring.append(line)
                self._lc_dirty = True
            stream.close()

        try:
            self._set_lc_status("starting sudo logcat -b radio…")
            proc = subprocess.Popen(
                ["sudo", "-n", LOGCAT_BIN, "-b", "radio"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
            self._lc_proc = proc
            GLib.idle_add(self._on_lc_filter_toggled)  # read widgets on UI thread
            t = threading.Thread(target=pump, args=(proc.stdout,),
                                 daemon=True)
            t.start()
            rc = proc.wait()
            t.join(timeout=2)
            if not self._lc_stop.is_set():
                self._set_lc_status(f"logcat exited (code {rc})")
                GLib.idle_add(self._lc_toggle.set_active, False)
        except Exception as e:  # noqa: BLE001
            self._set_lc_status(f"error: {e}")
            GLib.idle_add(self._lc_toggle.set_active, False)
        finally:
            self._lc_proc = None

    def _lc_render_tick(self) -> bool:
        if not self._lc_dirty:
            return True
        self._lc_dirty = False
        regex = self._lc_regex
        ctx = int(self._lc_ctx_row.get_value())
        key = (regex.pattern if regex else None, ctx)
        head_gen, total_gen, lines = self._lc_ring.snapshot()
        old_head = self._lc_seen_head
        reset = (key != self._lc_last_filter or old_head is None
                 or total_gen < self._lc_seen_total)
        self._lc_seen_head = head_gen
        self._lc_seen_total = total_gen
        if reset or regex is not None:
            # full rerender: filter changed, buffer cleared/restarted, or a
            # regex is active (context lines can join old and new lines;
            # rerendering <=256 kB every tick is cheap)
            self._lc_last_filter = key
            self._lc_appended_total = total_gen
            flags = _grep_context_flags(lines, regex, ctx)
            GLib.idle_add(self._lc_set_text,
                          "\n".join(l for l, s in zip(lines, flags) if s))
            return True
        # no filter: trim evicted lines off the top, append the new ones
        if old_head is not None and head_gen > old_head:
            GLib.idle_add(self._lc_delete_head, head_gen - old_head)
        new_count = total_gen - (self._lc_appended_total or 0)
        self._lc_appended_total = total_gen
        if new_count > 0:
            GLib.idle_add(self._lc_append, "\n".join(lines[-new_count:]))
        return True

    def _lc_delete_head(self, n: int) -> None:
        buf = self._lc_view.get_buffer()
        n = min(n, buf.get_line_count() - 1)
        if n <= 0:
            return
        ok, it = buf.get_iter_at_line(n)
        if not ok:
            return
        buf.delete(buf.get_start_iter(), it)

    def _lc_set_text(self, text: str) -> None:
        self._lc_last_filter = (
            self._lc_regex.pattern if self._lc_regex else None,
            int(self._lc_ctx_row.get_value()))
        buf = self._lc_view.get_buffer()
        buf.set_text(text, -1)
        self._lc_scroll_bottom()

    def _lc_append(self, text: str) -> None:
        buf = self._lc_view.get_buffer()
        if not self._lc_last_filter:
            self._lc_set_text(text)
            return
        if buf.get_line_count() > 0:
            buf.insert(buf.get_end_iter(), "\n", -1)
        buf.insert(buf.get_end_iter(), text, -1)
        self._lc_scroll_bottom()

    def _lc_scroll_bottom(self) -> None:
        # while capture is enabled the view is always pinned to the newest
        # line; when stopped, respect a manual scroll-up position
        if not (self._lc_autoscroll or self._lc_toggle.get_active()):
            return
        buf = self._lc_view.get_buffer()
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self._lc_suppress_scroll_flag = True
        self._lc_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        buf.delete_mark(mark)

    def _on_lc_clear(self, *_args) -> None:
        self._lc_ring.clear()
        self._lc_dirty = True
        self._lc_last_filter = None
        GLib.idle_add(self._lc_render_tick)

    def _on_lc_save(self, *_args) -> None:
        buf = self._lc_view.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, False)
        name = f"logcat-radio-{datetime.now():%Y%m%d-%H%M%S}.log"
        path = os.path.join(os.path.expanduser("~"), name)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            size = os.path.getsize(path)
            self._set_lc_status(f"saved {path} ({size} bytes)")
        except OSError as e:
            self._set_lc_status(f"save failed: {e}")

    def stop_logcat(self) -> None:
        """Stop capture + render timer (called on window destroy)."""
        if getattr(self, "_lc_render_tag", None):
            GLib.source_remove(self._lc_render_tag)
            self._lc_render_tag = None
        if getattr(self, "_lc_toggle", None) and self._lc_toggle.get_active():
            self._lc_toggle.set_active(False)

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
            ("ERAT=19", "Enable 4G+5G / 5G SA", "AT+ERAT=19"),
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
            self._refresh_apn_tab()
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
