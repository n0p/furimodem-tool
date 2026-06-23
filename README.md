# 5G Modem AT Tool (oFono)

PyGTK (PyGObject/GTK3) application that talks to a 5G modem through oFono's
DBus interface, using the FuriLabs AT passthrough plugin:

    busctl call org.ofono /ril_0 org.ofono.FuriLabs.AT SendCommand s "AT+ECSQ"

Designed to run on a Linux phone (PinePhone, Mobian, FuriOS, etc.) where oFono
is the canonical modem stack.

## Features

- Lists every oFono modem and lets you pick one.
- Live modem info panel (manufacturer, model, IMEI, online state, registration,
  RAT, signal %).
- 5G signal panel populated from `AT+QENG="servingcell"` (Quectel):
  RAT, band, bandwidth, ARFCN/NRARFCN, PCI, RSRP, RSRQ, SINR, RSSI, MCC/MNC,
  cell ID. Falls back to `AT+QCAINFO` parsing.
- AT command terminal with:
  - Color-coded tx/rx log (blue = sent, green = received).
  - Free-form input with command history (Up/Down to scroll, click to
    re-run from the history list).
  - Per-command timeout (the call runs on a worker thread so the GUI stays
    responsive).
- Preset buttons for common queries: `ATI`, `AT+COPS?`, `AT+ECSQ`, `AT+CESQ`,
  `AT+QENG="servingcell"`, `AT+QCAINFO`, `AT+QCFG="nr5g"?`,
  `AT+QCFG="band"?`, `AT+CGSN`, `AT+CIMI`, `AT+CGREG?`, etc.
- Subscribes to oFono `ModemAdded` / `ModemRemoved` and
  `PropertiesChanged` signals, so the UI updates when the modem hot-plugs
  or changes state.
- Auto-refresh of the modem info every 500 ms while the window is open.

## Requirements

On a typical Linux phone:

```sh
sudo apt install python3-gi python3-dbus gir1.2-gtk-3.0 ofono
sudo systemctl enable --now ofono
```

You need the FuriLabs oFono plugin installed (provides
`org.ofono.FuriLabs.AT.SendCommand`). On Mobian/FuriOS this is included by
default.

Verify the interface is present with:

```sh
busctl introspect org.ofono /ril_0 org.ofono.FuriLabs.AT
```

## Run

```sh
python3 5g-at-tool.py
```

## oFono interfaces used

| Interface | Purpose |
|-----------|---------|
| `org.ofono.Manager` | Enumerate modems via `GetModems()` |
| `org.ofono.Modem` | Manufacturer, Model, Revision, Serial, Online, Powered, Type |
| `org.freedesktop.DBus.Properties` | Property reads + change notifications |
| `org.ofono.FuriLabs.AT.SendCommand(s) -> s` | Raw AT passthrough |
| `org.ofono.Modem.Network` | Operator, registration, technology |
| `org.ofono.Modem.Signal` | Signal %, BER, RAT |
| `org.ofono.Modem.SimManager` | IMEI (from SIM serial) |

## Notes

- `SendCommand` is synchronous in oFono and has no built-in timeout; the
  app runs it on a worker thread and gives up after the user-configured
  timeout (the thread keeps running but its result is discarded).
- 5G NR-specific values (RSRP/RSRQ/SINR/band/ARFCN) come from
  `AT+QENG="servingcell"` because oFono's high-level interfaces only expose
  a coarse signal %. If your modem uses a different vendor dialect, edit
  the preset list at `MainWindow._build_ui()` and the parser at
  `parse_qeng_servingcell()`.
- The `Signal (ECSQ)` preset sends `AT+ECSQ` (Ericsson proprietary) and
  the response is parsed in `parse_ecsq()` if you want to display it.

## Files

- `5g-at-tool.py` — single-file application, no external assets.