# AGENTS.md — FuriModem Tool context

Project context for AI agents (or the user) to pick up work later.

## What this is

Multi-module Adw/GTK4 app that talks to a 5G modem through
oFono's FuriLabs AT passthrough plugin:

    busctl call org.ofono /ril_0 org.ofono.FuriLabs.AT SendCommand s "AT+ECSQ"

Target device: a FuriLabs phone running phosh (FuriOS). The user
deploys by `scp`-ing the files to `furios@<phone>`.

## File layout

```
furimodem-tool/
├── furimodem-tool.py       # entry point (Adw.Application)
├── ui.py                   # MainWindow + App + all UI (~1318 lines)
├── ofono.py                # OfonoModem, OfonoMonitor, DBus setup (~187 lines)
├── parsers.py              # AT response parsers (parse_cesq, parse_cereg,
│                           #   parse_cops, parse_ecell, parse_ecellmeas,
│                           #   parse_eimsgeo) (~223 lines)
├── furimodem-tool.desktop  # phosh launcher entry
├── icons/furimodem-tool.svg # app icon
├── README.md               # user-facing docs
├── AGENTS.md               # this file (agent/handoff context)
└── .gitignore
```

`131_DCC2283911_..._AT_Cmd_Modem.pdf` is the Quectel AT command reference
PDF — kept locally for reference, gitignored.

## Phone deployment

Two known networks. Pick whichever is reachable; both deploy to the same
path on the phone.

| Nickname | Host                | Notes                                |
|----------|---------------------|--------------------------------------|
| `casa`   | `furios@10.1.2.151` | Home Wi-Fi                           |
| `t3`     | `furios@10.205.52.43` | T3 mobile hotspot (stale)           |
| `cur`    | `furios@10.15.19.82` | Current access point (2026-10-04)   |

- User home: `/home/furios/`
- Python: `/usr/bin/python3` (3.13)
- Display: phosh on Wayland (`WAYLAND_DISPLAY=wayland-0`)

Files deployed on the phone:

| Local                                   | Phone                                                                |
|-----------------------------------------|----------------------------------------------------------------------|
| `furimodem-tool.py`                     | `/home/furios/5g-at-tool/furimodem-tool.py`                          |
| `ui.py`                                 | `/home/furios/5g-at-tool/ui.py`                                      |
| `ofono.py`                              | `/home/furios/5g-at-tool/ofono.py`                                   |
| `parsers.py`                            | `/home/furios/5g-at-tool/parsers.py`                                 |
| `furimodem-tool.desktop`                | `/home/furios/.local/share/applications/furimodem-tool.desktop`      |
| `icons/furimodem-tool.svg`              | `/home/furios/.local/share/icons/hicolor/scalable/apps/furimodem-tool.svg` |

After deploying the icon or desktop file, refresh the caches on the phone:

```sh
ssh furios@<host> 'gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor/ && update-desktop-database ~/.local/share/applications/'
```

The `.desktop` uses `Icon=furimodem-tool` (name, not path) so the hicolor theme
resolves it. `StartupWMClass=es.n0p.furimodem_tool` matches the GTK
`application_id` so the window is grouped correctly.

## UI Layout (Adw.OverlaySplitView + Adw.ViewStack / ViewSwitcherBar)

