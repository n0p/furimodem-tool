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
├── ui.py                   # MainWindow + App + all UI (~632 lines)
├── ofono.py                # OfonoModem, OfonoMonitor, DBus setup
├── parsers.py              # AT response parsers (parse_cesq, parse_cereg, etc.)
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
| `t3`     | `furios@10.205.52.43` | T3 mobile hotspot (current)         |

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

## UI Layout (Adw.OverlaySplitView)

- **Sidebar**: modem selector, log, history (AT command history rows).
- **Tab 1 — Status**: modem info (Manufacturer, Model, Revision, IMEI, IMSI, Online, Powered, Type, Operator, Registration, Tech, Strength) + 5G signal rows (RAT, MCC/MNC, TAC, Cell ID, RSRP, RSRQ, RSSI, SINR, RXLEV, RSCP, ECNO). Also shows refresh button and Operator/Registration from the sidebar.
- **Tab 2 — Positioning**: cell measurement (AT+ECELLMEAS, AT+ECELL) + geolocation (AT+ELOCAEN, AT+EIMSGEO) sections.
- **Tab 3 — Terminal**: AT command input (EntryRow + timeout SpinRow + Send button + Clear log button) + AT preset buttons + log TextView + history rows.
- **HeaderBar**: title, refresh modem button, sidebar toggle.

## oFono interfaces used

| Interface                            | Purpose                                        |
|--------------------------------------|------------------------------------------------|
| `org.ofono.Manager`                  | Enumerate modems via `GetModems()`             |
| `org.ofono.Modem`                    | Manufacturer, Model, Revision, Serial, Online  |
| `org.freedesktop.DBus.Properties`    | Property reads + change notifications          |
| `org.ofono.FuriLabs.AT`              | `SendCommand(s) -> s` raw AT passthrough       |
| `org.ofono.Modem.Network`            | Operator, registration, technology             |
| `org.ofono.Modem.Signal`             | Signal %, BER, RAT                             |
| `org.ofono.Modem.SimManager`         | IMEI (from SIM serial)                         |

Verify with:

```sh
busctl introspect org.ofono /ril_0 org.ofono.FuriLabs.AT
```

## AT command presets (in `_build_terminal_page`)

`ATI`, `AT+CGMI`, `AT+CGMM`, `AT+CGMR`, `AT+CGSN`, `AT+CIMI`,
`AT+COPS?`, `AT+CESQ`, `AT+ECSQ`, `AT+CREG?`, `AT+CEREG?`,
`AT+C5GREG?`, `AT+GCAP`, `AT+QENG="servingcell"`, `AT+QCAINFO`,
`AT+ECID`, `AT+CGATT?`, `AT+CPIN?`.

`AT+QENG="servingcell"` is parsed by `parse_qeng_servingcell()` for
RSRP/RSRQ/SINR/band/ARFCN/PCI/MCC/MNC/cell ID. `AT+ECSQ` is parsed by
`parse_ecsq()`.

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

Run app and take screenshot:

```sh
ssh furios@10.205.52.43 'setsid WAYLAND_DISPLAY=wayland-0 python3 /home/furios/5g-at-tool/furimodem-tool.py &' && sleep 3 && ssh furios@10.205.52.43 'shotman -c output' && scp furios@10.205.52.43:~/Pictures/*.png . && ssh furios@10.205.52.43 'pkill -9 python3; rm ~/Pictures/*.png'
```

## Bugs fixed

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
