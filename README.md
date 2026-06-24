# FuriModem Tool (oFono)

PyGTK (PyGObject/GTK4) application that talks to a 5G modem through oFono's
DBus interface, using the FuriLabs AT passthrough plugin:

    busctl call org.ofono /ril_0 org.ofono.FuriLabs.AT SendCommand s "AT+ECSQ"

Designed to run on a FuriLabs phone running phosh (FuriOS), where oFono
is the canonical modem stack.

## Features

- Lists every oFono modem and lets you pick one.
- Live modem info panel (manufacturer, model, IMEI, online state, registration,
  RAT, signal %).
- 5G signal panel from `AT+CESQ` + `AT+CEREG`: RAT, MCC/MNC, TAC, cell ID,
  RSRP, RSRQ, RSSI, SINR, RXLEV, RSCP, ECNO.
- AT command terminal with:
  - Color-coded tx/rx log (blue = sent, green = received).
  - Free-form input with command history (Up/Down to scroll, click to
    re-run from the history list).
  - Per-command timeout (the call runs on a worker thread so the GUI stays
    responsive).
- Preset buttons for common queries: `ATI`, `AT+COPS?`, `AT+ECSQ`, `AT+CESQ`,
  `AT+QENG="servingcell"`, `AT+QCAINFO`, `AT+CGSN`, `AT+CIMI`, etc.
- Subscribes to oFono `ModemAdded` / `ModemRemoved` and
  `PropertiesChanged` signals, so the UI updates when the modem hot-plugs
  or changes state.
- Auto-refresh of modem info and signal every 2 s.

## Requirements

On a FuriLabs phone (FuriOS):

- `python3-gi`, `python3-dbus`, `gir1.2-adw-1`, `gir1.2-gtk-4.0`, `oFono`
- FuriLabs oFono plugin (provides `org.ofono.FuriLabs.AT.SendCommand`)

Verify the interface is present with:

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
| `org.ofono.Manager` | Enumerate modems via `GetModems()` |
| `org.ofono.Modem` | Manufacturer, Model, Revision, Serial, Online, Powered, Type |
| `org.freedesktop.DBus.Properties` | Property reads + change notifications |
| `org.ofono.FuriLabs.AT.SendCommand(s) -> s` | Raw AT passthrough |
| `org.ofono.Modem.Network` | Operator, registration, technology |
| `org.ofono.Modem.Signal` | Signal %, BER, RAT |
| `org.ofono.Modem.SimManager` | IMEI (from SIM serial) |
