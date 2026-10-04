# FuriModem Tool (oFono)

GTK4/libadwaita app that talks to the modem in a FuriLabs phone (FuriOS,
phosh) through oFono — raw AT via the FuriLabs passthrough plugin plus
native oFono interfaces:

    busctl call org.ofono /ril_0 org.ofono.FuriLabs.AT SendCommand s "AT+ECSQ"

## Tabs

- **Status** — modem/SIM info and live signal (RSRP/RSRQ/SINR/…) polled
  via `AT+CESQ` + `AT+CEREG?`, serving + neighbour cells via
  `AT+ECELLMEAS=1` / `AT+ECELL`.
- **APN** — per-context APN edit (`ConnectionContext.SetProperty`),
  re-provision from `serviceproviders.xml` (`ProvisionContext`), and
  confirm-guarded restarts of oFono / ModemManager.
- **Networks** — run any script from `/usr/share/ofono/scripts` against
  the selected modem; one-click operator scan (manual mode →
  `get-operators` polling → auto mode).
- **mmcli** — preset/free `mmcli` commands with output pane (pinned to
  `-m 0`; `-m any` crashes the ofono2mm backend).
- **Terminal** — free AT input with per-command timeout, color-coded
  tx/rx log, history (Up/Down + click-to-rerun), and compact preset
  buttons (destructive ones need confirmation).

Sidebar: modem selector, log, command history. Modem hot-plug and
property changes update the UI live (`ModemAdded/Removed`,
`PropertiesChanged`).

## Requirements (on the phone)

- `python3-gi`, `python3-dbus`, `gir1.2-adw-1`, `gir1.2-gtk-4.0`, oFono
- FuriLabs oFono plugin (`org.ofono.FuriLabs.AT`)

Verify with:

```sh
busctl introspect org.ofono /ril_0 org.ofono.FuriLabs.AT
```

## Run

```sh
python3 furimodem-tool.py
```

## oFono interfaces used

| Interface | Purpose |
|-----------|---------|
| `org.ofono.Manager` | Enumerate modems, add/remove signals |
| `org.ofono.Modem` | Manufacturer, Model, Revision, Type, Online, Powered |
| `org.ofono.NetworkRegistration` | Operator, registration, Tech, CellId, MCC/MNC, Strength |
| `org.ofono.SimManager` | IMSI, ICCID |
| `org.ofono.FuriLabs.AT` | `SendCommand(s) -> s` raw AT passthrough |
| `org.ofono.ConnectionManager` | `GetContexts()` PDP context enumeration |
| `org.ofono.ConnectionContext` | `SetProperty` (APN), `ProvisionContext` (XML) |

Agent/handoff notes (deployment, phone hosts, pitfalls): see AGENTS.md.
