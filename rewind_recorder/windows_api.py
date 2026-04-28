import ctypes
import sys

import cv2
import numpy as np
from PySide6.QtWidgets import QWidget

from rewind_recorder.types import CaptureArea


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("hCursor", ctypes.c_void_p),
        ("ptScreenPos", POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", ctypes.c_bool),
        ("xHotspot", ctypes.c_ulong),
        ("yHotspot", ctypes.c_ulong),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", RGBQUAD * 1),
    ]


CURSOR_SHOWING = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
DIB_RGB_COLORS = 0
BI_RGB = 0
DI_NORMAL = 0x0003


def _configure_api() -> None:
    if sys.platform != "win32":
        return

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    user32.GetCursorInfo.restype = ctypes.c_bool
    user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ICONINFO)]
    user32.GetIconInfo.restype = ctypes.c_bool
    user32.DrawIconEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
    ]
    user32.DrawIconEx.restype = ctypes.c_bool
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = ctypes.c_bool

    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint32,
    ]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_bool
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.c_bool


_configure_api()


def get_cursor_info() -> CURSORINFO | None:
    if sys.platform != "win32":
        return None
    cursor_info = CURSORINFO()
    cursor_info.cbSize = ctypes.sizeof(CURSORINFO)
    if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(cursor_info)):
        return None
    if not cursor_info.flags & CURSOR_SHOWING:
        return None
    return cursor_info


def draw_cursor_overlay(frame: np.ndarray, area: CaptureArea) -> None:
    cursor_info = get_cursor_info()
    if cursor_info is None or not cursor_info.hCursor:
        return

    cursor_x = int(cursor_info.ptScreenPos.x) - area.x
    cursor_y = int(cursor_info.ptScreenPos.y) - area.y
    height, width = frame.shape[:2]
    if cursor_x < -32 or cursor_y < -32 or cursor_x >= width + 32 or cursor_y >= height + 32:
        return

    icon_info = ICONINFO()
    hotspot_x = 0
    hotspot_y = 0
    got_icon_info = bool(ctypes.windll.user32.GetIconInfo(cursor_info.hCursor, ctypes.byref(icon_info)))
    if got_icon_info:
        hotspot_x = int(icon_info.xHotspot)
        hotspot_y = int(icon_info.yHotspot)

    try:
        _draw_windows_cursor(frame, cursor_info.hCursor, cursor_x - hotspot_x, cursor_y - hotspot_y)
    finally:
        if got_icon_info:
            if icon_info.hbmMask:
                ctypes.windll.gdi32.DeleteObject(icon_info.hbmMask)
            if icon_info.hbmColor:
                ctypes.windll.gdi32.DeleteObject(icon_info.hbmColor)


def _draw_windows_cursor(frame: np.ndarray, cursor_handle: int, x: int, y: int) -> None:
    height, width = frame.shape[:2]
    bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
    bgra = np.ascontiguousarray(bgra)

    bitmap_info = BITMAPINFO()
    bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bitmap_info.bmiHeader.biWidth = width
    bitmap_info.bmiHeader.biHeight = -height
    bitmap_info.bmiHeader.biPlanes = 1
    bitmap_info.bmiHeader.biBitCount = 32
    bitmap_info.bmiHeader.biCompression = BI_RGB

    bits = ctypes.c_void_p()
    screen_dc = ctypes.windll.user32.GetDC(None)
    mem_dc = ctypes.windll.gdi32.CreateCompatibleDC(screen_dc)
    bitmap = ctypes.windll.gdi32.CreateDIBSection(
        mem_dc, ctypes.byref(bitmap_info), DIB_RGB_COLORS,
        ctypes.byref(bits), None, 0,
    )
    if screen_dc:
        ctypes.windll.user32.ReleaseDC(None, screen_dc)

    if not mem_dc or not bitmap or not bits.value:
        if bitmap:
            ctypes.windll.gdi32.DeleteObject(bitmap)
        if mem_dc:
            ctypes.windll.gdi32.DeleteDC(mem_dc)
        return

    old_bitmap = ctypes.windll.gdi32.SelectObject(mem_dc, bitmap)
    try:
        ctypes.memmove(bits.value, bgra.ctypes.data, bgra.nbytes)
        ctypes.windll.user32.DrawIconEx(mem_dc, int(x), int(y), cursor_handle, 0, 0, 0, None, DI_NORMAL)
        ctypes.memmove(bgra.ctypes.data, bits.value, bgra.nbytes)
    finally:
        ctypes.windll.gdi32.SelectObject(mem_dc, old_bitmap)
        ctypes.windll.gdi32.DeleteObject(bitmap)
        ctypes.windll.gdi32.DeleteDC(mem_dc)

    frame[:, :, :] = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)


def exclude_widget_from_capture(widget: QWidget) -> bool:
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(widget.winId())
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False


def force_widget_topmost(widget: QWidget) -> None:
    widget.raise_()
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        ctypes.windll.user32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0, flags)
    except Exception:
        pass
