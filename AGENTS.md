# AGENTS.md — 5G AT Tool context

Project context for AI agents (or the user) to pick up work later.

## What this is

Single-file PyGTK (PyGObject/GTK3) app that talks to a 5G modem through
oFono's FuriLabs AT passthrough plugin:

    busctl call org.ofono /ril_0 org.ofono.FuriLabs.AT SendCommand s "AT+ECSQ"

Target device: a FuriLabs phone running phosh (FuriOS). The user
deploys by `scp`-ing the files to `furios@<phone>`.

## File layout

```
5g-at-tool/
├── 5g-at-tool.py          # the whole app, ~1046 lines
├── 5g-at-tool.desktop     # phosh launcher entry
├── icons/5g-at-tool.svg   # app icon
├── README.md              # user-facing docs
├── AGENTS.md              # this file (agent/handoff context)
└── .gitignore
```

`131_DCC2283911_..._AT_Cmd_Modem.pdf` is the Quectel AT command reference
PDF — kept locally for reference, gitignored.

## Phone deployment

- Host: `furios@10.205.52.43` (ssh, ed25519)
- User home: `/home/furios/`
- Python: `/usr/bin/python3` (3.13)
- Display: phosh on Wayland (`WAYLAND_DISPLAY=wayland-0`)

Files deployed on the phone:

| Local                                   | Phone                                                                |
|-----------------------------------------|----------------------------------------------------------------------|
| `5g-at-tool.py`                         | `/home/furios/5g-at-tool.py`                                         |
| `5g-at-tool.desktop`                    | `/home/furios/.local/share/applications/5g-at-tool.desktop`          |
| `icons/5g-at-tool.svg`                  | `/home/furios/.local/share/icons/hicolor/scalable/apps/5g-at-tool.svg` |

After deploying the icon or desktop file, refresh the caches on the phone:

```sh
ssh furios@10.205.52.43 'gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor/ && update-desktop-database ~/.local/share/applications/'
```

The `.desktop` uses `Icon=5g-at-tool` (name, not path) so the hicolor theme
resolves it. `StartupWMClass=es.n0p.fiveg_at_tool` matches the GTK
`application_id` so the window is grouped correctly.

## Desktop file conventions used

```ini
Exec=python3 /home/furios/5g-at-tool.py
Icon=5g-at-tool
Categories=Network;TelephonyTools;Utility;
StartupNotify=true
StartupWMClass=es.n0p.fiveg_at_tool
```

`TelephonyTools` (not `Telephony`) is the freedesktop category used by
`modem-manager-gui.desktop` on the phone.

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

## AT command presets (in `_build_ui`)

`ATI`, `AT+COPS?`, `AT+ECSQ`, `AT+CESQ`, `AT+QENG="servingcell"`,
`AT+QCAINFO`, `AT+QCFG="nr5g"?`, `AT+QCFG="band"?`, `AT+CGSN`, `AT+CIMI`,
`AT+CGREG?`.

`AT+QENG="servingcell"` is parsed by `parse_qeng_servingcell()` for
RSRP/RSRQ/SINR/band/ARFCN/PCI/MCC/MNC/cell ID. `AT+ECSQ` is parsed by
`parse_ecsq()`.

## Known issues / non-fatal warnings

- `Gtk-CRITICAL gtk_box_pack: assertion '_gtk_widget_get_parent (child) ==
  NULL' failed` at startup — three widgets are packed into multiple
  parents (likely the preset buttons being reused in the toolbar and
  history pane). Non-fatal; window still renders. Worth fixing by
  detaching the widget from its old parent with `Gtk.container.remove`
  before re-packing, or by giving each section its own button instance.
- `desktop-file-validate` warns about `Categories=Network;TelephonyTools;Utility;`
  having more than one main category. `modem-manager-gui.desktop` ships
  the same way; leaving as-is.
- `firefox-esr.desktop` in `~/.local/share/applications/` is malformed
  on the phone (not ours — leave alone).

## Smoke tests on the phone

```sh
ssh furios@10.205.52.43 'python3 -c "import py_compile; py_compile.compile(\"/home/furios/5g-at-tool.py\", doraise=True)"'
ssh furios@10.205.52.43 'WAYLAND_DISPLAY=wayland-0 timeout 3 python3 /home/furios/5g-at-tool.py 2>&1 | head'
```

Icon resolution check:

```sh
ssh furios@10.205.52.43 'python3 -c "import gi; gi.require_version(\"Gtk\",\"3.0\"); from gi.repository import Gtk; t=Gtk.IconTheme.get_default(); print(t.lookup_icon(\"5g-at-tool\", 128, 0).get_filename())"'
```

## TODO / next steps

- [ ] Fix the `gtk_box_pack` warnings (detach buttons before re-pack).
- [ ] Add a "favorites" or pinned entry in phosh so the icon appears in
      the quick-launch strip (not just the app drawer). phosh reads
      `~/.local/share/applications/` for `.desktop` files and the pinned
      list lives in `org.gnome.desktop.app-folders` gschema — needs
      `gsettings` write.
- [ ] Add a status bar / tray indicator showing modem online state without
      opening the full window.
- [ ] Persist the last-used modem path across runs (currently picks the
      first one oFono returns).
- [ ] Save AT command history to `~/.local/share/5g-at-tool/history`.
- [ ] Consider packaging as a flatpak or a `.deb` once stable.
- [ ] The `Exec=python3 /home/furios/5g-at-tool.py` path is hardcoded;
      consider `Exec=python3 %U 5g-at-tool.py` with `Path=` or a wrapper
      script in `~/.local/bin/` so the install path isn't pinned.