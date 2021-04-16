#!/usr/bin/python -OO

import sys
import commands
import gettext

gettext.install("live-installer", "/usr/share/gooroom/locale")

sys.path.insert(1, '/usr/lib/live-installer')
from frontend.gtk_interface import InstallerWindow

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

# main entry
if __name__ == "__main__":
    #Just one window
    num_live_installer = commands.getoutput("ps -A | grep liveInstaller | wc -l")
    if (num_live_installer is not "0"):
        quit()

    #Set process name
    architecture = commands.getoutput("uname -a")
    if (architecture.find("x86_64") >= 0):
        import ctypes
        libc = ctypes.CDLL('libc.so.6')
        libc.prctl(15, 'liveInstaller', 0, 0, 0)
    else:
        import dl
        if os.path.exists('/lib/libc.so.6'):
            libc = dl.open('/lib/libc.so.6')
            libc.call('prctl', 15, 'liveInstaller', 0, 0, 0)
        elif os.path.exists('/lib/i386-linux-gnu/libc.so.6'):
            libc = dl.open('/lib/i386-linux-gnu/libc.so.6')
            libc.call('prctl', 15, 'liveInstaller', 0, 0, 0)

    if("install" in commands.getoutput("cat /proc/cmdline")):
        win = InstallerWindow(fullscreen=True)
    else:
        win = InstallerWindow(fullscreen=False)

    Gtk.main()