- **Sidebar**: modem selector, log, history (AT command history rows).
- **Tab 1 — Status**: modem info (Manufacturer, Model, Revision, IMEI, IMSI, Online, Powered, Type, Operator, Registration, Tech, Strength) + 5G signal rows (RAT, MCC/MNC, TAC, Cell ID, RSRP, RSRQ, RSSI, SINR, RXLEV, RSCP, ECNO) fed by polling `AT+CESQ` + `AT+CEREG?` every 2s (`POLL_INTERVAL_S`). Serving/neighbour cell blocks parsed from `AT+ECELLMEAS=1` / `AT+ECELL`.
- **Tab 2 — APN**: current SIM block (Operator, MCC/MNC, IMSI, ICCID of the inserted card), PDP context selector + editable APN EntryRow with Refresh / Apply / from XML buttons (Apply → `ConnectionContext.SetProperty("AccessPointName")`; from XML → `ConnectionContext.ProvisionContext` which re-applies the serviceproviders.xml entry matching the SIM MCC/MNC), and a Services block with confirm-guarded "Restart oFono" (`setprop vendor.ril.mtk.restart 1`, no root needed) and "Restart ModemManager" (`sudo -n systemctl restart ModemManager.service`). Keep context-button labels short — long labels overflow the phone screen.
- **Tab 3 — Networks**: runner for the python scripts in `/usr/share/ofono/scripts` (`OFONO_SCRIPTS_DIR`) against the selected modem — script ComboRow (auto-listed, default `get-operators`) + free Arguments EntryRow + Run button. `argv[1]` is always the modem path (ofono script convention). "Scan" button runs the operator-scan flow in a worker thread: `AT+COPS=2` (manual) → poll `get-operators` ×12 every 5s → `AT+COPS=0` in `finally`; clicking Scan again cancels. Output goes to a read-only TextView.
- **Tab 4 — mmcli**: runs `mmcli` with preset args (`MMCLI_PRESETS` in ui.py: 3GPP scan, Status, `-L`, Signal quality, Simple status, Location) or free args, Cancel button, output TextView. Always pin `-m 0` — `-m any` crashes libmm-glib with the ofono2mm backend.
- **Tab 5 — Terminal**: AT command input (EntryRow + timeout SpinRow + Send button + Clear log button) + compact AT preset FlowBox (label + description, 2 lines per button) + log TextView + history rows. Destructive presets (`AT+EPOF`, `AT+EPON`, `AT+ESLP=1`) are confirm-guarded.
- **Tab 6 — logcat**: streams `sudo -n /usr/sbin/logcat -b radio` (halium-lxc wrapper, root-only; furios has NOPASSWD sudo). Capture SwitchRow gates the stream; ToggleButtons PDN/CME/RMC/IMS + SpinRow 0–99 context lines reproduce `egrep 'A|B|C' -C##` (applied in-app via `_grep_context_flags` over a 256 kB `_LogcatRing`, re-filtered per 250 ms tick, so toggling never restarts the capture). Save log → `~/logcat-radio-<ts>.log` (visible/filtered text), Clear empties the ring. Small font via `caption` CSS class; autoscroll re-engages only when pinned to the bottom. `stop_logcat()` on window destroy pkills leftover `logcat -b radio` and removes the render timer.
- **Tab "Positioning"**: removed (see commit "Drop positioning tab"); ELOCAEN / cell-location commands live as Terminal presets.
- **HeaderBar**: title + modem-path subtitle, refresh modem button, sidebar toggle. Bottom `Adw.ViewSwitcherBar` for tab switching.

## oFono interfaces used

| Interface                            | Purpose                                        |
|--------------------------------------|------------------------------------------------|
| `org.ofono.Manager`                  | Enumerate modems via `GetModems()` + add/remove signals (`OfonoMonitor`) |
| `org.ofono.Modem`                    | Manufacturer, Model, Revision, Serial, Type, Online, Powered |
| `org.ofono.NetworkRegistration`      | Name, Code, Status, Technology, CellId, MCC/MNC, Strength |
| `org.ofono.SimManager`               | IMSI (`SubscriberIdentity`), ICCID (`CardIdentifier`) |
| `org.ofono.FuriLabs.AT`              | `SendCommand(s) -> s` raw AT passthrough       |
| `org.ofono.ConnectionManager`        | `GetContexts() -> a{oa{sv}}` PDP context enumeration |
| `org.ofono.ConnectionContext`        | `SetProperty(sv)` APN edit, `ProvisionContext()` XML re-apply |
| `org.freedesktop.DBus.Properties`    | Property reads (`GetAll`) + change notifications |

Property reads go through each interface's native `GetProperties`/`GetAll`
(`ofono.py:OfonoModem.get_all`). Missing fields fall back to AT commands
(`AT+CGMI`, `AT+CGMM`, `AT+CGSN`, `AT+CIMI`) cached in `_at_cache`.

Verify with:

```sh
busctl introspect org.ofono /ril_0 org.ofono.FuriLabs.AT
```

## AT command presets (in `_build_terminal_page`)

