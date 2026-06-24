#!/usr/bin/env python3
"""FuriModem Tool (oFono backend, Adw/GTK4).

Entry point. UI is in ui.py, DBus in ofono.py, parsers in parsers.py.

    busctl call org.ofono /ril_0 org.ofono.FuriLabs.AT SendCommand s "AT+ECSQ"
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from ui import MainWindow


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="es.n0p.furimodem_tool")

    def do_activate(self):
        win = MainWindow(self)
        win.present()


def main():
    app = App()
    return app.run(sys.argv)


if __name__ == "__main__":
    main()
