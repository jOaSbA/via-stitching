# Windows dialog helpers.
#
# An IPC plugin runs in its own process, so its wx dialog is a top-level window of a
# different application and gets its own taskbar button. Marking it a tool window
# removes that. Staying above the editor is wx.STAY_ON_TOP's job, not this file's:
# owning our window to KiCad's HWND across processes is unsupported and crashes both.
#
# No-op on non-Windows platforms.
#
# License: GPL-3.0-or-later

import sys


def make_tool_window(window):
    """Keep `window` out of the taskbar."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        user32 = ctypes.windll.user32
        hwnd = window.GetHandle()
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        )
    except Exception:
        pass