Compact FlowBox of `(label, description, command)` tuples:
`ATI`, `AT+CGMI`, `AT+CGMM`, `AT+CGMR`, `AT+CGSN`, `AT+CIMI`,
`AT+COPS?`, `AT+CESQ`, `AT+ECSQ`, `AT+CREG?`, `AT+CEREG?`,
`AT+C5GREG?`, `AT+GCAP`, `AT+ECID`, `AT+CGATT?`, `AT+CPIN?`,
`AT+EXOPL` (full op scan), `AT+EPRATL?`, `AT+E5GOPT?`, `AT+ERAT?`,
`AT+ERAT=19` (enable 4G+5G / 5G SA),
`AT+ECAINFO?` (carrier agg), `AT+ENRCABAND?`, `AT+ECCAUSE?` (reject
cause), `AT+EONS?`, `AT+ELCE?`, `AT+ECELCK?`, `AT+EPOF` (power off!),
`AT+ESLP?`. Commands in `DISRUPTIVE` (`AT+EPOF`, `AT+EPON`,
`AT+ESLP=1`) require confirmation. The preset set uses the E-prefixed
commands (`ECSQ`, `ECELL`, `EPOF`, …) the FuriLabs RIL answers — the
Quectel `QENG`/`QCAINFO` forms from the reference PDF were dropped from
the presets/parsers (see commits "Serving + neighbour cells, ECELL RSRP
fix, new AT presets" onward).

Parsers in `parsers.py`: `parse_cesq` (signal poll), `parse_cereg`
(registration/TAC), `parse_cops`, `parse_ecellmeas` + `parse_ecell`
(serving + neighbour cells: RSRP/RSRQ/SINR/PCI/Band/ARFCN),
`parse_eimsgeo`, `strip_at_response`.

## Known issues / non-fatal warnings

- `desktop-file-validate` warns about `Categories=Network;TelephonyTools;Utility;`
  having more than one main category. `modem-manager-gui.desktop` ships
  the same way; leaving as-is.
- `firefox-esr.desktop` in `~/.local/share/applications/` is malformed
  on the phone (not ours — leave alone).

## Smoke tests on the phone

```sh
ssh furios@<host> 'python3 -c "import py_compile; py_compile.compile(\"/home/furios/5g-at-tool/ui.py\", doraise=True)"'
ssh furios@<host> 'WAYLAND_DISPLAY=wayland-0 timeout 3 python3 /home/furios/5g-at-tool/furimodem-tool.py 2>&1 | head'
```

Icon resolution check:

```sh
ssh furios@<host> 'python3 -c "import gi; gi.require_version(\"Gtk\",\"3.0\"); from gi.repository import Gtk; t=Gtk.IconTheme.get_default(); print(t.lookup_icon(\"furimodem-tool\", 128, 0).get_filename())"'
```

Run app and take screenshot (use the current host; `shotman` panics —
see Round 4 notes for the working gdbus screenshot command):

```sh
ssh furios@10.15.19.82 'setsid XDG_RUNTIME_DIR=/run/user/32011 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/32011/bus WAYLAND_DISPLAY=wayland-0 python3 /home/furios/5g-at-tool/furimodem-tool.py &'
```

## Bugs fixed

### Round 5 (Networks + mmcli tabs)
- `mmcli -m any` crashes libmm-glib with the ofono2mm backend — always pin
  `-m 0` (the modem is always `/Modem/0`). Comment at `MMCLI_PRESETS`.
- Long mmcli/scan commands run in daemon threads (`subprocess.Popen` +
  `communicate(timeout=120)`) with a Cancel button; UI updates go through
  `GLib.idle_add` — never touch widgets from the worker thread.
- Operator scan must leave the modem in auto: `AT+COPS=0` runs in
  `finally` even if the `get-operators` polling throws. `get-operators`
  returns a cached list — a fresh scan needs `AT+COPS=2` first.
- ofono scripts convention: `argv[1]` = modem D-Bus path, rest = script
  args; run with `python3 /usr/share/ofono/scripts/<name>`.

### Round 4 (APN tab)
- FuriLabs ofono does NOT expose `Manager.GetObjectsAndInterfaces` (returns
  UnknownMethod) — enumerate contexts via
  `org.ofono.ConnectionManager.GetContexts() -> a{oa{sv}}` on the modem path.
- Contexts do NOT accept `org.freedesktop.DBus.Properties.Set` (UnknownMethod)
  — use the context's own `org.ofono.ConnectionContext.SetProperty(sv)`.
- Context APN changes are runtime-only: a RIL restart (`setprop
  vendor.ril.mtk.restart 1`) resets un-persisted APNs back to the built-in
  defaults when no `/var/lib/ofono/<IMSI>/settings` exists — re-Apply or
  Provision after restarting oFono.
- Long button labels overflow the phone-width window; use short labels
  (Refresh / Apply / from XML) + tooltips.
- Screenshot testing on the phone: shotman panics ("requested global not
  found"); use instead:
  `gdbus call --session --dest org.gnome.Shell.Screenshot --object-path
  /org/gnome/Shell/Screenshot --method org.gnome.Shell.Screenshot.Screenshot
  true false /tmp/x.png` with
  `XDG_RUNTIME_DIR=/run/user/32011 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/32011/bus`
  (furios' real uid runtime dir is 32011, NOT 1000 — GUI apps fail with
  "Gtk couldn't be initialized" or dconf permission errors without it).

### Round 1 (initial migration from single-file)
1. Removed unused `import GObject` (ui.py L16).
2. Removed dead `tag.set_foreground(... if False else None)` (ui.py L60).
3. Connected `key-pressed` to inner Gtk.Entry via `get_entry()` (ui.py L229).
4. Moved log from PreferencesGroup to Gtk.Frame inside page (ui.py L255-263).
5. Removed walrus `cmd:="..."` in tuple (ui.py L291).
6. `get_selected()` now checks `Gtk.INVALID_LIST_POSITION` (ui.py L448).
7. `from parsers import parse_ecell` moved to top (ui.py L26-30).
8. Replaced non-existent `get_rows()` with `observe_children()` (ui.py L627-631).
9. `self.log_view` assigned (ui.py L253).
10. `_on_props_changed` connected to bus via `add_signal_receiver` (ui.py L68-75).
11. `_poll_signal` now calls `_refresh_signal_info` (ui.py L671-675).
12. Removed invalid `member_signature=None` from `add_signal_receiver` (ui.py L74).
13. Replaced `set_placeholder_text` with `set_tooltip_text` + `connect("apply")` — EntryRow lacks placeholder method (ui.py L227-229).
14. `Gtk.WrapMode.WRAP_CHAR` → `WORD_CHAR` (ui.py L251).
15. `page.add(log_frame)` → use `Adw.PreferencesGroup(log_group).add(scrolled)` (ui.py L263-265).
16. `Gio.BindingFlags` → `GObject.BindingFlags` (ui.py L128).
17. Escaped `&` in titles (`"Power & sleep"` → `"Power &amp; sleep"`, `"CSG & operator"` → `"CSG &amp; operator"`) (ui.py L372, L386).
18. `insert_with_tags_by_name(end, text, -1, tag)` → `insert_with_tags_by_name(end, msg, tag)` — GTK4 doesn't accept length 3rd arg as int instead of tag name (ui.py L404-408).

### Round 2 (tab restructure)
- Replaced 3rd "Network" tab with "Terminal" tab reusing signal/presets from sidebar.
- Moved modem info + signal rows from sidebar to Tab 1 (Status).
- Moved AT preset buttons from sidebar to Tab 3 (Terminal).
- Sidebar now only has: modem selector combo + log + history.

### Round 3 (rename to FuriModem Tool)
- Renamed project from `5g-at-tool` to `furimodem-tool`.
- Updated `application_id` from `es.n0p.fiveg_at_tool` to `es.n0p.furimodem_tool`.
- Updated all file names, desktop entry, icon, README, AGENTS.md.

## TODO / next steps

- [ ] Fix the `gtk_box_pack` warnings (detach buttons before re-pack).
- [ ] Add a "favorites" or pinned entry in phosh so the icon appears in
      the quick-launch strip (not just the app drawer).
- [ ] Add a status bar / tray indicator showing modem online state without
      opening the full window.
- [ ] Persist the last-used modem path across runs.
- [ ] Save AT command history to `~/.local/share/furimodem-tool/history`.
- [ ] Consider packaging as a flatpak or a `.deb` once stable.
- [ ] Consider a wrapper script in `~/.local/bin/` so the install path isn't pinned.
