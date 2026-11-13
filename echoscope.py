#!/usr/bin/env python3
"""
EchoScope (native) - audio gaming radar for Windows.

Listens to the default playback device through WASAPI loopback and draws a
radar: angle = left/right, radius = loudness, white arrow = newest cue.
The window is frameless, always-on-top and translucent (Win32 layered window
with per-pixel alpha). Overlay mode makes it click-through so it can float over
borderless-windowed games.

Mirrors soundsight.html (the web version): same analysis, cue model, five
visual modes, hotkeys and settings.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import sys
import threading
import time
import traceback
from ctypes import (POINTER, Structure, WINFUNCTYPE, byref, c_int, c_long, c_ssize_t,
                    c_size_t, c_ubyte, c_ulong, c_ushort, c_void_p, c_wchar_p, cast,
                    wintypes)

APP_NAME = 'EchoScope'
VERSION = '1.0.0'


# ---------------------------------------------------------------------------
# Paths and logging (settings.json and errorlog.txt live next to the EXE)
# ---------------------------------------------------------------------------
def _app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
SETTINGS_PATH = os.path.join(APP_DIR, 'settings.json')
LOG_PATH = os.path.join(APP_DIR, 'errorlog.txt')
_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = time.strftime('%Y-%m-%d %H:%M:%S') + '  ' + msg.rstrip() + '\n'
    with _log_lock:
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line)
        except OSError:
            pass


def log_exc(where: str) -> None:
    log(where + '\n' + traceback.format_exc())


class _LogStream:
    """Stand-in for stdout/stderr when running without a console."""

    def __init__(self) -> None:
        self._buf = ''

    def write(self, s: str) -> int:
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line.strip():
                log('[stderr] ' + line)
        return len(s)

    def flush(self) -> None:
        pass


if sys.stdout is None:
    sys.stdout = _LogStream()  # type: ignore[assignment]
if sys.stderr is None:
    sys.stderr = _LogStream()  # type: ignore[assignment]


def _fatal(msg: str) -> None:
    log('FATAL: ' + msg)
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
    except Exception:
        pass
    sys.exit(1)


try:
    import numpy as np
    import cairo
    import pyaudiowpatch as pyaudio
except ImportError as e:  # pragma: no cover
    _fatal('A required package is missing: %s\n\nInstall them with:\n'
           '    python -m pip install -r requirements.txt' % e)


# ---------------------------------------------------------------------------
# Constants shared with the web version
# ---------------------------------------------------------------------------
MODES = ['petals', 'dots', 'comet', 'fan', 'line']
MODE_NAMES = {'petals': 'Petals', 'dots': 'Dots', 'comet': 'Comet', 'fan': 'Solid Fan', 'line': 'Single Line'}
THEME_ORDER = ['cyan', 'green', 'magenta', 'amber', 'white', 'red']
THEMES_HEX = {'cyan': '#3ee8ff', 'green': '#62ff8a', 'magenta': '#ff5fe0', 'amber': '#ffb648',
              'white': '#f4f7fb', 'red': '#ff5566'}


def _hex(h: str):
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5))


THEMES = {k: _hex(v) for k, v in THEMES_HEX.items()}
FG = _hex('#e7edf4')
MUTED = _hex('#8b9ab0')
LIVE = _hex('#62ff8a')
WARN = _hex('#ffb648')
DANGER = _hex('#ff5566')
PANEL = _hex('#10161e')
PANEL2 = _hex('#171f29')
LINE = _hex('#28323f')

FLOOR_DB, CEIL_DB = -72.0, -22.0          # band level mapped onto 0..1 between these
BAND_LO, BAND_HI, BAND_N = 70.0, 9000.0, 10  # log-spaced analysis bands
FFT_SIZE = 2048
SMOOTHING = 0.4                           # AnalyserNode smoothingTimeConstant
FRAME = 1.0 / 60.0

DEFAULTS = {
    'mode': 'petals', 'boost': False, 'life': 0.4, 'opacity': 0.9, 'lineWidth': 3.0,
    'radiusBoost': 20, 'gate': 0.2, 'gain': 1.0, 'theme': 'cyan', 'guides': True, 'meters': True,
    'pinned': True, 'hideWelcome': False, 'window': None,
    'globalHotkeys': True,   # Ctrl+Shift+key works from any app
    'startOverlay': False,   # launch straight into click-through overlay mode
    'smoothing': 0.5,        # 0..1: how much cue angle / level are smoothed over time
    'maxCues': 3,            # only the N loudest bands draw a cue each frame
    'adaptive': False,       # fade steady sounds to half so changes stand out (off: show everything)
    'sensitivity': 5,        # 1..10 master dial: scales input gain and shifts the noise gate together
    'width': 7,              # 1..10 stereo width: how small a L/R level difference already counts as fully sideways
    'centre': 5,             # 0..10 centre suppression: down-weights sound that is identical in both ears
    'colourByPitch': True,   # low bands warm, high bands light: tells engines from footsteps at a glance
    'markers': True,         # sudden sounds leave a dot on the rim for a few seconds
    'profiles': {},          # per-game tuning: {"game.exe": {setting: value}}
    'autoProfile': True,     # switch profiles with the foreground app
    'autoHide': False,       # show the radar only while a profiled game (or EchoScope) is in front
    'surround': 'auto',      # 'auto': full-circle radar when the playback device is 5.1/7.1; 'off': stereo only
    'fullCircle': False,     # always draw the full circle, even with stereo input (front half used)
    'processCapture': False, # capture only the last app that was in front (WASAPI process loopback)
    'schema': 2,             # bumped when a default must override an older saved value
}
PROFILE_KEYS = ['mode', 'sensitivity', 'width', 'centre', 'smoothing', 'maxCues', 'gate', 'radiusBoost', 'life',
                'lineWidth', 'opacity', 'boost', 'adaptive', 'colourByPitch', 'markers']
DESKTOP_EXES = {'explorer.exe', 'searchhost.exe', 'startmenuexperiencehost.exe', 'shellexperiencehost.exe',
                'textinputhost.exe', 'lockapp.exe', 'taskmgr.exe', 'applicationframehost.exe', 'dwm.exe'}
SIZE_PRESETS = [('Small', 300, 210), ('Medium', 460, 320), ('Large', 640, 440)]
MARKER_LIFE = 4.0
# speaker angles (degrees, 0 = forward, + = right) by channel count; None = LFE (ignored)
SPEAKER_LAYOUTS = {4: [-30, 30, -110, 110], 6: [-30, 30, 0, None, -110, 110], 8: [-30, 30, 0, None, -150, 150, -90, 90]}


def width_db(S: dict) -> float:
    """Level difference (dB) between L and R that maps to 90 degrees: width 1 -> 15.5 dB (narrow,
    close to a constant-power pan law), 7 -> 6.5 dB, 10 -> 2 dB (anything off-centre is sideways)."""
    w = int(clamp(int(S.get('width', 7)), 1, 10))
    return 2.0 + (10 - w) * 1.5
SMOOTHING_CHOICES = [0.0, 0.25, 0.5, 0.75, 1.0]
SMOOTHING_NAMES = {0.0: 'Off', 0.25: 'Low', 0.5: 'Medium', 0.75: 'High', 1.0: 'Max'}
MAXCUES_CHOICES = [1, 2, 3, 5, 10]


def effective_gain_gate(S: dict):
    """Sensitivity 1..10 folds into the raw gain/gate: 5 is neutral, each step is +-3 dB of gain
    and -+0.03 of gate, so one dial makes cues both bigger and more numerous (or the reverse)."""
    sens = int(clamp(int(S.get('sensitivity', 5)), 1, 10))
    gain = float(S['gain']) * (2.0 ** ((sens - 5) / 2.0))
    gate = clamp(float(S['gate']) + (5 - sens) * 0.03, 0.0, 0.9)
    return gain, gate


def clamp(v, a, b):
    return b if v > b else a if v < a else v


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            for k in DEFAULTS:
                if k in obj:
                    s[k] = obj[k]
    except FileNotFoundError:
        pass
    except Exception:
        log_exc('load settings')
    try:
        schema = int(s.get('schema', 1))
    except Exception:
        schema = 1
    if schema < 2:
        # 1.0 shipped "adaptive" on by default, which hid continuous sounds almost entirely.
        s['adaptive'] = False
    s['schema'] = DEFAULTS['schema']
    if s['mode'] not in MODES:
        s['mode'] = 'petals'
    if s['theme'] not in THEMES:
        s['theme'] = 'cyan'
    try:
        s['life'] = clamp(float(s['life']), 0.1, 2.0)
        s['opacity'] = clamp(float(s['opacity']), 0.2, 1.0)
        s['lineWidth'] = clamp(float(s['lineWidth']), 1.0, 8.0)
        s['radiusBoost'] = clamp(float(s['radiusBoost']), 5.0, 25.0)
        s['gate'] = clamp(float(s['gate']), 0.0, 0.6)
        s['gain'] = clamp(float(s['gain']), 0.25, 6.0)
        s['smoothing'] = clamp(float(s['smoothing']), 0.0, 1.0)
        s['maxCues'] = int(clamp(int(s['maxCues']), 1, BAND_N))
        s['sensitivity'] = int(clamp(int(s['sensitivity']), 1, 10))
        s['width'] = int(clamp(int(s['width']), 1, 10))
        s['centre'] = int(clamp(int(s['centre']), 0, 10))
    except Exception:
        for k in ('life', 'opacity', 'lineWidth', 'radiusBoost', 'gate', 'gain', 'smoothing', 'maxCues', 'sensitivity', 'width', 'centre'):
            s[k] = DEFAULTS[k]
    for k in ('boost', 'guides', 'meters', 'pinned', 'hideWelcome', 'globalHotkeys', 'startOverlay', 'adaptive',
              'colourByPitch', 'markers', 'autoProfile', 'autoHide', 'processCapture', 'fullCircle'):
        s[k] = bool(s[k])
    if not isinstance(s['profiles'], dict):
        s['profiles'] = {}
    if s['surround'] not in ('auto', 'off'):
        s['surround'] = 'auto'
    return s


def save_settings(s: dict) -> None:
    try:
        tmp = SETTINGS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        log_exc('save settings')


# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
shell32 = ctypes.WinDLL('shell32', use_last_error=True)
ole32 = ctypes.WinDLL('ole32', use_last_error=True)
winmm = ctypes.WinDLL('winmm', use_last_error=True)

LRESULT = c_ssize_t
LONG_PTR = c_ssize_t
WNDPROC = WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('style', wintypes.UINT), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', c_int), ('cbWndExtra', c_int), ('hInstance', wintypes.HINSTANCE),
                ('hIcon', wintypes.HICON), ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR),
                ('hIconSm', wintypes.HICON)]


class BLENDFUNCTION(Structure):
    _fields_ = [('BlendOp', c_ubyte), ('BlendFlags', c_ubyte), ('SourceConstantAlpha', c_ubyte),
                ('AlphaFormat', c_ubyte)]


class BITMAPINFOHEADER(Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', c_long), ('biHeight', c_long),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', c_long), ('biYPelsPerMeter', c_long),
                ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]


class BITMAPINFO(Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD * 3)]


class MINMAXINFO(Structure):
    _fields_ = [('ptReserved', wintypes.POINT), ('ptMaxSize', wintypes.POINT), ('ptMaxPosition', wintypes.POINT),
                ('ptMinTrackSize', wintypes.POINT), ('ptMaxTrackSize', wintypes.POINT)]


class MONITORINFO(Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT),
                ('dwFlags', wintypes.DWORD)]


class GUID(Structure):
    _fields_ = [('Data1', c_ulong), ('Data2', c_ushort), ('Data3', c_ushort), ('Data4', c_ubyte * 8)]


class NOTIFYICONDATAW(Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND), ('uID', wintypes.UINT), ('uFlags', wintypes.UINT),
                ('uCallbackMessage', wintypes.UINT), ('hIcon', wintypes.HICON), ('szTip', ctypes.c_wchar * 128),
                ('dwState', wintypes.DWORD), ('dwStateMask', wintypes.DWORD), ('szInfo', ctypes.c_wchar * 256),
                ('uVersion', wintypes.UINT), ('szInfoTitle', ctypes.c_wchar * 64), ('dwInfoFlags', wintypes.DWORD),
                ('guidItem', GUID), ('hBalloonIcon', wintypes.HICON)]


def _proto(fn, restype, argtypes):
    fn.restype = restype
    fn.argtypes = argtypes


_proto(user32.DefWindowProcW, LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM])
_proto(user32.RegisterClassExW, wintypes.ATOM, [POINTER(WNDCLASSEXW)])
_proto(user32.CreateWindowExW, wintypes.HWND,
       [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, c_int, c_int, c_int, c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID])
_proto(user32.GetWindowLongPtrW, LONG_PTR, [wintypes.HWND, c_int])
_proto(user32.SetWindowLongPtrW, LONG_PTR, [wintypes.HWND, c_int, LONG_PTR])
_proto(user32.SetWindowPos, wintypes.BOOL, [wintypes.HWND, c_ssize_t, c_int, c_int, c_int, c_int, wintypes.UINT])
_proto(user32.UpdateLayeredWindow, wintypes.BOOL,
       [wintypes.HWND, wintypes.HDC, POINTER(wintypes.POINT), POINTER(wintypes.SIZE), wintypes.HDC,
        POINTER(wintypes.POINT), wintypes.COLORREF, POINTER(BLENDFUNCTION), wintypes.DWORD])
_proto(user32.PeekMessageW, wintypes.BOOL, [POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT])
_proto(user32.TranslateMessage, wintypes.BOOL, [POINTER(wintypes.MSG)])
_proto(user32.DispatchMessageW, LRESULT, [POINTER(wintypes.MSG)])
_proto(user32.MsgWaitForMultipleObjects, wintypes.DWORD, [wintypes.DWORD, c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD])
_proto(user32.PostMessageW, wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM])
_proto(user32.SendMessageW, LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM])
_proto(user32.GetWindowRect, wintypes.BOOL, [wintypes.HWND, POINTER(wintypes.RECT)])
_proto(user32.GetClientRect, wintypes.BOOL, [wintypes.HWND, POINTER(wintypes.RECT)])
_proto(user32.ShowWindow, wintypes.BOOL, [wintypes.HWND, c_int])
_proto(user32.SetForegroundWindow, wintypes.BOOL, [wintypes.HWND])
_proto(user32.SetFocus, wintypes.HWND, [wintypes.HWND])
_proto(user32.DestroyWindow, wintypes.BOOL, [wintypes.HWND])
_proto(user32.IsIconic, wintypes.BOOL, [wintypes.HWND])
_proto(user32.LoadCursorW, wintypes.HANDLE, [wintypes.HINSTANCE, wintypes.LPCWSTR])
_proto(user32.LoadIconW, wintypes.HICON, [wintypes.HINSTANCE, wintypes.LPCWSTR])
_proto(user32.CreatePopupMenu, wintypes.HMENU, [])
_proto(user32.AppendMenuW, wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, c_size_t, wintypes.LPCWSTR])
_proto(user32.TrackPopupMenu, c_int, [wintypes.HMENU, wintypes.UINT, c_int, c_int, c_int, wintypes.HWND, c_void_p])
_proto(user32.DestroyMenu, wintypes.BOOL, [wintypes.HMENU])
_proto(user32.GetCursorPos, wintypes.BOOL, [POINTER(wintypes.POINT)])
_proto(user32.RegisterHotKey, wintypes.BOOL, [wintypes.HWND, c_int, wintypes.UINT, wintypes.UINT])
_proto(user32.UnregisterHotKey, wintypes.BOOL, [wintypes.HWND, c_int])
_proto(user32.MonitorFromRect, wintypes.HMONITOR, [POINTER(wintypes.RECT), wintypes.DWORD])
_proto(user32.MonitorFromWindow, wintypes.HMONITOR, [wintypes.HWND, wintypes.DWORD])
_proto(user32.GetMonitorInfoW, wintypes.BOOL, [wintypes.HMONITOR, POINTER(MONITORINFO)])
_proto(user32.SetTimer, c_size_t, [wintypes.HWND, c_size_t, wintypes.UINT, c_void_p])
_proto(user32.KillTimer, wintypes.BOOL, [wintypes.HWND, c_size_t])
_proto(user32.PostQuitMessage, None, [c_int])
_proto(user32.ValidateRect, wintypes.BOOL, [wintypes.HWND, c_void_p])
_proto(user32.MessageBoxW, c_int, [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT])
_proto(user32.GetKeyState, wintypes.SHORT, [c_int])
_proto(user32.WindowFromPoint, wintypes.HWND, [wintypes.POINT])
_proto(user32.GetForegroundWindow, wintypes.HWND, [])
_proto(user32.GetWindowThreadProcessId, wintypes.DWORD, [wintypes.HWND, POINTER(wintypes.DWORD)])
_proto(kernel32.OpenProcess, wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
_proto(kernel32.QueryFullProcessImageNameW, wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, POINTER(wintypes.DWORD)])
_proto(kernel32.GetExitCodeProcess, wintypes.BOOL, [wintypes.HANDLE, POINTER(wintypes.DWORD)])
_proto(kernel32.CloseHandle, wintypes.BOOL, [wintypes.HANDLE])
_proto(kernel32.CreateEventW, wintypes.HANDLE, [c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR])
_proto(kernel32.SetEvent, wintypes.BOOL, [wintypes.HANDLE])
_proto(kernel32.WaitForSingleObject, wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD])
_proto(kernel32.GetCurrentProcessId, wintypes.DWORD, [])


def process_name(pid: int) -> str:
    """Lower-case exe name of a process, or '' if it cannot be read."""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)
    return ''


def process_alive(pid: int) -> bool:
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        return bool(kernel32.GetExitCodeProcess(h, byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)
_proto(gdi32.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC])
_proto(gdi32.CreateDIBSection, wintypes.HBITMAP,
       [wintypes.HDC, POINTER(BITMAPINFO), wintypes.UINT, POINTER(c_void_p), wintypes.HANDLE, wintypes.DWORD])
_proto(gdi32.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ])
_proto(gdi32.DeleteObject, wintypes.BOOL, [wintypes.HGDIOBJ])
_proto(gdi32.DeleteDC, wintypes.BOOL, [wintypes.HDC])
_proto(kernel32.GetModuleHandleW, wintypes.HMODULE, [wintypes.LPCWSTR])
_proto(user32.IsWindow, wintypes.BOOL, [wintypes.HWND])
_proto(user32.RegisterWindowMessageW, wintypes.UINT, [wintypes.LPCWSTR])
_proto(shell32.ExtractIconExW, wintypes.UINT, [wintypes.LPCWSTR, c_int, POINTER(wintypes.HICON), POINTER(wintypes.HICON), wintypes.UINT])
_proto(shell32.Shell_NotifyIconW, wintypes.BOOL, [wintypes.DWORD, POINTER(NOTIFYICONDATAW)])
try:
    _proto(shell32.SetCurrentProcessExplicitAppUserModelID, ctypes.c_long, [wintypes.LPCWSTR])
except AttributeError:  # pragma: no cover
    pass
_proto(ole32.CoInitializeEx, ctypes.c_long, [c_void_p, wintypes.DWORD])
_proto(ole32.CoCreateInstance, ctypes.c_long, [POINTER(GUID), c_void_p, wintypes.DWORD, POINTER(GUID), POINTER(c_void_p)])
_proto(ole32.CLSIDFromString, ctypes.c_long, [wintypes.LPCWSTR, POINTER(GUID)])
_proto(ole32.CoTaskMemFree, None, [c_void_p])
try:
    _proto(user32.GetDpiForWindow, wintypes.UINT, [wintypes.HWND])
except AttributeError:  # pragma: no cover
    pass

WS_POPUP = 0x80000000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_THICKFRAME = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_APPWINDOW = 0x00040000
GWL_EXSTYLE = -20
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20
SW_SHOW, SW_RESTORE = 5, 9

WM_DESTROY, WM_MOVE, WM_SIZE, WM_ACTIVATE, WM_PAINT, WM_CLOSE = 0x2, 0x3, 0x5, 0x6, 0xF, 0x10
WM_QUIT, WM_ERASEBKGND, WM_GETMINMAXINFO, WM_DISPLAYCHANGE = 0x12, 0x14, 0x24, 0x7E
WM_CONTEXTMENU, WM_SETICON, WM_NCCALCSIZE, WM_NCHITTEST = 0x7B, 0x80, 0x83, 0x84
WM_NCLBUTTONDBLCLK, WM_NCRBUTTONDOWN, WM_NCRBUTTONUP = 0xA3, 0xA4, 0xA5
WM_KEYDOWN, WM_CHAR, WM_SYSKEYDOWN, WM_SYSCHAR = 0x100, 0x102, 0x104, 0x106
WM_TIMER, WM_RBUTTONDOWN, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x113, 0x204, 0x205, 0x203
WM_LBUTTONUP, WM_LBUTTONDOWN, WM_MOUSEMOVE, WM_MOUSEWHEEL = 0x202, 0x201, 0x200, 0x20A
WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE, WM_DPICHANGED, WM_HOTKEY = 0x231, 0x232, 0x2E0, 0x312
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
IDI_APPLICATION = 32512
HTTRANSPARENT, HTCLIENT, HTCAPTION = -1, 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 10, 11, 12, 13, 14, 15, 16, 17
MF_STRING, MF_GRAYED, MF_CHECKED, MF_POPUP, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x10, 0x800
TPM_LEFTALIGN, TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0, 0x2, 0x100
PM_REMOVE, QS_ALLINPUT = 0x1, 0x4FF
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x2, 0x4, 0x4000
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 0x2, 0x0, 0x1
DIB_RGB_COLORS, BI_RGB = 0, 0
MONITOR_DEFAULTTONULL, MONITOR_DEFAULTTOPRIMARY, MONITOR_DEFAULTTONEAREST = 0, 1, 2
VK_ESCAPE, VK_RETURN, VK_SPACE, VK_F1 = 0x1B, 0x0D, 0x20, 0x70
IDC_ARROW = 32512
TIMER_FRAME = 1
HOTKEY_OVERLAY = 1
# Global hotkeys: Ctrl+Shift + key -> action name (handled by App.hotkey_action)
GLOBAL_HOTKEYS = [
    (HOTKEY_OVERLAY, ord('O'), 'overlay'), (2, ord('B'), 'boost'), (3, ord('P'), 'pin'), (4, ord('R'), 'restart'),
    (5, 0xDB, 'life-'), (6, 0xDD, 'life+'), (7, 0xBD, 'op-'), (8, 0xBB, 'op+'),      # [ ] - =
    (11, ord('1'), 'mode0'), (12, ord('2'), 'mode1'), (13, ord('3'), 'mode2'), (14, ord('4'), 'mode3'), (15, ord('5'), 'mode4'),
    (16, 0xBC, 'sens-'), (17, 0xBE, 'sens+'),                                       # , .
]
VK_TAB, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x09, 0x25, 0x26, 0x27, 0x28


def _lo16(v):
    return ctypes.c_short(v & 0xFFFF).value


def _hi16(v):
    return ctypes.c_short((v >> 16) & 0xFFFF).value


# ---------------------------------------------------------------------------
# Default playback endpoint id (IMMDeviceEnumerator through raw COM) so a device
# change is noticed even while the old device keeps streaming silence.
# ---------------------------------------------------------------------------
_CLSID_MMDeviceEnumerator = None
_IID_IMMDeviceEnumerator = None
_GetDefaultAudioEndpoint = WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_int, c_int, POINTER(c_void_p))
_GetId = WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(c_wchar_p))
_Release = WINFUNCTYPE(c_ulong, c_void_p)
_endpoint_probe_failed = False


def _vtbl(obj, idx, proto):
    vt = cast(obj, POINTER(c_void_p))[0]
    fn = cast(c_void_p(vt), POINTER(c_void_p))[idx]
    return proto(fn)


def default_render_endpoint_id():
    global _CLSID_MMDeviceEnumerator, _IID_IMMDeviceEnumerator, _endpoint_probe_failed
    if _endpoint_probe_failed:
        return None
    try:
        if _CLSID_MMDeviceEnumerator is None:
            g1, g2 = GUID(), GUID()
            ole32.CLSIDFromString('{BCDE0395-E52F-467C-8E3D-C4579291692E}', byref(g1))
            ole32.CLSIDFromString('{A95664D2-9614-4F35-A746-DE8DB63617E6}', byref(g2))
            _CLSID_MMDeviceEnumerator, _IID_IMMDeviceEnumerator = g1, g2
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED; S_FALSE if already done
        enum = c_void_p()
        hr = ole32.CoCreateInstance(byref(_CLSID_MMDeviceEnumerator), None, 1, byref(_IID_IMMDeviceEnumerator), byref(enum))
        if hr != 0 or not enum:
            return None
        try:
            dev = c_void_p()
            _vtbl(enum, 4, _GetDefaultAudioEndpoint)(enum, 0, 1, byref(dev))  # eRender, eMultimedia
            if not dev:
                return None
            try:
                pid = c_wchar_p()
                _vtbl(dev, 5, _GetId)(dev, byref(pid))
                s = pid.value
                ole32.CoTaskMemFree(cast(pid, c_void_p))
                return s
            finally:
                _vtbl(dev, 2, _Release)(dev)
        finally:
            _vtbl(enum, 2, _Release)(enum)
    except OSError as e:
        # E_NOTFOUND when no playback device exists: report "none" rather than failing forever
        if getattr(e, 'winerror', None) == -2147023728:
            return ''
        log('endpoint probe: %r' % e)
        return None
    except Exception:
        log_exc('endpoint probe')
        _endpoint_probe_failed = True
        return None


# ---------------------------------------------------------------------------
# Per-process WASAPI loopback (Windows 10 2004+ / Windows 11): one application's audio only.
# Raw COM through ctypes: ActivateAudioInterfaceAsync on the process-loopback virtual device,
# IAudioClient in shared loopback mode, IAudioCaptureClient polled on an event.
# ---------------------------------------------------------------------------
class _BLOB(Structure):
    _fields_ = [('cbSize', c_ulong), ('pBlobData', c_void_p)]


class PROPVARIANT(Structure):
    _fields_ = [('vt', c_ushort), ('r1', c_ushort), ('r2', c_ushort), ('r3', c_ushort), ('blob', _BLOB)]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [('TargetProcessId', wintypes.DWORD), ('ProcessLoopbackMode', c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [('ActivationType', c_int), ('ProcessLoopbackParams', AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [('wFormatTag', c_ushort), ('nChannels', c_ushort), ('nSamplesPerSec', c_ulong),
                ('nAvgBytesPerSec', c_ulong), ('nBlockAlign', c_ushort), ('wBitsPerSample', c_ushort),
                ('cbSize', c_ushort)]


class WAVEFORMATEXTENSIBLE(Structure):
    _pack_ = 1
    _fields_ = [('wFormatTag', c_ushort), ('nChannels', c_ushort), ('nSamplesPerSec', c_ulong),
                ('nAvgBytesPerSec', c_ulong), ('nBlockAlign', c_ushort), ('wBitsPerSample', c_ushort),
                ('cbSize', c_ushort), ('wValidBitsPerSample', c_ushort), ('dwChannelMask', c_ulong),
                ('SubFormat', GUID)]


CHANNEL_MASKS = {2: 0x3, 4: 0x33, 6: 0x3F, 8: 0x63F}
_IID_IAudioClient_str = '{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}'
_Activate_T = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))
_GetMixFormat_T = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(c_void_p))


def default_mix_channels() -> int:
    """Channel count of the default playback device's shared mix format (8 when Windows spatial
    sound presents a virtual 7.1 to applications), or 2 if it cannot be read."""
    try:
        ole32.CoInitializeEx(None, 0x2)
        clsid, iid = GUID(), GUID()
        ole32.CLSIDFromString('{BCDE0395-E52F-467C-8E3D-C4579291692E}', byref(clsid))
        ole32.CLSIDFromString('{A95664D2-9614-4F35-A746-DE8DB63617E6}', byref(iid))
        enum = c_void_p()
        if ole32.CoCreateInstance(byref(clsid), None, 1, byref(iid), byref(enum)) != 0 or not enum:
            return 2
        try:
            dev = c_void_p()
            _vtbl(enum, 4, _GetDefaultAudioEndpoint)(enum, 0, 1, byref(dev))
            if not dev:
                return 2
            try:
                client = c_void_p()
                iac = GUID()
                ole32.CLSIDFromString(_IID_IAudioClient_str, byref(iac))
                if _vtbl(dev, 3, _Activate_T)(dev, byref(iac), 23, None, byref(client)) != 0 or not client:
                    return 2
                try:
                    mix = c_void_p()
                    if _vtbl(client, 8, _GetMixFormat_T)(client, byref(mix)) != 0 or not mix:
                        return 2
                    ch = int(cast(mix, POINTER(WAVEFORMATEX)).contents.nChannels)
                    ole32.CoTaskMemFree(mix)
                    return ch
                finally:
                    _vtbl(client, 2, _AR_T)(client)
            finally:
                _vtbl(dev, 2, _AR_T)(dev)
        finally:
            _vtbl(enum, 2, _AR_T)(enum)
    except Exception:
        log_exc('mix format probe')
        return 2


_QI_T = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_AR_T = WINFUNCTYPE(c_ulong, c_void_p)
_AC_T = WINFUNCTYPE(ctypes.c_long, c_void_p, c_void_p)


class _HandlerVtbl(Structure):
    _fields_ = [('QueryInterface', _QI_T), ('AddRef', _AR_T), ('Release', _AR_T), ('ActivateCompleted', _AC_T)]


class _Handler(Structure):
    _fields_ = [('lpVtbl', POINTER(_HandlerVtbl))]


_Initialize_T = WINFUNCTYPE(ctypes.c_long, c_void_p, c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                            POINTER(WAVEFORMATEX), POINTER(GUID))
_GetService_T = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_SetEventHandle_T = WINFUNCTYPE(ctypes.c_long, c_void_p, wintypes.HANDLE)
_Simple_T = WINFUNCTYPE(ctypes.c_long, c_void_p)
_GetBuffer_T = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(c_void_p), POINTER(wintypes.UINT), POINTER(wintypes.DWORD),
                           POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong))
_ReleaseBuffer_T = WINFUNCTYPE(ctypes.c_long, c_void_p, wintypes.UINT)
_GetNextPacketSize_T = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(wintypes.UINT))
_GetActivateResult_T = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(ctypes.c_long), POINTER(c_void_p))


def _iid(s: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(s, byref(g))
    return g


def _hrs(hr: int) -> str:
    return '0x%08X' % (hr & 0xFFFFFFFF)


class ProcessLoopback:
    RATE = 48000

    def __init__(self, pid: int, name: str, push, on_end, channels: int = 2) -> None:
        self.pid, self.name, self.push, self.on_end = pid, name, push, on_end
        self.channels = channels if channels in CHANNEL_MASKS else 2
        self.running = False
        self.alive = False
        self.error = ''
        self.ready = threading.Event()
        self.thread = None
        self._keep = None

    def start(self) -> bool:
        """Start capturing; returns True once audio is flowing, False (with .error) if it could not."""
        self.running = True
        self.thread = threading.Thread(target=self._run, name='process-loopback', daemon=True)
        self.thread.start()
        self.ready.wait(6.0)
        return self.alive

    def stop(self) -> None:
        self.running = False

    def _run(self) -> None:
        try:
            self._capture()
        except Exception as e:
            log_exc('process loopback')
            self.error = str(e) or e.__class__.__name__
        finally:
            was_alive = self.alive
            self.alive = False
            self.ready.set()
            if self.running and was_alive:
                self.on_end(self.error or 'capture ended')

    def _capture(self) -> None:
        ole32.CoInitializeEx(None, 0)  # MTA for this worker
        mmdevapi = ctypes.WinDLL('mmdevapi')
        mmdevapi.ActivateAudioInterfaceAsync.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT), c_void_p, POINTER(c_void_p)]
        mmdevapi.ActivateAudioInterfaceAsync.restype = ctypes.c_long
        IID_IAudioClient = _iid('{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}')
        IID_IAudioCaptureClient = _iid('{C8ADBD64-E71E-48A0-A4DE-185C395CD317}')
        IID_Handler = _iid('{41D949AB-9862-444A-80F6-C261334DA5EB}')
        IID_IUnknown = _iid('{00000000-0000-0000-C000-000000000046}')
        IID_IAgileObject = _iid('{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}')  # marker: handler may be called from any thread
        done = kernel32.CreateEventW(None, True, False, None)
        result = {'hr': -1, 'punk': None}
        accepted = (bytes(IID_Handler), bytes(IID_IUnknown), bytes(IID_IAgileObject))

        def qi(this, riid, ppv):
            if bytes(riid.contents) in accepted:
                ppv[0] = this
                return 0
            ppv[0] = None
            return -2147467262  # E_NOINTERFACE

        def addref(this):
            return 1

        def release(this):
            return 1

        def completed(this, op):
            try:
                hr = ctypes.c_long(0)
                punk = c_void_p()
                _vtbl(c_void_p(op), 3, _GetActivateResult_T)(op, byref(hr), byref(punk))
                result['hr'], result['punk'] = hr.value, punk.value
            except Exception:
                log_exc('ActivateCompleted')
            kernel32.SetEvent(done)
            return 0

        vt = _HandlerVtbl(_QI_T(qi), _AR_T(addref), _AR_T(release), _AC_T(completed))
        handler = _Handler(ctypes.pointer(vt))
        self._keep = (vt, handler, qi, addref, release, completed)
        params = AUDIOCLIENT_ACTIVATION_PARAMS(1, AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(self.pid, 0))  # include process tree
        pv = PROPVARIANT()
        pv.vt = 0x41  # VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.addressof(params)
        op = c_void_p()
        hr = mmdevapi.ActivateAudioInterfaceAsync('VAD\\Process_Loopback', byref(IID_IAudioClient), byref(pv),
                                                  ctypes.addressof(handler), byref(op))
        if hr != 0:
            raise OSError('ActivateAudioInterfaceAsync failed: ' + _hrs(hr))
        if kernel32.WaitForSingleObject(done, 5000) != 0:
            raise OSError('process loopback activation timed out')
        if op:
            _vtbl(op, 2, _AR_T)(op)
        if result['hr'] != 0 or not result['punk']:
            raise OSError('process loopback activation result: ' + _hrs(result['hr']))
        client = c_void_p(result['punk'])
        cap = c_void_p()
        ev = None
        try:
            C = self.channels
            flags = 0x00020000 | 0x00040000  # LOOPBACK | EVENTCALLBACK
            pcm16 = False
            if C == 2:
                fmt_float = WAVEFORMATEX(3, 2, self.RATE, self.RATE * 8, 8, 32, 0)
                hr = _vtbl(client, 3, _Initialize_T)(client, 0, flags, 1000000, 0, byref(fmt_float), None)
            else:
                sub = GUID()
                ole32.CLSIDFromString('{00000003-0000-0010-8000-00AA00389B71}', byref(sub))  # IEEE float
                fmt_ext = WAVEFORMATEXTENSIBLE(0xFFFE, C, self.RATE, self.RATE * C * 4, C * 4, 32, 22, 32, CHANNEL_MASKS[C], sub)
                hr = _vtbl(client, 3, _Initialize_T)(client, 0, flags, 1000000, 0, cast(byref(fmt_ext), POINTER(WAVEFORMATEX)), None)
                if hr != 0:
                    log('process loopback: %d-channel format refused (%s), using stereo' % (C, _hrs(hr)))
                    C = self.channels = 2
                    fmt_float = WAVEFORMATEX(3, 2, self.RATE, self.RATE * 8, 8, 32, 0)
                    hr = _vtbl(client, 3, _Initialize_T)(client, 0, flags, 1000000, 0, byref(fmt_float), None)
            if hr != 0:
                log('process loopback: float format refused (%s), trying 16-bit PCM' % _hrs(hr))
                fmt_pcm = WAVEFORMATEX(1, 2, self.RATE, self.RATE * 4, 4, 16, 0)
                hr = _vtbl(client, 3, _Initialize_T)(client, 0, flags, 1000000, 0, byref(fmt_pcm), None)
                pcm16 = True
                C = self.channels = 2
            if hr != 0:
                raise OSError('IAudioClient.Initialize failed: ' + _hrs(hr))
            ev = kernel32.CreateEventW(None, False, False, None)
            hr = _vtbl(client, 13, _SetEventHandle_T)(client, ev)
            if hr != 0:
                raise OSError('SetEventHandle failed: ' + _hrs(hr))
            hr = _vtbl(client, 14, _GetService_T)(client, byref(IID_IAudioCaptureClient), byref(cap))
            if hr != 0 or not cap:
                raise OSError('GetService(IAudioCaptureClient) failed: ' + _hrs(hr))
            hr = _vtbl(client, 10, _Simple_T)(client)
            if hr != 0:
                raise OSError('IAudioClient.Start failed: ' + _hrs(hr))
            self.alive = True
            self.ready.set()
            log('Process loopback capturing pid %d (%s), %s, %d ch' % (self.pid, self.name, 'pcm16' if pcm16 else 'float32', C))
            get_next = _vtbl(cap, 5, _GetNextPacketSize_T)
            get_buf = _vtbl(cap, 3, _GetBuffer_T)
            rel = _vtbl(cap, 4, _ReleaseBuffer_T)
            while self.running:
                kernel32.WaitForSingleObject(ev, 100)
                while self.running:
                    n = wintypes.UINT(0)
                    if get_next(cap, byref(n)) != 0 or n.value == 0:
                        break
                    p = c_void_p()
                    frames = wintypes.UINT(0)
                    flags = wintypes.DWORD(0)
                    if get_buf(cap, byref(p), byref(frames), byref(flags), None, None) != 0:
                        break
                    if frames.value:
                        if (flags.value & 2) or not p.value:  # AUDCLNT_BUFFERFLAGS_SILENT
                            data = np.zeros((frames.value, C), np.float32)
                        elif pcm16:
                            raw = ctypes.string_at(p.value, frames.value * 4)
                            data = (np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0).reshape(-1, 2)
                        else:
                            raw = ctypes.string_at(p.value, frames.value * 4 * C)
                            data = np.frombuffer(raw, dtype=np.float32).reshape(-1, C).copy()
                        self.push(data)
                    rel(cap, frames.value)
            _vtbl(client, 11, _Simple_T)(client)
        finally:
            if cap:
                _vtbl(cap, 2, _AR_T)(cap)
            _vtbl(client, 2, _AR_T)(client)
            if ev:
                kernel32.CloseHandle(ev)
            kernel32.CloseHandle(done)


# ---------------------------------------------------------------------------
# Audio: WASAPI loopback of the default playback device, with automatic retry
# ---------------------------------------------------------------------------
class AudioEngine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buf = np.zeros((FFT_SIZE, 2), dtype=np.float32)
        self.gen = 0
        self.last_data = 0.0
        self.pa = None
        self.stream = None
        self.channels = 2
        self.rate = 48000
        self.device_name = ''
        self.endpoint_id = None
        self.state = 'starting'      # starting | live | retrying | restarting | demo
        self.error = ''
        self.opening = False
        self.last_attempt = 0.0
        self.last_probe = 0.0
        self.retry_delay = 1.5
        self.opened_count = 0
        self._want_restart = False
        self.demo = None             # DemoScene while the synthetic scene is running
        self.capture = 'device'      # device | process | demo
        self.ploop = None            # ProcessLoopback while capturing one app
        self.want_process = False
        self.process_pid = 0
        self.process_name = ''
        self.process_error = ''

    def set_process_target(self, pid: int, name: str) -> None:
        if pid and pid != self.process_pid:
            self.process_pid, self.process_name = pid, name
            if self.want_process and self.demo is None:
                self._want_restart = True

    def set_want_process(self, on: bool) -> None:
        self.want_process = bool(on)
        if self.demo is None:
            self._want_restart = True

    def _on_process_end(self, msg: str) -> None:
        log('Process loopback ended: ' + msg)
        self._want_restart = True

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self.opening or self.demo is not None:
            return
        self.opening = True
        self.last_attempt = time.perf_counter()
        threading.Thread(target=self._open, name='audio-open', daemon=True).start()

    def restart(self) -> None:
        if self.demo is not None:
            self.stop_demo()
        else:
            self._want_restart = True

    def shutdown(self) -> None:
        if self.demo is not None:
            self.demo.stop()
            self.demo = None
        self._close()

    def start_demo(self) -> None:
        if self.demo is not None:
            return
        self._close()
        self.rate = DemoScene.RATE
        self.channels = 2
        self.device_name = 'Demo: engine, footsteps and shots'
        self.error = ''
        self.state = 'demo'
        self.capture = 'demo'
        self.demo = DemoScene(self)
        self.demo.start()
        log('Demo scene started')

    def stop_demo(self) -> None:
        if self.demo is None:
            return
        self.demo.stop()
        self.demo = None
        self.state = 'restarting'
        self.start()
        log('Demo scene stopped; reopening loopback')

    def _close(self) -> None:
        pl = self.ploop
        self.ploop = None
        if pl is not None:
            pl.stop()
        st, pa = self.stream, self.pa
        self.stream = None
        self.pa = None
        if st is not None:
            try:
                st.stop_stream()
            except Exception:
                pass
            try:
                st.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass

    @staticmethod
    def _find_loopback(pa):
        info = None
        try:
            info = pa.get_default_wasapi_loopback()
        except Exception as e:
            log('get_default_wasapi_loopback: %r' % e)
        if info is None:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            out = pa.get_device_info_by_index(wasapi['defaultOutputDevice'])
            for d in pa.get_loopback_device_info_generator():
                if out['name'] in d['name']:
                    info = d
                    break
        return info

    def _open(self) -> None:
        try:
            self._close()
            if self.want_process and self.process_pid and process_alive(self.process_pid):
                mix_ch = default_mix_channels()
                pl = ProcessLoopback(self.process_pid, self.process_name, self.push, self._on_process_end,
                                     channels=mix_ch if mix_ch in SPEAKER_LAYOUTS else 2)
                if pl.start():
                    self.ploop = pl
                    self.capture = 'process'
                    self.rate = pl.RATE
                    self.channels = pl.channels
                    self.device_name = '%s only' % self.process_name
                    self.endpoint_id = None
                    self.error = ''
                    self.process_error = ''
                    self.retry_delay = 1.5
                    self.opened_count += 1
                    self.state = 'live'
                    return
                self.process_error = pl.error or 'unknown error'
                log('Process loopback unavailable (%s); falling back to the device' % self.process_error)
            self.capture = 'device'
            pa = pyaudio.PyAudio()
            info = self._find_loopback(pa)
            if info is None:
                pa.terminate()
                raise RuntimeError('No WASAPI loopback device found. Is a playback device enabled?')
            ch = max(1, int(info.get('maxInputChannels', 2)))
            rate = int(info.get('defaultSampleRate', 48000))
            self.channels = ch
            stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                             input_device_index=int(info['index']), frames_per_buffer=512,
                             stream_callback=self._callback)
            self.pa, self.stream = pa, stream
            self.rate = rate
            self.device_name = str(info.get('name', '')).replace(' [Loopback]', '')
            self.endpoint_id = default_render_endpoint_id()
            self.mix_channels = ch
            self.error = ''
            self.retry_delay = 1.5
            self.opened_count += 1
            self.state = 'live'
            log('Loopback open: %s, %d Hz, %d ch' % (self.device_name, rate, ch))
        except Exception as e:
            log_exc('open loopback')
            self.error = str(e) or e.__class__.__name__
            self._close()
            self.retry_delay = min(self.retry_delay * 1.5, 8.0)
            self.state = 'retrying'
        finally:
            self.last_attempt = time.perf_counter()
            self.opening = False

    # -- data ----------------------------------------------------------------
    def push(self, st) -> None:
        """Append (n, C) float32 samples to the analysis window (C = 2, or the device's 4/6/8 channels)."""
        n = st.shape[0]
        if not n:
            return
        with self.lock:
            if self.buf.shape[1] != st.shape[1]:
                self.buf = np.zeros((FFT_SIZE, st.shape[1]), dtype=np.float32)
            if n >= FFT_SIZE:
                self.buf = np.array(st[-FFT_SIZE:], dtype=np.float32)
            else:
                self.buf = np.concatenate((self.buf[n:], st)).astype(np.float32, copy=False)
            self.gen += 1
            self.last_data = time.perf_counter()

    def _callback(self, in_data, frame_count, time_info, status):
        try:
            ch = self.channels
            a = np.frombuffer(in_data, dtype=np.float32)
            n = a.size // ch
            if n:
                a = a[:n * ch].reshape(n, ch)
                if ch in SPEAKER_LAYOUTS:
                    self.push(a)                      # surround: keep every channel
                else:
                    self.push(a[:, :2] if ch >= 2 else np.repeat(a, 2, axis=1))
        except Exception:
            log_exc('audio callback')
        return (None, pyaudio.paContinue)

    def snapshot(self):
        with self.lock:
            return self.buf, self.gen, self.last_data

    # -- watchdog (called from the main loop) --------------------------------
    def tick(self, now: float) -> None:
        if self.opening or self.demo is not None:
            return
        if self._want_restart:
            self._want_restart = False
            self.state = 'restarting'
            self.start()
            return
        if self.state == 'live' and self.capture == 'process':
            pl = self.ploop
            if pl is None or not pl.alive:
                self.state = 'restarting'
                self.start()
                return
            if now - self.last_probe > 1.0:
                self.last_probe = now
                if not process_alive(self.process_pid):
                    log('Captured process %s exited; back to the device' % self.process_name)
                    self.process_pid = 0
                    self.state = 'restarting'
                    self.start()
            return
        if self.state == 'live':
            st = self.stream
            try:
                active = st is not None and st.is_active()
            except Exception:
                active = False
            if not active:
                log('Audio stream stopped (device removed or changed); will retry')
                self.state = 'retrying'
                self.error = 'Audio device stopped'
                self.last_attempt = now
                self.retry_delay = 1.0
                return
            if now - self.last_probe > 1.0:
                self.last_probe = now
                eid = default_render_endpoint_id()
                if eid is not None and self.endpoint_id is not None and eid != self.endpoint_id:
                    log('Default playback device changed; restarting loopback')
                    self.state = 'restarting'
                    self.start()
                elif self.want_process and self.process_pid and process_alive(self.process_pid) and not self.process_error:
                    self.state = 'restarting'   # a target appeared after the device stream opened
                    self.start()
        elif self.state in ('retrying', 'starting'):
            if now - self.last_attempt >= self.retry_delay:
                self.start()


# ---------------------------------------------------------------------------
# Demo: a synthetic stereo scene fed straight into the analyser (mirrors buildDemo()
# in the web version): an engine idling and drifting, a walker circling, an occasional shot.
# ---------------------------------------------------------------------------
class DemoScene:
    RATE = 48000
    CHUNK = 512

    def __init__(self, engine, realtime: bool = True) -> None:
        self.engine = engine
        self.realtime = realtime     # False: generate as fast as possible (offline rendering)
        self.running = False
        self.thread = None
        rng = np.random.default_rng(7)
        noise = rng.uniform(-1.0, 1.0, self.RATE * 2).astype(np.float32)
        self.beds = {
            'step_a': self._shape(noise, lambda f: self._bp(f, 900.0, 1.2)),
            'step_b': self._shape(noise, lambda f: self._bp(f, 1400.0, 1.2)),
            'shot_lo': self._shape(noise, lambda f: self._lp(f, 400.0)),
            'shot_hi': self._shape(noise, lambda f: self._bp(f, 2500.0, 0.8)),
        }
        self.rng = rng

    @staticmethod
    def _bp(f, f0, q):
        r = f / f0 - f0 / np.maximum(f, 1e-6)
        return 1.0 / np.sqrt(1.0 + (q * r) ** 2)

    @staticmethod
    def _lp(f, fc):
        return 1.0 / np.sqrt(1.0 + (f / fc) ** 4)

    def _shape(self, x, mag):
        X = np.fft.rfft(x)
        f = np.fft.rfftfreq(x.size, 1.0 / self.RATE)
        return np.fft.irfft(X * mag(f), n=x.size).astype(np.float32)

    @staticmethod
    def _pan_gains(pan):
        x = (clamp(pan, -1.0, 1.0) + 1.0) * 0.5
        return math.cos(x * math.pi / 2), math.sin(x * math.pi / 2)

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._run, name='demo', daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.running = False

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            log_exc('demo scene')

    def render(self, seconds: float) -> None:
        """Offline: generate `seconds` of the scene synchronously into engine.push (no pacing)."""
        self.running = True
        self._loop(max_samples=int(seconds * self.RATE))
        self.running = False

    def _loop(self, max_samples=None) -> None:
        RATE, CHUNK = self.RATE, self.CHUNK
        # engine: sawtooth 48 Hz + square 96.5 Hz through a 260 Hz low-pass, as harmonic sums
        def lp260(f):
            return 1.0 / math.sqrt((1 - (f / 260.0) ** 2) ** 2 + (f / 260.0) ** 2)
        tones = []
        for k in range(1, 9):
            tones.append((48.0 * k, (2 / math.pi) * ((-1) ** (k + 1)) / k * lp260(48.0 * k)))
        for k in (1, 3, 5):
            tones.append((96.5 * k, (4 / math.pi) / k * lp260(96.5 * k)))
        bursts = []  # [bed, start_sample, offset, dur_samples, gain, pan]
        next_step, step_i, next_shot = 0.4, 0, 1.6
        t0 = time.perf_counter()
        n = 0
        idx = np.arange(CHUNK, dtype=np.float64)
        while self.running and (max_samples is None or n < max_samples):
            ts = (n + idx) / RATE
            tsec = n / RATE
            eng = np.zeros(CHUNK)
            for f, g in tones:
                eng += g * np.sin(2 * math.pi * f * ts)
            eng *= 0.1
            gl, gr = self._pan_gains(math.sin(tsec * 0.13) * 0.9)
            L, R = eng * gl, eng * gr
            while tsec >= next_step:
                bed = 'step_a' if step_i % 2 == 0 else 'step_b'
                step_i += 1
                bursts.append([bed, n, int(self.rng.uniform(0, 1.5) * RATE), int(0.14 * RATE), 1.8,
                               math.sin(tsec * 0.42 + 1.3) * 0.95])
                next_step += 0.43
            while tsec >= next_shot:
                pan = self.rng.uniform(-1, 1)
                bursts.append(['shot_lo', n, int(self.rng.uniform(0, 1.5) * RATE), int(0.35 * RATE), 1.6, pan])
                bursts.append(['shot_hi', n, int(self.rng.uniform(0, 1.5) * RATE), int(0.2 * RATE), 0.9, pan])
                next_shot += 3.6
            alive = []
            for b in bursts:
                bed, start, off, dur, gain, pan = b
                rel0 = n - start
                if rel0 >= dur:
                    continue
                alive.append(b)
                cnt = min(CHUNK, dur - rel0)
                rel = rel0 + np.arange(cnt)
                tt = rel / RATE
                atk = np.minimum(1.0, tt / 0.006)
                dec = (0.0005 / gain) ** np.clip((tt - 0.006) / max(1e-3, dur / RATE - 0.006), 0, 1)
                env = gain * atk * dec
                src = np.take(self.beds[bed], (off + rel) % (RATE * 2))
                gl, gr = self._pan_gains(pan)
                L[:cnt] += src * env * gl
                R[:cnt] += src * env * gr
            bursts = alive
            st = np.stack((np.clip(L, -1, 1), np.clip(R, -1, 1)), axis=1).astype(np.float32)
            self.engine.push(st)
            n += CHUNK
            if self.realtime:
                d = t0 + n / RATE - time.perf_counter()
                if d > 0:
                    time.sleep(d)


# ---------------------------------------------------------------------------
# Analysis: band-based stereo direction estimate (same maths as the web analyse())
# ---------------------------------------------------------------------------
class Cue:
    __slots__ = ('t', 'a', 'r', 'n', 'b')

    def __init__(self, t, a, r, n, b=0):
        self.t, self.a, self.r, self.n, self.b = t, a, r, n, b


class Analyser:
    def __init__(self, engine: AudioEngine) -> None:
        self.engine = engine
        self.window = np.blackman(FFT_SIZE).astype(np.float32)
        self.bins = FFT_SIZE // 2 + 1
        self.sL = np.zeros(self.bins, dtype=np.float64)
        self.sR = np.zeros(self.bins, dtype=np.float64)
        self.sM = np.zeros(self.bins, dtype=np.float64)   # |L+R|/2 : what both ears share
        self.sS = np.zeros(self.bins, dtype=np.float64)   # |L-R|/2 : what differs between the ears
        self.sCh = None                                   # (bins, C) per-channel magnitudes in surround
        self.meterL = 0.0
        self.meterR = 0.0
        self.last_gen = -1
        self.rate = 0
        self.bands = []
        self.cues: list[Cue] = []
        self.trail: list[Cue] = []
        self.markers: list[list] = []                     # [t, angle, band, strength]
        self.marker_total = 0
        self.dominant: Cue | None = None
        self.cue_total = 0
        self.surround = False
        self.channels = 2
        # per-band temporal state: smoothed angle (unit vector) / level, slow background, onset reference
        self.band_ax = np.ones(BAND_N)
        self.band_ay = np.zeros(BAND_N)
        self.band_level = np.zeros(BAND_N)
        self.band_bg = np.zeros(BAND_N)
        self.band_ref = np.zeros(BAND_N)
        self.band_last = np.full(BAND_N, -1e9)
        self.band_mark = np.full(BAND_N, -1e9)
        self.dom_band = -1
        self.dom_ax = 1.0
        self.dom_ay = 0.0
        self.dom_last = -1e9
        self.last_t = 0.0

    def _compute_bands(self, rate: int) -> None:
        self.rate = rate
        hz_per_bin = rate / 2.0 / (self.bins - 1)
        bands = []
        for i in range(BAND_N):
            f0 = BAND_LO * (BAND_HI / BAND_LO) ** (i / BAND_N)
            f1 = BAND_LO * (BAND_HI / BAND_LO) ** ((i + 1) / BAND_N)
            b0 = max(1, int(math.floor(f0 / hz_per_bin)))
            b1 = max(b0 + 1, int(math.ceil(f1 / hz_per_bin)))
            bands.append((b0, min(b1, self.bins - 1)))
        self.bands = bands

    @staticmethod
    def _rms_norm(x) -> float:
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        db = 20.0 * math.log10(rms + 1e-9)
        return clamp((db + 60.0) / 60.0, 0.0, 1.0)

    def reset(self) -> None:
        self.cues.clear()
        self.trail.clear()
        self.markers.clear()
        self.dominant = None
        self.sL[:] = 0
        self.sR[:] = 0
        self.sM[:] = 0
        self.sS[:] = 0
        self.sCh = None
        self.meterL = self.meterR = 0.0
        self.band_ax[:] = 1
        self.band_ay[:] = 0
        self.band_level[:] = 0
        self.band_bg[:] = 0
        self.band_ref[:] = 0
        self.band_last[:] = -1e9
        self.band_mark[:] = -1e9
        self.dom_band = -1
        self.dom_last = -1e9

    def analyse(self, t: float, S: dict) -> None:
        eng = self.engine
        if eng.rate != self.rate:
            self._compute_bands(eng.rate)
        buf, gen, last_data = eng.snapshot()
        C = buf.shape[1] if buf.ndim == 2 else 1
        layout = SPEAKER_LAYOUTS.get(C) if S.get('surround', 'auto') != 'off' else None
        surround = layout is not None
        if surround != self.surround or C != self.channels:
            self.surround, self.channels = surround, C
            self.reset()
        gain, gate = effective_gain_gate(S)
        if gen != self.last_gen:
            self.last_gen = gen
            x = buf * np.float32(gain)
            if surround:
                mags = np.abs(np.fft.rfft(x * self.window[:, None], axis=0)) / FFT_SIZE   # (bins, C)
                if self.sCh is None or self.sCh.shape != mags.shape:
                    self.sCh = mags.astype(np.float64)
                else:
                    self.sCh = SMOOTHING * self.sCh + (1.0 - SMOOTHING) * mags
                self.sL, self.sR = self.sCh[:, 0], self.sCh[:, 1]
            else:
                XL = np.fft.rfft(x[:, 0] * self.window) / FFT_SIZE
                XR = np.fft.rfft(x[:, 1] * self.window) / FFT_SIZE
                self.sL = SMOOTHING * self.sL + (1.0 - SMOOTHING) * np.abs(XL)
                self.sR = SMOOTHING * self.sR + (1.0 - SMOOTHING) * np.abs(XR)
                self.sM = SMOOTHING * self.sM + (1.0 - SMOOTHING) * (np.abs(XL + XR) * 0.5)
                self.sS = SMOOTHING * self.sS + (1.0 - SMOOTHING) * (np.abs(XL - XR) * 0.5)
            self.meterL += (self._rms_norm(x[:, 0]) - self.meterL) * 0.35
            self.meterR += (self._rms_norm(x[:, 1]) - self.meterR) * 0.35
        elif t - last_data > 0.25:
            # No packets arrive while the device is idle: let the picture settle to silence.
            self.sL *= 0.6
            self.sR *= 0.6
            self.sM *= 0.6
            self.sS *= 0.6
            if self.sCh is not None:
                self.sCh *= 0.6
            self.meterL *= 0.65
            self.meterR *= 0.65
            if self.meterL < 0.001:
                self.meterL = 0.0
            if self.meterR < 0.001:
                self.meterR = 0.0

        rb = S['radiusBoost'] / 20.0
        sm = clamp(S['smoothing'], 0.0, 1.0)
        wdb = width_db(S)
        adaptive = S['adaptive']
        k_cs = clamp(int(S.get('centre', 5)), 0, 10) / 10.0
        markers_on = bool(S.get('markers', True))
        dt = clamp(t - self.last_t, 0.0, 0.1) if self.last_t else 1.0 / 60.0
        self.last_t = t
        # Smoothing: time constants for angle and level (0 -> raw, 1 -> ~250 ms / ~100 ms)
        ka = 1.0 if sm <= 0 else 1.0 - math.exp(-dt / (0.03 + 0.22 * sm))
        kl = 1.0 if sm <= 0 else 1.0 - math.exp(-dt / (0.02 + 0.08 * sm))
        k_bg_up = 1.0 - math.exp(-dt / 2.0)    # steady sounds become "background" over ~2 s
        k_bg_down = 1.0 - math.exp(-dt / 0.5)  # and the background drops quickly when they stop
        k_ref = 1.0 - math.exp(-dt / 0.15)     # short-term reference for onset (marker) detection

        cands = []
        for b, (b0, b1) in enumerate(self.bands):
            if surround:
                lv = self.sCh[b0:b1, :].mean(axis=0)
                vx = vy = tot = 0.0
                for c, deg in enumerate(layout):
                    if deg is None or c >= lv.size:
                        continue
                    th = math.radians(deg)
                    vx += lv[c] * math.cos(th)
                    vy += lv[c] * math.sin(th)
                    tot += lv[c]
                a_raw = math.atan2(vy, vx) if tot > 1e-9 else 0.0
                lsum = float(lv.sum())
                sr_b = 1.0 - clamp(lv[2] / lsum, 0.0, 1.0) if (len(layout) >= 6 and lsum > 1e-12) else 1.0
                tot *= 0.5
            else:
                mL, mR = self.sL[b0:b1], self.sR[b0:b1]
                mM, mS = self.sM[b0:b1], self.sS[b0:b1]
                lvl = mL + mR
                tot = float(lvl.mean()) * 0.5
                # off-centredness per bin: 0 = identical in both ears, 1 = only in one ear
                sr = np.clip(2.0 * mS / (mM + mS + 1e-12), 0.0, 1.0)
                lsum = float(lvl.sum())
                sr_b = float((lvl * sr).sum() / lsum) if lsum > 1e-12 else 0.0
                # per-bin voting: bins carrying off-centre sound decide the direction, bins that only
                # carry the shared (centred) sound do not dilute it
                w = lvl * sr
                wsum = float(w.sum())
                ild_i = np.clip(20.0 * np.log10((mR + 1e-12) / (mL + 1e-12)), -30.0, 30.0)
                if wsum > 1e-12 and sr_b > 0.05:
                    ild = float((w * ild_i).sum() / wsum)
                else:
                    ild = 20.0 * math.log10((float(mR.mean()) + 1e-12) / (float(mL.mean()) + 1e-12))
                # Stereo width: +-wdb dB counts as fully sideways
                a_raw = clamp(ild / wdb, -1.0, 1.0) * (math.pi / 2.0)
            if tot <= 1e-9:
                n_raw = 0.0
            else:
                db = 20.0 * math.log10(tot)
                n_raw = clamp((db - FLOOR_DB) / (CEIL_DB - FLOOR_DB), 0.0, 1.0)
            # Centre suppression: sound shared equally by both ears (own engine, music, narration) shrinks
            n_raw *= (1.0 - k_cs * (1.0 - sr_b))
            # Onset: a jump above the short-term reference leaves a marker on the rim
            ref = self.band_ref[b]
            flux = n_raw - ref
            self.band_ref[b] = ref + (n_raw - ref) * k_ref
            if markers_on and flux > 0.12 and n_raw > gate + 0.04 and t - self.band_mark[b] > 0.25:
                self.band_mark[b] = t
                self.markers.append([t, a_raw, b, clamp(flux * 2.5, 0.3, 1.0)])
                self.marker_total += 1
            bg = self.band_bg[b]
            bg += (n_raw - bg) * (k_bg_up if n_raw > bg else k_bg_down)
            self.band_bg[b] = bg
            # Adaptive: a sound that has been steady for a couple of seconds shows at half
            # level, so it stays visible while anything new or changing stands out.
            n_eff = (n_raw - 0.5 * bg) if adaptive else n_raw
            if n_eff <= gate:
                continue
            n = clamp((n_eff - gate) / (1.0 - gate), 0.0, 1.0)
            cx, cy = math.cos(a_raw), math.sin(a_raw)
            if t - self.band_last[b] > 0.25:
                # band was quiet: start from the fresh values rather than sweeping from stale ones
                self.band_ax[b], self.band_ay[b] = cx, cy
                self.band_level[b] = n
            else:
                self.band_ax[b] += (cx - self.band_ax[b]) * ka
                self.band_ay[b] += (cy - self.band_ay[b]) * ka
                self.band_level[b] += (n - self.band_level[b]) * kl
            self.band_last[b] = t
            cands.append((float(self.band_level[b]), b))

        if not cands:
            return
        cands.sort(reverse=True)
        cands = cands[:max(1, int(S['maxCues']))]
        best_n, best_b = cands[0]
        for n, b in cands:
            a = math.atan2(self.band_ay[b], self.band_ax[b])
            self.cues.append(Cue(t, a, clamp(n * rb, 0.0, 1.0), n, b))
            self.cue_total += 1

        # Dominant cue with hysteresis: keep the current band unless another is clearly louder.
        dom = self.dom_band
        keep = any(b == dom and n >= best_n * 0.85 for n, b in cands)
        if not keep:
            dom = best_b
        self.dom_band = dom
        ax, ay = float(self.band_ax[dom]), float(self.band_ay[dom])
        if t - self.dom_last > 0.25 or sm <= 0:
            self.dom_ax, self.dom_ay = ax, ay
        else:
            self.dom_ax += (ax - self.dom_ax) * ka
            self.dom_ay += (ay - self.dom_ay) * ka
        self.dom_last = t
        n_dom = float(self.band_level[dom])
        self.dominant = Cue(t, math.atan2(self.dom_ay, self.dom_ax), clamp(n_dom * rb, 0.0, 1.0), n_dom, dom)
        self.trail.append(self.dominant)

    def prune(self, t: float, life: float) -> None:
        cues = self.cues
        i = 0
        while i < len(cues) and t - cues[i].t > life:
            i += 1
        if i:
            del cues[:i]
        if len(cues) > 4000:
            del cues[:len(cues) - 4000]
        tl = life * 2.5
        trail = self.trail
        j = 0
        while j < len(trail) and t - trail[j].t > tl:
            j += 1
        if j:
            del trail[:j]
        mk = self.markers
        k = 0
        while k < len(mk) and t - mk[k][0] > MARKER_LIFE:
            k += 1
        if k:
            del mk[:k]
        if len(mk) > 200:
            del mk[:len(mk) - 200]


# ---------------------------------------------------------------------------
# The window + renderer
# ---------------------------------------------------------------------------
HELP_KEYS = [
    ('Tab', 'Tuning panel: pick a row with the arrow keys, adjust with Left / Right'),
    (',  .', 'Sensitivity down / up (1 to 10). Ctrl+Shift+, and . from any app'),
    ('1 .. 5', 'Petals / Dots / Comet / Solid Fan / Single Line'),
    ('B', 'Boost: visual-only push of cues toward the rim'),
    ('[  ]', 'Trail / line life shorter or longer'),
    ('-  =', 'Opacity down or up'),
    ('P', 'Pin on top (always-on-top window)'),
    ('O', 'Overlay: click-through, no panel. Ctrl+Shift+O: back, from any app'),
    ('R', 'Restart the audio stream'),
    ('D', 'Demo scene: engine, footsteps and shots (no game needed)'),
    ('S  A  C', 'Smoothing level / fade steady sounds / cues per frame'),
    ('G  M  T', 'Guides / level meters / next colour theme'),
    ('?  F1', 'This help (Space: next page)'),
    ('Esc', 'Close panels, leave overlay'),
    ('Ctrl+Q', 'Quit'),
    ('Ctrl+Shift+', '1-5, B, O, P, R, [ ], - = do the same from any app'),
    ('Right-click', 'All settings. Tray icon: left-click brings the panel back'),
]
HELP_TEXT = [
    ('h', 'Reading the scope'),
    ('p', 'The arc is forward. Angle is left or right, radius is loudness. The white arrow, or the Single Line pointer, marks the newest cue.'),
    ('h', 'Tuning hints'),
    ('p', 'Sounds beside you show too close to the middle? Raise Stereo width in the Tab panel; lower it if everything sticks to the sides. Your own engine or music swamping the scope? Raise Centre suppression.'),
    ('p', 'Colours: warm = low pitch (engines, rumble), theme colour = middle, light = high pitch (steps, hits). Rim dots mark sudden sounds for a few seconds. Right-click > Game profiles saves a tuning per game and switches it automatically.'),
    ('p', 'Jumpy or all over the place? Raise Smoothing (S), lower Cues per frame (C), or raise the noise gate. A constant hum taking all the space? Try "Fade steady sounds" (A); turn it off if the scope goes empty.'),
    ('p', 'Looks streaky? Lower Life. Cues vanish too fast? Raise Life. Footsteps look tiny? Turn Boost on. Scope sticks to the rim? Turn Boost off or raise the noise gate.'),
    ('p', 'Everything looks small? Raise Radius boost or Input gain. No movement? The game must play through the Windows default playback device; press R after changing devices.'),
    ('p', 'Pinned window hidden by the game? Use borderless windowed mode. Exclusive fullscreen covers every overlay.'),
]
WELCOME_TEXT = [
    ('p', 'EchoScope turns your game\'s stereo audio into an on-screen radar. Angle shows left or right, radius shows loudness, and the white arrow marks the newest cue. Footsteps, engines, shots and vehicles become something you can read at a glance.'),
    ('n', '1.  It is already listening to your default playback device. Play a game or any sound and watch the scope.'),
    ('n', '2.  Drag the panel where you want it and resize it from the edges. Press O for Overlay: the radar turns click-through and floats over borderless-windowed games. Ctrl+Shift+O brings the panel back from any app.'),
    ('n', '3.  Press Tab for the tuning panel: Sensitivity, Smoothing, Cues per frame and more, adjusted live with the arrow keys. Tap 1 to 5 to compare visual modes. Right-click (or the tray icon) for every setting, ? for help.'),
    ('m', 'Nothing is recorded or uploaded. Audio is analysed locally and never leaves this PC. This is not a cheat: it only listens to the audio you already hear.'),
]

MENU_MODE = 100
MENU_BOOST, MENU_PIN, MENU_OVERLAY, MENU_RESTART, MENU_GUIDES, MENU_METERS = 110, 111, 112, 113, 114, 115
MENU_DEMO, MENU_GLOBALHK, MENU_STARTUP, MENU_STARTOVERLAY, MENU_ADAPTIVE, MENU_TUNE = 116, 117, 118, 119, 126, 127
MENU_SMOOTH, MENU_MAXCUES, MENU_SENS, MENU_STWIDTH = 340, 360, 380, 400
SENS_CHOICES = list(range(1, 11))

# Rows of the Tab tuning panel: (settings key, label, kind, lo, hi, step, format, choices)
MENU_SAVE_PROFILE, MENU_DEL_PROFILE, MENU_AUTOPROFILE, MENU_AUTOHIDE, MENU_PROCCAP, MENU_SURROUND = 128, 129, 130, 131, 132, 133
MENU_SIZE = 420
TUNE_ROWS = [
    ('sensitivity', 'Sensitivity', 'int', 1, 10, 1, lambda v: '%d / 10' % v, None),
    ('width', 'Stereo width', 'int', 1, 10, 1, lambda v: '%d / 10' % v, None),
    ('centre', 'Centre suppression', 'int', 0, 10, 1, lambda v: ('Off' if v == 0 else '%d / 10' % v), None),
    ('smoothing', 'Smoothing', 'choice', 0, 0, 0, lambda v: SMOOTHING_NAMES.get(v, '%g' % v), SMOOTHING_CHOICES),
    ('maxCues', 'Cues per frame', 'choice', 0, 0, 0, lambda v: '%d' % v, MAXCUES_CHOICES),
    ('gate', 'Noise gate', 'float', 0.0, 0.6, 0.05, lambda v: '%.2f' % v, None),
    ('radiusBoost', 'Radius boost', 'float', 5, 25, 1, lambda v: '%d' % v, None),
    ('life', 'Trail life', 'float', 0.1, 2.0, 0.05, lambda v: '%.2f s' % v, None),
    ('lineWidth', 'Line width', 'float', 1, 8, 0.5, lambda v: '%g' % v, None),
    ('opacity', 'Opacity', 'float', 0.2, 1.0, 0.05, lambda v: '%d%%' % round(v * 100), None),
    ('boost', 'Boost', 'bool', 0, 1, 1, lambda v: 'On' if v else 'Off', None),
    ('adaptive', 'Fade steady sounds', 'bool', 0, 1, 1, lambda v: 'On' if v else 'Off', None),
    ('colourByPitch', 'Colour by pitch', 'bool', 0, 1, 1, lambda v: 'On' if v else 'Off', None),
    ('markers', 'Event markers', 'bool', 0, 1, 1, lambda v: 'On' if v else 'Off', None),
    ('fullCircle', 'Full circle always', 'bool', 0, 1, 1, lambda v: 'On' if v else 'Auto', None),
]
MENU_FULLCIRCLE, MENU_COLOUR, MENU_MARKERS = 134, 135, 136
MENU_CENTRE = 440
CENTRE_CHOICES = list(range(0, 11))
MENU_HELP, MENU_WELCOME, MENU_OPEN_SETTINGS, MENU_OPEN_LOG, MENU_RESET, MENU_QUIT = 120, 121, 122, 123, 124, 125
MENU_THEME, MENU_LIFE, MENU_OPACITY, MENU_WIDTH, MENU_RADIUS, MENU_GATE, MENU_GAIN = 200, 220, 240, 260, 280, 300, 320
LIFE_CHOICES = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0]
OPACITY_CHOICES = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
WIDTH_CHOICES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
RADIUS_CHOICES = [5, 10, 15, 20, 25]
GATE_CHOICES = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.6]
GAIN_CHOICES = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]


class App:
    def __init__(self, stats_path: str | None = None) -> None:
        self.S = load_settings()
        self.engine = AudioEngine()
        self.an = Analyser(self.engine)
        self.hwnd = None
        self.hinst = kernel32.GetModuleHandleW(None)
        self.dpr = 1.0
        self.W = self.H = 0
        self.hdc = None
        self.hbmp = None
        self.hbmp_old = None
        self.surface = None
        self.surface_buf = None
        self.surface_own = None
        self.bits = None
        self.overlay = False
        self.active = True
        self.running = True
        self.modal = 0               # >0 while a Win32 modal loop (move/size/menu) runs
        self.panel = None            # None | 'welcome' | 'help' | 'help2' | 'tune' | 'menu'
        self.tune_row = 0
        self.tune_rows = []          # (y0, y1, index, track_x, track_w) for mouse hits, filled while drawing
        self.panel_rect = None       # (x, y, w, h) of the open side panel, for hit-testing
        self.menu_path = []          # labels of the opened groups
        self.menu_sels = [0]         # selected row per level
        self.menu_scroll = 0
        self.menu_rows = []          # (y0, y1, index) of the drawn rows
        self.menu_head = None        # (y0, y1) of the "< Back" header
        # foreground app tracking (profiles, auto-hide, per-app capture)
        self.my_pid = kernel32.GetCurrentProcessId()
        self.fg_exe = ''
        self.fg_pid = 0
        self.fg_self = True
        self.fg_last = 0.0
        self.last_app_exe = ''
        self.last_app_pid = 0
        self.active_profile = None
        self.base_tuning = None
        self.visible = True
        self.toast_text = ''
        self.toast_until = 0.0
        self.last_render = 0.0
        self.next_frame = 0.0
        self.geom_dirty = False
        self.hotkey_ok = False
        self.hotkeys_registered = 0
        self.tray_added = False
        self.tray_icon = None
        self.msg_taskbar_created = 0
        self.stats_path = stats_path
        self.stats_next = 0.0
        self.frames = 0
        self.fps = 0.0
        self._fps_t = 0.0
        self._fps_n = 0
        self._icons = []
        self._wndproc = WNDPROC(self._wnd_proc)

    # -- window creation ------------------------------------------------------
    def create(self) -> None:
        try:
            user32.SetProcessDpiAwarenessContext.argtypes = [c_ssize_t]
            user32.SetProcessDpiAwarenessContext(-4)  # PER_MONITOR_AWARE_V2
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
        try:
            winmm.timeBeginPeriod(1)
        except Exception:
            pass
        try:
            shell32.SetCurrentProcessExplicitAppUserModelID('EchoScope.Radar')
        except Exception:
            pass
        try:
            # Dark popup menus (Windows 10 1903+): uxtheme ordinal 135 = SetPreferredAppMode(ForceDark), 136 = FlushMenuThemes
            uxtheme = ctypes.WinDLL('uxtheme')
            set_mode = uxtheme[135]
            set_mode.restype, set_mode.argtypes = c_int, [c_int]
            set_mode(2)
            uxtheme[136]()
        except Exception:
            pass

        big, small = wintypes.HICON(), wintypes.HICON()
        try:
            shell32.ExtractIconExW(sys.executable, 0, byref(big), byref(small), 1)
        except Exception:
            pass
        self.tray_icon = small if small else (big if big else user32.LoadIconW(None, wintypes.LPCWSTR(IDI_APPLICATION)))
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self.hinst
        wc.hIcon = big
        wc.hIconSm = small
        wc.hCursor = user32.LoadCursorW(None, wintypes.LPCWSTR(IDC_ARROW))
        wc.hbrBackground = None
        wc.lpszClassName = 'EchoScopeRadar'
        if not user32.RegisterClassExW(byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())

        ex = WS_EX_LAYERED | WS_EX_APPWINDOW | (WS_EX_TOPMOST if self.S['pinned'] else 0)
        style = WS_POPUP | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX
        x, y, w, h = self._initial_geometry()
        self.hwnd = user32.CreateWindowExW(ex, 'EchoScopeRadar', APP_NAME, style, x, y, w, h, None, None, self.hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._update_dpr()
        if self.S['window'] is None:
            # First run: size for this monitor's DPI and centre on the primary work area.
            w, h = int(460 * self.dpr), int(320 * self.dpr)
            x, y = self._centre_on_primary(w, h)
            user32.SetWindowPos(self.hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
        self._ensure_surface()
        self.render(time.perf_counter())
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.SetForegroundWindow(self.hwnd)
        user32.SetTimer(self.hwnd, TIMER_FRAME, 8, None)
        self.msg_taskbar_created = user32.RegisterWindowMessageW('TaskbarCreated')
        self._tray_add()
        self._register_hotkeys()
        if self.S['startOverlay']:
            self.set_overlay(True)
        elif not self.S['hideWelcome']:
            self.panel = 'welcome'
        if self.S['processCapture']:
            self.engine.want_process = True   # target arrives with the first game in front
        if self.engine.state == 'starting' and not self.engine.opening:
            self.engine.start()   # (the first render above may already have started it)

    # -- tray icon ------------------------------------------------------------
    def _tray_data(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.tray_icon
        nid.szTip = 'EchoScope: left-click shows the panel, right-click for settings'
        return nid

    def _tray_add(self) -> None:
        try:
            nid = self._tray_data()
            self.tray_added = bool(shell32.Shell_NotifyIconW(NIM_ADD, byref(nid)))
            if not self.tray_added:
                log('Shell_NotifyIcon add failed')
        except Exception:
            log_exc('tray add')

    def _tray_remove(self) -> None:
        if self.tray_added:
            try:
                nid = self._tray_data()
                shell32.Shell_NotifyIconW(NIM_DELETE, byref(nid))
            except Exception:
                pass
            self.tray_added = False

    def _tray_event(self, ev: int) -> None:
        if ev == WM_LBUTTONUP:
            self.show_panel()
        elif ev == WM_LBUTTONDBLCLK:
            self.set_overlay(not self.overlay)
        elif ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
            self.show_panel()
            if self.panel != 'menu':
                self.show_menu()

    def show_panel(self) -> None:
        """Bring the (non-overlay) panel to the front with keyboard focus."""
        if self.overlay:
            self.set_overlay(False)
        if not self.visible:
            self.visible = True
            user32.ShowWindow(self.hwnd, SW_SHOW)
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
        user32.SetForegroundWindow(self.hwnd)

    # -- global hotkeys -----------------------------------------------------------
    def _register_hotkeys(self) -> None:
        self._unregister_hotkeys()
        if not self.S['globalHotkeys']:
            return
        failed = []
        for hid, vk, name in GLOBAL_HOTKEYS:
            if user32.RegisterHotKey(self.hwnd, hid, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, vk):
                self.hotkeys_registered += 1
            else:
                failed.append(name)
        self.hotkey_ok = 'overlay' not in failed
        if failed:
            log('Global hotkeys already taken by another app: ' + ', '.join(failed))

    def _unregister_hotkeys(self) -> None:
        for hid, _vk, _name in GLOBAL_HOTKEYS:
            user32.UnregisterHotKey(self.hwnd, hid)
        self.hotkeys_registered = 0
        self.hotkey_ok = False

    def hotkey_action(self, hid: int) -> None:
        name = next((n for h, _v, n in GLOBAL_HOTKEYS if h == hid), None)
        S = self.S
        if name == 'overlay':
            self.set_overlay(not self.overlay)
            if not self.overlay:
                user32.SetForegroundWindow(self.hwnd)
        elif name == 'boost':
            self.set_boost(not S['boost'])
        elif name == 'pin':
            self.set_pinned(not S['pinned'])
        elif name == 'restart':
            self.restart_stream()
        elif name == 'life-':
            self.set_life(S['life'] - 0.05)
        elif name == 'life+':
            self.set_life(S['life'] + 0.05)
        elif name == 'op-':
            self.set_opacity(S['opacity'] - 0.05)
        elif name == 'op+':
            self.set_opacity(S['opacity'] + 0.05)
        elif name == 'sens-':
            self.set_sensitivity(S['sensitivity'] - 1)
        elif name == 'sens+':
            self.set_sensitivity(S['sensitivity'] + 1)
        elif name and name.startswith('mode'):
            self.set_mode(MODES[int(name[4:])])

    # -- start with Windows (HKCU Run key) -------------------------------------------
    @staticmethod
    def _startup_command() -> str:
        if getattr(sys, 'frozen', False):
            return '"%s"' % sys.executable
        pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
        return '"%s" "%s"' % (pyw if os.path.exists(pyw) else sys.executable, os.path.abspath(__file__))

    @staticmethod
    def get_run_at_startup() -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except Exception:
            return False

    def set_run_at_startup(self, on: bool) -> None:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run', 0,
                                winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, self._startup_command())
                else:
                    try:
                        winreg.DeleteValue(k, APP_NAME)
                    except FileNotFoundError:
                        pass
            self.toast('Starts with Windows' if on else 'No longer starts with Windows')
        except Exception:
            log_exc('run at startup')
            self.toast('Could not change the startup setting (see errorlog.txt)', 3000)

    def _initial_geometry(self):
        g = self.S['window']
        if isinstance(g, dict):
            try:
                x, y, w, h = int(g['x']), int(g['y']), int(g['w']), int(g['h'])
                w, h = max(160, w), max(120, h)
                rc = wintypes.RECT(x, y, x + w, y + h)
                if user32.MonitorFromRect(byref(rc), MONITOR_DEFAULTTONULL):
                    return x, y, w, h
            except Exception:
                pass
        self.S['window'] = None
        w, h = 460, 320
        x, y = self._centre_on_primary(w, h)
        return x, y, w, h

    def _centre_on_primary(self, w, h):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        pt = wintypes.RECT(0, 0, 1, 1)
        hm = user32.MonitorFromRect(byref(pt), MONITOR_DEFAULTTOPRIMARY)
        if hm and user32.GetMonitorInfoW(hm, byref(mi)):
            wa = mi.rcWork
            return wa.left + (wa.right - wa.left - w) // 2, wa.top + (wa.bottom - wa.top - h) // 2
        return 100, 100

    def _update_dpr(self) -> None:
        try:
            d = user32.GetDpiForWindow(self.hwnd)
            self.dpr = (d / 96.0) if d else 1.0
        except Exception:
            self.dpr = 1.0

    # -- back buffer -----------------------------------------------------------
    def _ensure_surface(self) -> None:
        rc = wintypes.RECT()
        user32.GetClientRect(self.hwnd, byref(rc))
        W, H = max(1, rc.right - rc.left), max(1, rc.bottom - rc.top)
        if self.surface is not None and (W, H) == (self.W, self.H):
            return
        self._free_surface()
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = W
        bmi.bmiHeader.biHeight = -H  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = c_void_p()
        self.hdc = gdi32.CreateCompatibleDC(None)
        self.hbmp = gdi32.CreateDIBSection(None, byref(bmi), DIB_RGB_COLORS, byref(bits), None, 0)
        if not self.hbmp or not bits:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hbmp_old = gdi32.SelectObject(self.hdc, self.hbmp)
        self.bits = bits
        stride = W * 4
        try:
            self.surface_buf = (c_ubyte * (stride * H)).from_address(bits.value)
            self.surface = cairo.ImageSurface.create_for_data(self.surface_buf, cairo.FORMAT_ARGB32, W, H, stride)
            self.surface_own = None
        except Exception:
            # Fallback: draw into cairo's own buffer and copy it into the DIB each frame.
            log_exc('create_for_data (falling back to memcpy)')
            self.surface_buf = None
            self.surface_own = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
            self.surface = self.surface_own
        self.W, self.H = W, H

    def _free_surface(self) -> None:
        self.surface = None
        self.surface_own = None
        self.surface_buf = None
        if self.hdc:
            if self.hbmp_old:
                gdi32.SelectObject(self.hdc, self.hbmp_old)
            gdi32.DeleteDC(self.hdc)
        if self.hbmp:
            gdi32.DeleteObject(self.hbmp)
        self.hdc = self.hbmp = self.hbmp_old = None
        self.bits = None

    def _present(self) -> None:
        self.surface.flush()
        if self.surface_own is not None:
            ctypes.memmove(self.bits, self.surface_own.get_data(), self.W * self.H * 4)
        size = wintypes.SIZE(self.W, self.H)
        src = wintypes.POINT(0, 0)
        bf = BLENDFUNCTION(AC_SRC_OVER, 0, int(round(clamp(self.S['opacity'], 0.2, 1.0) * 255)), AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, None, None, byref(size), self.hdc, byref(src), 0, byref(bf), ULW_ALPHA)

    # -- main loop --------------------------------------------------------------
    def run(self) -> None:
        msg = wintypes.MSG()
        self.next_frame = time.perf_counter()
        while self.running:
            while user32.PeekMessageW(byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    self.running = False
                    break
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
            if not self.running:
                break
            now = time.perf_counter()
            wait = self.next_frame - now
            if wait <= 0.0005:
                self.render(now)
                self.next_frame = max(self.next_frame + FRAME, now)
            else:
                user32.MsgWaitForMultipleObjects(0, None, False, max(1, int(wait * 1000)), QS_ALLINPUT)
        self.shutdown()

    def shutdown(self) -> None:
        self.running = False
        try:
            self.engine.shutdown()
        except Exception:
            log_exc('shutdown audio')
        if self.hwnd and user32.IsWindow(self.hwnd):
            self._save_geometry()
        save_settings(self.S)
        self._free_surface()
        try:
            winmm.timeEndPeriod(1)
        except Exception:
            pass

    # -- window procedure --------------------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            r = self._handle(hwnd, msg, wparam, lparam)
            if r is not None:
                return r
        except Exception:
            log_exc('wndproc msg=0x%X' % msg)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle(self, hwnd, msg, wparam, lparam):
        if msg == WM_NCCALCSIZE:
            return 0  # no frame: the client area is the whole window (both wParam cases)
        if msg == WM_NCHITTEST:
            return self._hit_test(_lo16(lparam), _hi16(lparam))
        if msg == WM_TIMER:
            # Keeps the radar moving while a modal loop (drag, resize, menu) owns the thread.
            if wparam == TIMER_FRAME and self.modal:
                now = time.perf_counter()
                if now - self.last_render >= 0.015:
                    self.render(now)
            return 0
        if msg == WM_ENTERSIZEMOVE:
            self.modal += 1
            return 0
        if msg == WM_NCLBUTTONDBLCLK or msg == WM_LBUTTONDBLCLK:
            return 0
        if msg in (WM_NCRBUTTONUP, WM_RBUTTONUP):
            self.show_menu()
            return 0
        if msg in (WM_NCRBUTTONDOWN, WM_RBUTTONDOWN, WM_CONTEXTMENU):
            return 0
        if msg in (WM_MOUSEMOVE, WM_LBUTTONUP, WM_LBUTTONDOWN):
            x, y = _lo16(lparam), _hi16(lparam)  # client coordinates (client == window here)
            if self.panel == 'menu':
                self._menu_mouse(x, y, click=(msg == WM_LBUTTONUP))
            elif self.panel == 'tune' and msg == WM_LBUTTONUP:
                self._tune_mouse(x, y)
            return 0
        if msg == WM_MOUSEWHEEL:
            if self.panel == 'menu':
                self._menu_move(-1 if _hi16(wparam) > 0 else 1)
            elif self.panel == 'tune':
                self.tune_row = (self.tune_row + (-1 if _hi16(wparam) > 0 else 1)) % len(TUNE_ROWS)
            return 0
        if msg == WM_GETMINMAXINFO:
            mmi = cast(lparam, POINTER(MINMAXINFO)).contents
            mmi.ptMinTrackSize.x = int(160 * self.dpr)
            mmi.ptMinTrackSize.y = int(120 * self.dpr)
            return 0
        if msg == WM_SIZE:
            if self.hwnd and wparam != 1:  # not minimised
                self._ensure_surface()
                self.geom_dirty = True
            return 0
        if msg == WM_MOVE:
            self.geom_dirty = True
            return 0
        if msg == WM_EXITSIZEMOVE:
            self.modal = max(0, self.modal - 1)
            self._save_geometry()
            save_settings(self.S)
            return 0
        if msg == WM_ACTIVATE:
            self.active = (wparam & 0xFFFF) != 0
            return 0
        if msg == WM_KEYDOWN or msg == WM_SYSKEYDOWN:
            if self._key_down(wparam):
                return 0
            return None
        if msg == WM_CHAR:
            if self._key_char(wparam):
                return 0
            return None
        if msg == WM_HOTKEY:
            self.hotkey_action(int(wparam))
            return 0
        if msg == WM_TRAY:
            self._tray_event(int(lparam) & 0xFFFF)
            return 0
        if self.msg_taskbar_created and msg == self.msg_taskbar_created:
            self.tray_added = False
            self._tray_add()  # Explorer restarted: put the icon back
            return 0
        if msg == WM_DPICHANGED:
            self._update_dpr()
            rc = cast(lparam, POINTER(wintypes.RECT)).contents
            user32.SetWindowPos(hwnd, 0, rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top, SWP_NOZORDER | SWP_NOACTIVATE)
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_PAINT:
            user32.ValidateRect(hwnd, None)
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._save_geometry()  # the window still exists here; it is gone by the time run() returns
            save_settings(self.S)
            self._tray_remove()
            try:
                self._unregister_hotkeys()
                user32.KillTimer(hwnd, TIMER_FRAME)
            except Exception:
                pass
            self.running = False
            user32.PostQuitMessage(0)
            return 0
        return None

    def _hit_test(self, sx, sy):
        if self.overlay:
            return HTTRANSPARENT
        rc = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, byref(rc))
        if self.panel in ('menu', 'tune') and self.panel_rect:
            px, py, pw, ph = self.panel_rect
            cx, cy = sx - rc.left, sy - rc.top
            if px <= cx <= px + pw and py <= cy <= py + ph:
                return HTCLIENT  # the side panel takes mouse input; the rest still drags the window
        b = int(8 * self.dpr)
        left = sx < rc.left + b
        right = sx >= rc.right - b
        top = sy < rc.top + b
        bottom = sy >= rc.bottom - b
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
        return HTCAPTION

    def _save_geometry(self) -> None:
        if not self.hwnd or user32.IsIconic(self.hwnd):
            return
        rc = wintypes.RECT()
        if user32.GetWindowRect(self.hwnd, byref(rc)):
            self.S['window'] = {'x': rc.left, 'y': rc.top, 'w': rc.right - rc.left, 'h': rc.bottom - rc.top}
        self.geom_dirty = False

    # -- keys ---------------------------------------------------------------------
    def _key_down(self, vk) -> bool:
        if vk == VK_ESCAPE:
            self.close_all()
            return True
        if vk == VK_F1:
            self.toggle_help()
            return True
        if vk == VK_TAB:
            self.toggle_tune()
            return True
        if self.panel == 'menu':
            return self._menu_key(vk)
        if self.panel == 'tune':
            if vk == VK_UP:
                self.tune_row = (self.tune_row - 1) % len(TUNE_ROWS)
            elif vk == VK_DOWN:
                self.tune_row = (self.tune_row + 1) % len(TUNE_ROWS)
            elif vk == VK_LEFT:
                self.tune_adjust(-1)
            elif vk == VK_RIGHT:
                self.tune_adjust(+1)
            elif vk == VK_RETURN:
                self.close_panel()
            else:
                return False
            return True
        if self.panel and vk == VK_RETURN:
            self.close_panel()
            return True
        return False

    # -- tuning panel -----------------------------------------------------------------
    def toggle_tune(self) -> None:
        self.panel = None if self.panel == 'tune' else 'tune'

    def tune_adjust(self, direction: int) -> None:
        key, label, kind, lo, hi, step, fmt, choices = TUNE_ROWS[self.tune_row]
        S = self.S
        v = S[key]
        if key == 'fullCircle':
            self.set_full_circle(not bool(v))
            return
        if kind == 'bool':
            v = not bool(v)
        elif kind == 'choice':
            try:
                i = min(range(len(choices)), key=lambda j: abs(float(choices[j]) - float(v)))
            except Exception:
                i = 0
            v = choices[int(clamp(i + direction, 0, len(choices) - 1))]
        elif kind == 'int':
            v = int(clamp(int(v) + direction * step, lo, hi))
        else:
            v = clamp(round((float(v) + direction * step) / step) * step, lo, hi)
            v = float('%.4g' % v)
        S[key] = v
        self._sync_profile()
        save_settings(S)

    def set_sensitivity(self, v: int) -> None:
        self.S['sensitivity'] = int(clamp(int(v), 1, 10))
        self.saved('Sensitivity %d / 10' % self.S['sensitivity'])

    # -- foreground app: profiles, auto-hide, per-app capture ----------------------------
    def _poll_foreground(self, t: float) -> None:
        if t - self.fg_last < 0.5:
            return
        self.fg_last = t
        try:
            h = user32.GetForegroundWindow()
            pid = wintypes.DWORD(0)
            if h:
                user32.GetWindowThreadProcessId(h, byref(pid))
            pid = int(pid.value)
            self.fg_self = (pid == self.my_pid) or pid == 0
            if self.fg_self:
                self._apply_autohide()
                return
            exe = process_name(pid)
            if exe and (exe != self.fg_exe or pid != self.fg_pid):
                self.fg_exe, self.fg_pid = exe, pid
                if exe not in DESKTOP_EXES:
                    self.last_app_exe, self.last_app_pid = exe, pid
                    if self.S['processCapture']:
                        self.engine.set_process_target(pid, exe)
                self._apply_profile_for(exe)
            self._apply_autohide()
        except Exception:
            log_exc('foreground poll')

    def _apply_profile_for(self, exe: str) -> None:
        S = self.S
        prof = S['profiles'].get(exe) if S['autoProfile'] else None
        if prof:
            if self.active_profile != exe:
                if self.active_profile is None:
                    self.base_tuning = {k: S[k] for k in PROFILE_KEYS}
                for k in PROFILE_KEYS:
                    if k in prof:
                        S[k] = prof[k]
                self.active_profile = exe
                self.toast('Profile: ' + exe, 2000)
        elif exe not in DESKTOP_EXES and self.active_profile is not None:
            self._leave_profile()

    def _leave_profile(self) -> None:
        if self.base_tuning:
            self.S.update(self.base_tuning)
        self.active_profile = None
        self.base_tuning = None

    def _sync_profile(self) -> None:
        if self.active_profile:
            self.S['profiles'][self.active_profile] = {k: self.S[k] for k in PROFILE_KEYS}

    def save_profile(self) -> None:
        exe = self.last_app_exe
        if not exe:
            self.toast('Bring the game to the front first, then save its profile', 2600)
            return
        self.S['profiles'][exe] = {k: self.S[k] for k in PROFILE_KEYS}
        if self.active_profile is None:
            self.base_tuning = {k: self.S[k] for k in PROFILE_KEYS}
        self.active_profile = exe
        self.saved('Profile saved for ' + exe)

    def remove_profile(self, exe: str) -> None:
        self.S['profiles'].pop(exe, None)
        if self.active_profile == exe:
            self.active_profile = None
            self.base_tuning = None
        self.saved('Profile removed for ' + exe)

    def _apply_autohide(self) -> None:
        want = True
        if self.S['autoHide']:
            want = self.fg_self or (self.fg_exe in self.S['profiles'])
        if want != self.visible:
            self.visible = want
            user32.ShowWindow(self.hwnd, 4 if want else 0)  # SW_SHOWNOACTIVATE / SW_HIDE

    def set_process_capture(self, on: bool) -> None:
        self.S['processCapture'] = bool(on)
        if on:
            if self.last_app_pid:
                self.engine.set_process_target(self.last_app_pid, self.last_app_exe)
            self.engine.set_want_process(True)
            self.saved('Capturing only %s' % (self.last_app_exe or 'the next app in front'))
        else:
            self.engine.set_want_process(False)
            self.saved('Capturing the whole playback device')

    def set_full_circle(self, on: bool) -> None:
        self.S['fullCircle'] = bool(on)
        if on and not self.an.surround:
            self.saved('Full circle. Stereo input only fills the front half: turn on Windows Sonic or DTS spatial sound '
                       'for the headset so games output 7.1, and the back half comes alive', 5000)
        else:
            self.saved('Full circle ' + ('always' if on else 'only with surround input'))

    def set_size_preset(self, i: int) -> None:
        name, w, h = SIZE_PRESETS[i]
        user32.SetWindowPos(self.hwnd, 0, 0, 0, int(w * self.dpr), int(h * self.dpr), SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
        self.toast('Size: ' + name)

    def _key_char(self, code) -> bool:
        ch = chr(code) if code < 0x110000 else ''
        if code == 0x11:  # Ctrl+Q quits from anywhere, panels included
            self.quit()
            return True
        if self.panel == 'menu':
            if ch == ' ':
                self._menu_activate()
            return True  # letters are not hotkeys while the menu is open
        if self.panel == 'tune':
            # the radar keeps its hotkeys while the tuning panel is open; arrows are handled in _key_down
            if ch in ('\r', '\t'):
                return True
        elif self.panel:
            if self.panel == 'welcome' and ch in ('d', 'D'):
                self.S['hideWelcome'] = True
                save_settings(self.S)
                self.close_panel()
            elif self.panel == 'welcome' and ch == ' ':
                self.close_panel()
                self.toggle_demo(True)
            elif self.panel == 'help' and ch in (' ', '?'):
                self.panel = 'help2'
            elif ch in ('?', '\r', ' ', '\x1b'):
                self.close_panel()
            return True  # other keys are swallowed while a panel is open
        S = self.S
        if ch in (',', '<'):
            self.set_sensitivity(S['sensitivity'] - 1)
        elif ch in ('.', '>'):
            self.set_sensitivity(S['sensitivity'] + 1)
        elif ch in '12345' and ch:
            self.set_mode(MODES[int(ch) - 1])
        elif ch in ('b', 'B'):
            self.set_boost(not S['boost'])
        elif ch == '[':
            self.set_life(S['life'] - 0.05)
        elif ch == ']':
            self.set_life(S['life'] + 0.05)
        elif ch in ('-', '_'):
            self.set_opacity(S['opacity'] - 0.05)
        elif ch in ('=', '+'):
            self.set_opacity(S['opacity'] + 0.05)
        elif ch in ('p', 'P'):
            self.set_pinned(not S['pinned'])
        elif ch in ('o', 'O'):
            self.set_overlay(not self.overlay)
        elif ch in ('r', 'R'):
            self.restart_stream()
        elif ch in ('d', 'D'):
            self.toggle_demo()
        elif ch in ('s', 'S'):
            self.cycle_smoothing()
        elif ch in ('a', 'A'):
            self.set_adaptive(not S['adaptive'])
        elif ch in ('c', 'C'):
            self.cycle_max_cues()
        elif ch == '?':
            self.toggle_help()
        elif ch in ('g', 'G'):
            S['guides'] = not S['guides']
            self.saved('Guides ' + ('on' if S['guides'] else 'off'))
        elif ch in ('m', 'M'):
            S['meters'] = not S['meters']
            self.saved('Meters ' + ('on' if S['meters'] else 'off'))
        elif ch in ('t', 'T'):
            i = THEME_ORDER.index(S['theme']) if S['theme'] in THEME_ORDER else 0
            S['theme'] = THEME_ORDER[(i + 1) % len(THEME_ORDER)]
            self.saved('Theme: ' + S['theme'].capitalize())
        elif code == 0x11:  # Ctrl+Q
            self.quit()
        else:
            return False
        return True

    # -- actions -------------------------------------------------------------------
    def toast(self, text: str, ms: int = 1600) -> None:
        self.toast_text = text
        self.toast_until = time.perf_counter() + ms / 1000.0

    def saved(self, msg: str | None = None, ms: int = 1600) -> None:
        self._sync_profile()
        save_settings(self.S)
        if msg:
            self.toast(msg, ms)

    def set_mode(self, m: str) -> None:
        self.S['mode'] = m
        self.saved('Mode: ' + MODE_NAMES[m])

    def set_boost(self, v: bool) -> None:
        self.S['boost'] = bool(v)
        self.saved('Boost ' + ('on' if v else 'off'))

    def set_life(self, v: float) -> None:
        self.S['life'] = clamp(round(v * 20) / 20.0, 0.1, 2.0)
        self.saved('Life %.2f s' % self.S['life'])

    def set_opacity(self, v: float) -> None:
        self.S['opacity'] = clamp(round(v * 20) / 20.0, 0.2, 1.0)
        self.saved('Opacity %d%%' % round(self.S['opacity'] * 100))

    def set_pinned(self, v: bool, quiet: bool = False) -> None:
        self.S['pinned'] = bool(v)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST if v else HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        save_settings(self.S)
        if not quiet:
            self.toast('Pinned on top' if v else 'Not pinned')

    def set_overlay(self, on: bool) -> None:
        self.overlay = bool(on)
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if on else (ex & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)
        if on:
            self.panel = None
            if not self.S['pinned']:
                self.set_pinned(True, quiet=True)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            back = 'Ctrl+Shift+O' if self.hotkey_ok else 'the taskbar button, then O'
            self.toast('Overlay on. Clicks pass through. %s brings the panel back' % back, 3200)
        else:
            user32.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            self.toast('Overlay off')

    def restart_stream(self) -> None:
        self.an.reset()
        self.engine.restart()
        self.toast('Restarting stream')

    def toggle_demo(self, on: bool | None = None) -> None:
        want = (self.engine.demo is None) if on is None else bool(on)
        self.an.reset()
        if want:
            self.engine.start_demo()
            self.toast('Demo: engine, footsteps and shots. D or R returns to game audio', 2600)
        else:
            self.engine.stop_demo()
            self.toast('Back to game audio')

    def set_global_hotkeys(self, on: bool) -> None:
        self.S['globalHotkeys'] = bool(on)
        self._register_hotkeys()
        self.saved('Global hotkeys ' + ('on' if on else 'off'))

    def set_smoothing(self, v: float) -> None:
        self.S['smoothing'] = clamp(float(v), 0.0, 1.0)
        name = SMOOTHING_NAMES.get(self.S['smoothing'], '%d%%' % round(self.S['smoothing'] * 100))
        self.saved('Smoothing: ' + name)

    def cycle_smoothing(self) -> None:
        cur = self.S['smoothing']
        nxt = next((v for v in SMOOTHING_CHOICES if v > cur + 1e-6), SMOOTHING_CHOICES[0])
        self.set_smoothing(nxt)

    def set_max_cues(self, v: int) -> None:
        self.S['maxCues'] = int(clamp(int(v), 1, BAND_N))
        self.saved('Cues per frame: %d' % self.S['maxCues'])

    def cycle_max_cues(self) -> None:
        cur = self.S['maxCues']
        nxt = next((v for v in MAXCUES_CHOICES if v > cur), MAXCUES_CHOICES[0])
        self.set_max_cues(nxt)

    def set_adaptive(self, on: bool) -> None:
        self.S['adaptive'] = bool(on)
        self.saved('Fade steady sounds ' + ('on' if on else 'off'))

    def toggle_help(self) -> None:
        self.panel = None if self.panel in ('help', 'help2') else 'help'

    def close_panel(self) -> None:
        self.panel = None

    def close_all(self) -> None:
        if self.panel:
            self.panel = None
        elif self.overlay:
            self.set_overlay(False)

    def quit(self) -> None:
        user32.DestroyWindow(self.hwnd)

    def reset_settings(self) -> None:
        keep_mode = self.S['mode']
        win = self.S['window']
        self.S.update(DEFAULTS)
        self.S['mode'] = keep_mode
        self.S['window'] = win
        self.S['hideWelcome'] = True
        self.set_pinned(True, quiet=True)
        self.saved('Settings reset')

    # -- context menu ----------------------------------------------------------------
    # -- in-window menu: drawn with cairo in the HUD's own style, mouse + keyboard ----------------
    def show_menu(self) -> None:
        if self.panel == 'menu':
            self.panel = None
            return
        self.panel = 'menu'
        self.menu_path = []
        self.menu_sels = [0]
        self.menu_scroll = 0

    def _menu_root(self) -> list:
        """The menu model, rebuilt on every frame so values and switches are always current."""
        S = self.S
        game = self.last_app_exe
        sep = {'kind': 'sep'}

        def act(label, cmd, key=None, grayed=False):
            return {'kind': 'action', 'label': label, 'cmd': cmd, 'key': key, 'grayed': grayed}

        def tog(label, cmd, on, key=None):
            return {'kind': 'toggle', 'label': label, 'cmd': cmd, 'on': bool(on), 'key': key}

        def sub(label, items, value=None, key=None):
            return {'kind': 'sub', 'label': label, 'items': items, 'value': value, 'key': key}

        def opts(fmt, values, current, base, keys=None):
            return [{'kind': 'option', 'label': fmt(v), 'on': abs(float(v) - float(current)) < 1e-6, 'cmd': base + i,
                     'key': keys[i] if keys else None} for i, v in enumerate(values)]

        def choice(label, value, options, key=None):
            return {'kind': 'choice', 'label': label, 'items': options, 'value': value, 'key': key}

        mode_items = [{'kind': 'option', 'label': MODE_NAMES[m], 'on': S['mode'] == m, 'cmd': MENU_MODE + i, 'key': str(i + 1)}
                      for i, m in enumerate(MODES)]
        detection = [
            choice('Smoothing', SMOOTHING_NAMES.get(S['smoothing'], '%g' % S['smoothing']),
                   opts(lambda v: SMOOTHING_NAMES[v], SMOOTHING_CHOICES, S['smoothing'], MENU_SMOOTH), 'S'),
            choice('Cues per frame', '%d' % S['maxCues'], opts(lambda v: '%d' % v, MAXCUES_CHOICES, S['maxCues'], MENU_MAXCUES), 'C'),
            choice('Centre suppression', 'Off' if S['centre'] == 0 else '%d / 10' % S['centre'],
                   opts(lambda v: 'Off' if v == 0 else '%d' % v, CENTRE_CHOICES, S['centre'], MENU_CENTRE)),
            choice('Noise gate', '%.2f' % S['gate'], opts(lambda v: '%.2f' % v, GATE_CHOICES, S['gate'], MENU_GATE)),
            choice('Radius boost', '%d' % S['radiusBoost'], opts(lambda v: '%d' % v, RADIUS_CHOICES, S['radiusBoost'], MENU_RADIUS)),
            choice('Trail life', '%.2f s' % S['life'], opts(lambda v: '%.2f s' % v, LIFE_CHOICES, S['life'], MENU_LIFE), '[ ]'),
            choice('Input gain', '%.2fx' % S['gain'], opts(lambda v: '%.2fx' % v, GAIN_CHOICES, S['gain'], MENU_GAIN)),
            sep,
            tog('Boost: push cues toward the rim', MENU_BOOST, S['boost'], 'B'),
            tog('Fade steady sounds to half', MENU_ADAPTIVE, S['adaptive'], 'A'),
        ]
        look = [
            choice('Colour theme', S['theme'].capitalize(),
                   [{'kind': 'option', 'label': n.capitalize(), 'on': S['theme'] == n, 'cmd': MENU_THEME + i} for i, n in enumerate(THEME_ORDER)], 'T'),
            choice('Line width', '%g' % S['lineWidth'], opts(lambda v: '%g' % v, WIDTH_CHOICES, S['lineWidth'], MENU_WIDTH)),
            choice('Opacity', '%d%%' % round(S['opacity'] * 100), opts(lambda v: '%d%%' % round(v * 100), OPACITY_CHOICES, S['opacity'], MENU_OPACITY), '- ='),
            sub('Window size', [{'kind': 'option', 'label': '%s  (%d x %d)' % (n, w, h), 'on': False, 'cmd': MENU_SIZE + i}
                                for i, (n, w, h) in enumerate(SIZE_PRESETS)]),
            sep,
            tog('Colour by pitch', MENU_COLOUR, S['colourByPitch']),
            tog('Event markers on the rim', MENU_MARKERS, S['markers']),
            tog('Rim and guides', MENU_GUIDES, S['guides'], 'G'),
            tog('L / R level meters', MENU_METERS, S['meters'], 'M'),
        ]
        audio = [
            act('Restart audio stream', MENU_RESTART, 'R'),
            tog('Demo scene (no game needed)', MENU_DEMO, self.engine.demo is not None, 'D'),
            sep,
            tog('Capture only the game' + ('  (%s)' % game if game else ''), MENU_PROCCAP, S['processCapture']),
            tog('Full circle on 5.1 / 7.1 devices', MENU_SURROUND, S['surround'] != 'off'),
            tog('Full circle always', MENU_FULLCIRCLE, S['fullCircle']),
        ]
        profiles = [
            act('Save tuning as profile for %s' % (game or '(bring a game to the front first)'), MENU_SAVE_PROFILE, grayed=not game),
            act('Remove profile for %s' % (game or '...'), MENU_DEL_PROFILE, grayed=not (game and game in S['profiles'])),
            sep,
            tog('Switch profiles with the game in front', MENU_AUTOPROFILE, S['autoProfile']),
            tog('Auto-hide unless a profiled game is in front', MENU_AUTOHIDE, S['autoHide']),
        ]
        if S['profiles']:
            profiles.append(sep)
            profiles += [{'kind': 'info', 'label': n + ('   (active)' if n == self.active_profile else '')} for n in sorted(S['profiles'])]
        window = [
            tog('Pin on top', MENU_PIN, S['pinned'], 'P'),
            tog('Overlay: click-through, no panel', MENU_OVERLAY, self.overlay, 'O'),
            sep,
            tog('Global hotkeys (Ctrl+Shift+key)', MENU_GLOBALHK, S['globalHotkeys']),
            tog('Start in overlay mode', MENU_STARTOVERLAY, S['startOverlay']),
            tog('Start with Windows', MENU_STARTUP, self.get_run_at_startup()),
        ]
        helpm = [
            act('Controls and tips', MENU_HELP, '?'),
            act('Welcome screen', MENU_WELCOME),
            sep,
            act('Open settings.json', MENU_OPEN_SETTINGS),
            act('Open errorlog.txt', MENU_OPEN_LOG),
            sep,
            act('Reset all settings', MENU_RESET),
        ]
        return [
            act('Tuning panel', MENU_TUNE, 'Tab'),
            sep,
            sub('Mode', mode_items, MODE_NAMES[S['mode']]),
            choice('Sensitivity', '%d / 10' % S['sensitivity'], opts(lambda v: '%d' % v, SENS_CHOICES, S['sensitivity'], MENU_SENS), ', .'),
            choice('Stereo width', '%d / 10' % S['width'], opts(lambda v: '%d' % v, SENS_CHOICES, S['width'], MENU_STWIDTH)),
            sub('Detection', detection),
            sub('Look', look),
            sub('Audio', audio),
            sub('Game profiles', profiles, self.active_profile),
            sub('Window', window),
            sep,
            sub('Help', helpm),
            act('Quit', MENU_QUIT, 'Ctrl+Q'),
        ]

    def _menu_level(self) -> list:
        items = self._menu_root()
        for label in self.menu_path:
            nxt = next((it for it in items if it.get('label') == label and it['kind'] in ('sub', 'choice')), None)
            if nxt is None:
                self.menu_path = []
                self.menu_sels = [0]
                return self._menu_root()
            items = nxt['items']
        return items

    @staticmethod
    def _menu_selectable(it) -> bool:
        return it['kind'] not in ('sep', 'info') and not it.get('grayed')

    def _menu_move(self, step: int) -> None:
        items = self._menu_level()
        if not items:
            return
        i = self.menu_sels[-1]
        for _ in range(len(items)):
            i = (i + step) % len(items)
            if self._menu_selectable(items[i]):
                break
        self.menu_sels[-1] = i

    def _menu_back(self) -> None:
        if self.menu_path:
            self.menu_path.pop()
            self.menu_sels.pop()
            self.menu_scroll = 0
        else:
            self.panel = None

    def _menu_activate(self, it=None) -> None:
        items = self._menu_level()
        if it is None:
            if not items:
                return
            it = items[self.menu_sels[-1]]
        if not self._menu_selectable(it):
            return
        kind = it['kind']
        if kind in ('sub', 'choice'):
            self.menu_path.append(it['label'])
            on = next((i for i, o in enumerate(it['items']) if o.get('on')), 0)
            self.menu_sels.append(on)
            self.menu_scroll = 0
        elif kind == 'option':
            self.menu_command(int(it['cmd']))
            if self.panel == 'menu':
                self._menu_back()
        else:
            self.menu_command(int(it['cmd']))

    def _menu_key(self, vk) -> bool:
        if vk == VK_UP:
            self._menu_move(-1)
        elif vk == VK_DOWN:
            self._menu_move(1)
        elif vk in (VK_RIGHT, VK_RETURN):
            self._menu_activate()
        elif vk in (VK_LEFT, 0x08):  # Left / Backspace
            self._menu_back()
        else:
            return False
        return True

    def _menu_mouse(self, x: float, y: float, click: bool) -> None:
        """Hover selects, click activates; a click on the header row goes back; outside the panel closes."""
        if not self.panel_rect:
            return
        px, py, pw, ph = self.panel_rect
        inside = px <= x <= px + pw and py <= y <= py + ph
        if click and not inside:
            self.panel = None
            return
        if click and self.menu_path and self.menu_head and self.menu_head[0] <= y <= self.menu_head[1]:
            self._menu_back()
            return
        for y0, y1, idx in self.menu_rows:
            if y0 <= y < y1:
                items = self._menu_level()
                if 0 <= idx < len(items) and self._menu_selectable(items[idx]):
                    self.menu_sels[-1] = idx
                    if click:
                        self._menu_activate(items[idx])
                return

    def _draw_menu(self, ctx, accent) -> None:
        W, H, dpr = self.W, self.H, self.dpr
        items = self._menu_level()
        sel = self.menu_sels[-1] if self.menu_sels else 0
        pw = min(250 * dpr, W - 12 * dpr)
        top = 30 * dpr
        x, y, ph = W - pw - 6 * dpr, top, H - top - 6 * dpr
        self.panel_rect = (x, y, pw, ph)
        self._rounded(ctx, x, y, pw, ph, 8 * dpr)
        ctx.set_source_rgba(PANEL[0], PANEL[1], PANEL[2], 0.95)
        ctx.fill_preserve()
        ctx.set_source_rgba(accent[0], accent[1], accent[2], 0.5)
        ctx.set_line_width(1 * dpr)
        ctx.stroke()
        pad = 10 * dpr
        head_h = 24 * dpr
        foot_h = 15 * dpr
        rh = 22 * dpr
        fs = 10.5 * dpr
        # header: title, or "< Back" + current group
        hy = y + 4 * dpr
        if self.menu_path:
            self.menu_head = (hy, hy + head_h)
            self._text(ctx, '‹  ' + self.menu_path[-1].upper(), x + pad, hy + head_h / 2, 11 * dpr, FG, True, 'left', 'middle')
            self._text(ctx, 'Back', x + pw - pad, hy + head_h / 2, 8.5 * dpr, MUTED, False, 'right', 'middle')
        else:
            self.menu_head = None
            self._text(ctx, 'MENU', x + pad, hy + head_h / 2, 11 * dpr, FG, True, 'left', 'middle')
            self._text(ctx, 'Esc closes', x + pw - pad, hy + head_h / 2, 8.5 * dpr, MUTED, False, 'right', 'middle')
        ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.1)
        ctx.rectangle(x + pad, hy + head_h, pw - 2 * pad, 1 * dpr)
        ctx.fill()
        # rows (scrolled so the selection stays visible)
        list_y = hy + head_h + 4 * dpr
        avail = y + ph - foot_h - list_y
        # shrink rows (down to 16 px) before resorting to scrolling
        n_sep = sum(1 for it in items if it['kind'] == 'sep')
        n_row = max(1, len(items) - n_sep)
        rh = clamp((avail - n_sep * 8 * dpr) / n_row, 16 * dpr, 22 * dpr)
        fs = clamp(rh * 0.48, 8.5 * dpr, 10.5 * dpr)
        heights = [(8 * dpr if it['kind'] == 'sep' else rh) for it in items]
        max_rows = len(items)
        while max_rows > 0 and sum(heights[self.menu_scroll:self.menu_scroll + max_rows]) > avail:
            max_rows -= 1
        if sel < self.menu_scroll:
            self.menu_scroll = sel
        while sel >= self.menu_scroll + max_rows and max_rows > 0:
            self.menu_scroll += 1
        self.menu_rows = []
        ry = list_y
        for i in range(self.menu_scroll, len(items)):
            it = items[i]
            h = heights[i]
            if ry + h > y + ph - foot_h:
                break
            if it['kind'] == 'sep':
                ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.1)
                ctx.rectangle(x + pad, ry + h / 2, pw - 2 * pad, 1 * dpr)
                ctx.fill()
                ry += h
                continue
            self.menu_rows.append((ry, ry + h, i))
            selected = (i == sel)
            grayed = it.get('grayed') or it['kind'] == 'info'
            if selected:
                self._rounded(ctx, x + 4 * dpr, ry + 1 * dpr, pw - 8 * dpr, h - 2 * dpr, 4 * dpr)
                ctx.set_source_rgba(accent[0], accent[1], accent[2], 0.16)
                ctx.fill()
            col = MUTED if grayed else (accent if selected else FG)
            cy = ry + h / 2
            right = x + pw - pad
            kind = it['kind']
            if kind == 'toggle':
                tw_, th_ = 22 * dpr, 12 * dpr
                tx = right - tw_
                self._rounded(ctx, tx, cy - th_ / 2, tw_, th_, th_ / 2)
                if it['on']:
                    ctx.set_source_rgba(accent[0], accent[1], accent[2], 0.9)
                else:
                    ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.18)
                ctx.fill()
                kx = tx + (tw_ - th_ / 2 - 2 * dpr) if it['on'] else tx + th_ / 2 + 2 * dpr
                ctx.set_source_rgba(PANEL[0], PANEL[1], PANEL[2], 1) if it['on'] else ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.8)
                ctx.arc(kx, cy, th_ / 2 - 2 * dpr, 0, 2 * math.pi)
                ctx.fill()
                right = tx - 8 * dpr
            elif kind == 'option':
                ctx.set_line_width(1.2 * dpr)
                ctx.set_source_rgba(accent[0], accent[1], accent[2], 0.9 if it['on'] else 0.35)
                ctx.new_sub_path()
                ctx.arc(right - 5 * dpr, cy, 4.5 * dpr, 0, 2 * math.pi)
                ctx.stroke()
                if it['on']:
                    ctx.arc(right - 5 * dpr, cy, 2.3 * dpr, 0, 2 * math.pi)
                    ctx.fill()
                right -= 18 * dpr
            elif kind in ('sub', 'choice'):
                self._text(ctx, '›', right, cy, 13 * dpr, (FG[0], FG[1], FG[2], 0.55), True, 'right', 'middle')
                right -= 12 * dpr
                if it.get('value'):
                    right -= self._text(ctx, str(it['value']), right, cy, fs, accent if not grayed else MUTED, True, 'right', 'middle') + 8 * dpr
            if it.get('key'):
                right -= self._text(ctx, str(it['key']), right, cy, 8.5 * dpr, (MUTED[0], MUTED[1], MUTED[2], 0.9), False, 'right', 'middle') + 8 * dpr
            # label, clipped to the space left
            label = it['label']
            self._font(ctx, fs, selected)
            maxw = right - (x + pad) - 4 * dpr
            while label and ctx.text_extents(label).x_advance > maxw and len(label) > 3:
                label = label[:-2].rstrip('. ') + '…'
            self._text(ctx, label, x + pad, cy, fs, col, selected, 'left', 'middle')
            ry += h
        self._text(ctx, '↑↓ move    Enter open    ← back    click works too', x + pw / 2, y + ph - 4 * dpr, 8 * dpr, MUTED, False, 'center', 'bottom')

    def menu_command(self, cmd: int) -> None:
        S = self.S
        if MENU_MODE <= cmd < MENU_MODE + len(MODES):
            self.set_mode(MODES[cmd - MENU_MODE])
        elif cmd == MENU_BOOST:
            self.set_boost(not S['boost'])
        elif cmd == MENU_PIN:
            self.set_pinned(not S['pinned'])
        elif cmd == MENU_OVERLAY:
            self.set_overlay(not self.overlay)
        elif cmd == MENU_RESTART:
            self.restart_stream()
        elif cmd == MENU_DEMO:
            self.toggle_demo()
        elif cmd == MENU_GLOBALHK:
            self.set_global_hotkeys(not S['globalHotkeys'])
        elif cmd == MENU_STARTOVERLAY:
            S['startOverlay'] = not S['startOverlay']
            self.saved('Starts in overlay mode' if S['startOverlay'] else 'Starts with the panel')
        elif cmd == MENU_STARTUP:
            self.set_run_at_startup(not self.get_run_at_startup())
        elif cmd == MENU_ADAPTIVE:
            self.set_adaptive(not S['adaptive'])
        elif cmd == MENU_TUNE:
            self.toggle_tune()
        elif cmd == MENU_SAVE_PROFILE:
            self.save_profile()
        elif cmd == MENU_DEL_PROFILE:
            self.remove_profile(self.last_app_exe)
        elif cmd == MENU_AUTOPROFILE:
            S['autoProfile'] = not S['autoProfile']
            self.saved('Profile switching ' + ('on' if S['autoProfile'] else 'off'))
        elif cmd == MENU_AUTOHIDE:
            S['autoHide'] = not S['autoHide']
            self.saved('Auto-hide ' + ('on: shows only while a profiled game is in front' if S['autoHide'] else 'off'), 2600)
        elif cmd == MENU_PROCCAP:
            self.set_process_capture(not S['processCapture'])
        elif cmd == MENU_SURROUND:
            S['surround'] = 'off' if S['surround'] != 'off' else 'auto'
            self.an.reset()
            self.saved('Full-circle radar on surround devices ' + ('on' if S['surround'] != 'off' else 'off'))
        elif cmd == MENU_FULLCIRCLE:
            self.set_full_circle(not S['fullCircle'])
        elif cmd == MENU_COLOUR:
            S['colourByPitch'] = not S['colourByPitch']
            self.saved('Colour by pitch ' + ('on' if S['colourByPitch'] else 'off'))
        elif cmd == MENU_MARKERS:
            S['markers'] = not S['markers']
            self.saved('Event markers ' + ('on' if S['markers'] else 'off'))
        elif MENU_CENTRE <= cmd < MENU_CENTRE + len(CENTRE_CHOICES):
            S['centre'] = CENTRE_CHOICES[cmd - MENU_CENTRE]
            self.saved('Centre suppression ' + ('off' if S['centre'] == 0 else '%d / 10' % S['centre']))
        elif MENU_SIZE <= cmd < MENU_SIZE + len(SIZE_PRESETS):
            self.set_size_preset(cmd - MENU_SIZE)
        elif MENU_SENS <= cmd < MENU_SENS + len(SENS_CHOICES):
            self.set_sensitivity(SENS_CHOICES[cmd - MENU_SENS])
        elif MENU_STWIDTH <= cmd < MENU_STWIDTH + len(SENS_CHOICES):
            S['width'] = SENS_CHOICES[cmd - MENU_STWIDTH]
            self.saved('Stereo width %d / 10' % S['width'])
        elif MENU_SMOOTH <= cmd < MENU_SMOOTH + len(SMOOTHING_CHOICES):
            self.set_smoothing(SMOOTHING_CHOICES[cmd - MENU_SMOOTH])
        elif MENU_MAXCUES <= cmd < MENU_MAXCUES + len(MAXCUES_CHOICES):
            self.set_max_cues(MAXCUES_CHOICES[cmd - MENU_MAXCUES])
        elif cmd == MENU_GUIDES:
            S['guides'] = not S['guides']
            self.saved()
        elif cmd == MENU_METERS:
            S['meters'] = not S['meters']
            self.saved()
        elif cmd == MENU_HELP:
            self.panel = 'help'
        elif cmd == MENU_WELCOME:
            self.panel = 'welcome'
        elif cmd == MENU_OPEN_SETTINGS:
            save_settings(S)
            self._open_path(SETTINGS_PATH)
        elif cmd == MENU_OPEN_LOG:
            if not os.path.exists(LOG_PATH):
                log('(log created from the menu)')
            self._open_path(LOG_PATH)
        elif cmd == MENU_RESET:
            self.reset_settings()
        elif cmd == MENU_QUIT:
            self.quit()
        elif MENU_THEME <= cmd < MENU_THEME + len(THEME_ORDER):
            S['theme'] = THEME_ORDER[cmd - MENU_THEME]
            self.saved('Theme: ' + S['theme'].capitalize())
        elif MENU_LIFE <= cmd < MENU_LIFE + len(LIFE_CHOICES):
            self.set_life(LIFE_CHOICES[cmd - MENU_LIFE])
        elif MENU_OPACITY <= cmd < MENU_OPACITY + len(OPACITY_CHOICES):
            self.set_opacity(OPACITY_CHOICES[cmd - MENU_OPACITY])
        elif MENU_WIDTH <= cmd < MENU_WIDTH + len(WIDTH_CHOICES):
            S['lineWidth'] = WIDTH_CHOICES[cmd - MENU_WIDTH]
            self.saved('Line width %g' % S['lineWidth'])
        elif MENU_RADIUS <= cmd < MENU_RADIUS + len(RADIUS_CHOICES):
            S['radiusBoost'] = RADIUS_CHOICES[cmd - MENU_RADIUS]
            self.saved('Radius boost %d' % S['radiusBoost'])
        elif MENU_GATE <= cmd < MENU_GATE + len(GATE_CHOICES):
            S['gate'] = GATE_CHOICES[cmd - MENU_GATE]
            self.saved('Noise gate %.2f' % S['gate'])
        elif MENU_GAIN <= cmd < MENU_GAIN + len(GAIN_CHOICES):
            S['gain'] = GAIN_CHOICES[cmd - MENU_GAIN]
            self.saved('Input gain %.2fx' % S['gain'])

    @staticmethod
    def _open_path(p: str) -> None:
        try:
            os.startfile(p)
        except Exception:
            log_exc('open ' + p)

    # -- rendering --------------------------------------------------------------------
    def render(self, t: float) -> None:
        if not self.hwnd or self.surface is None:
            return
        self.last_render = t
        try:
            self._poll_foreground(t)
            self.engine.tick(t)
            if self.engine.state in ('live', 'demo'):
                self.an.analyse(t, self.S)
            self.an.prune(t, self.S['life'])
            if user32.IsIconic(self.hwnd) or not self.visible:
                return
            ctx = cairo.Context(self.surface)
            ctx.set_operator(cairo.OPERATOR_CLEAR)
            ctx.paint()
            ctx.set_operator(cairo.OPERATOR_OVER)
            self.draw(ctx, t)
            self._present()
        except Exception:
            log_exc('render')
        self._fps_n += 1
        if t - self._fps_t >= 1.0:
            self.fps = self._fps_n / (t - self._fps_t) if self._fps_t else 0.0
            self._fps_t, self._fps_n = t, 0
        if self.stats_path and t >= self.stats_next:
            self.stats_next = t + 1.0
            self._write_stats(t)

    def _write_stats(self, t: float) -> None:
        try:
            rec = {'t': round(t, 2), 'state': self.engine.state, 'device': self.engine.device_name,
                   'rate': self.engine.rate, 'error': self.engine.error, 'fps': round(self.fps, 1),
                   'meterL': round(self.an.meterL, 3), 'meterR': round(self.an.meterR, 3),
                   'cues': len(self.an.cues), 'cue_total': self.an.cue_total, 'gen': self.engine.gen,
                   'mode': self.S['mode'], 'overlay': self.overlay, 'pinned': self.S['pinned'],
                   'boost': self.S['boost'], 'life': self.S['life'], 'opacity': self.S['opacity'],
                   'theme': self.S['theme'], 'panel': self.panel, 'w': self.W, 'h': self.H,
                   'hotkey': self.hotkey_ok, 'hotkeys': self.hotkeys_registered, 'tray': self.tray_added,
                   'demo': self.engine.demo is not None, 'opened': self.engine.opened_count, 'guides': self.S['guides'],
                   'meters': self.S['meters'], 'gate': self.S['gate'], 'gain': self.S['gain'],
                   'smoothing': self.S['smoothing'], 'maxCues': self.S['maxCues'], 'adaptive': self.S['adaptive'],
                   'sensitivity': self.S['sensitivity'], 'radiusBoost': self.S['radiusBoost'], 'lineWidth': self.S['lineWidth'],
                   'width': self.S['width'], 'centre': self.S['centre'], 'capture': self.engine.capture,
                   'fg': self.fg_exe, 'lastApp': self.last_app_exe, 'profile': self.active_profile,
                   'surround': self.an.surround, 'channels': self.an.channels, 'markers': len(self.an.markers),
                   'fullCircle': self.S['fullCircle'], 'mixChannels': getattr(self.engine, 'mix_channels', None),
                   'marker_total': self.an.marker_total, 'visible': self.visible,
                   'processCapture': self.S['processCapture'], 'processError': self.engine.process_error,
                   'dominant': None if self.an.dominant is None else
                   {'a': round(self.an.dominant.a, 3), 'r': round(self.an.dominant.r, 3), 'n': round(self.an.dominant.n, 3)}}
            with open(self.stats_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec) + '\n')
        except Exception:
            log_exc('stats')

    def _band_colors(self, accent):
        """Low bands warm (amber), middle bands the theme colour, high bands a light tint of it."""
        if not self.S['colourByPitch']:
            return [accent] * BAND_N
        warm = (1.0, 0.71, 0.28)
        light = tuple(accent[k] * 0.45 + 0.55 for k in range(3))
        cols = []
        for i in range(BAND_N):
            f = i / float(BAND_N - 1)
            if f < 0.5:
                u = f * 2.0
                cols.append(tuple(warm[k] * (1 - u) + accent[k] * u for k in range(3)))
            else:
                u = (f - 0.5) * 2.0
                cols.append(tuple(accent[k] * (1 - u) + light[k] * u for k in range(3)))
        return cols

    def draw(self, ctx, t: float) -> None:
        S, W, H, dpr = self.S, self.W, self.H, self.dpr
        accent = THEMES.get(S['theme'], THEMES['cyan'])
        cols = self._band_colors(accent)
        pad = 16 * dpr
        hud_h = 0 if self.overlay else 26 * dpr
        if not self.overlay:
            self._draw_panel_bg(ctx, accent)
        full = self.an.surround or S['fullCircle']
        margin = (18 if S['markers'] else 12) * dpr
        if full:
            ox = W / 2.0
            oy = hud_h + (H - hud_h) / 2.0
            R = max(10.0, min(W / 2.0 - pad - margin, (H - hud_h) / 2.0 - pad - margin))
            base_y = oy + R
        else:
            label_h = (16 if S['guides'] else 2) * dpr
            ox = W / 2.0
            oy = H - pad - label_h
            R = max(10.0, min(W / 2.0 - pad - margin, oy - pad - 8 * dpr - hud_h - (margin - 12 * dpr)))
            base_y = oy
        lw = S['lineWidth'] * dpr
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)

        if S['guides']:
            self._draw_guides(ctx, ox, oy, R, dpr, full)
        mode = S['mode']
        if mode == 'petals':
            self._draw_petals(ctx, ox, oy, R, lw, cols, t)
        elif mode == 'dots':
            self._draw_dots(ctx, ox, oy, R, lw, cols, t)
        elif mode == 'comet':
            self._draw_comet(ctx, ox, oy, R, lw, cols, t)
        elif mode == 'fan':
            self._draw_fan(ctx, ox, oy, R, cols, t)
        else:
            self._draw_line(ctx, ox, oy, R, lw, cols, t)
        if S['markers']:
            self._draw_markers(ctx, ox, oy, R, dpr, cols, t)
        if mode != 'line':
            self._draw_arrow(ctx, ox, oy, R, dpr, t)
        if S['meters'] and self.engine.state in ('live', 'demo'):
            self._draw_meters(ctx, W, H, base_y, R, dpr, accent)
        if not self.overlay:
            self._draw_hud(ctx, accent)
        if self.panel in ('help', 'help2'):
            self._draw_help(ctx)
        elif self.panel == 'welcome':
            self._draw_welcome(ctx)
        elif self.panel == 'tune':
            self._draw_tune(ctx, accent)
        elif self.panel == 'menu':
            self._draw_menu(ctx, accent)
        if self.panel not in ('menu', 'tune'):
            self.panel_rect = None
        if t < self.toast_until and self.toast_text:
            self._draw_toast(ctx, self.toast_text)

    # helpers
    @staticmethod
    def _pt(ox, oy, R, a, r):
        return ox + math.sin(a) * r * R, oy - math.cos(a) * r * R

    def _vis(self, r):
        return math.sqrt(r) if self.S['boost'] else r

    @staticmethod
    def _rounded(ctx, x, y, w, h, rad):
        rad = min(rad, w / 2.0, h / 2.0)
        ctx.new_sub_path()
        ctx.arc(x + w - rad, y + rad, rad, -math.pi / 2, 0)
        ctx.arc(x + w - rad, y + h - rad, rad, 0, math.pi / 2)
        ctx.arc(x + rad, y + h - rad, rad, math.pi / 2, math.pi)
        ctx.arc(x + rad, y + rad, rad, math.pi, 3 * math.pi / 2)
        ctx.close_path()

    @staticmethod
    def _font(ctx, size, bold=True):
        ctx.select_font_face('Segoe UI', cairo.FONT_SLANT_NORMAL,
                             cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        ctx.set_font_size(size)

    def _text(self, ctx, s, x, y, size, rgba, bold=True, align='left', vpos='baseline'):
        self._font(ctx, size, bold)
        ext = ctx.text_extents(s)
        fe = ctx.font_extents()
        if align == 'center':
            x -= ext.x_advance / 2.0
        elif align == 'right':
            x -= ext.x_advance
        if vpos == 'top':
            y += fe[0]
        elif vpos == 'middle':
            y += (fe[0] - fe[1]) / 2.0
        elif vpos == 'bottom':
            y -= fe[1]
        ctx.set_source_rgba(*rgba)
        ctx.move_to(x, y)
        ctx.show_text(s)
        return ext.x_advance

    def _wrap(self, ctx, s, size, bold, maxw):
        self._font(ctx, size, bold)
        words = s.split(' ')
        lines, cur = [], ''
        for w in words:
            cand = w if not cur else cur + ' ' + w
            if ctx.text_extents(cand).x_advance <= maxw or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    # drawing pieces
    def _draw_panel_bg(self, ctx, accent):
        W, H, dpr = self.W, self.H, self.dpr
        self._rounded(ctx, 0.5 * dpr, 0.5 * dpr, W - dpr, H - dpr, 10 * dpr)
        ctx.set_source_rgba(PANEL[0], PANEL[1], PANEL[2], 0.80)
        ctx.fill_preserve()
        if self.active:
            ctx.set_source_rgba(accent[0], accent[1], accent[2], 0.55)
        else:
            ctx.set_source_rgba(LINE[0], LINE[1], LINE[2], 1.0)
        ctx.set_line_width(1 * dpr)
        ctx.stroke()

    def _draw_hud(self, ctx, accent):
        W, dpr, S = self.W, self.dpr, self.S
        x = 12 * dpr
        y = 13 * dpr
        x += self._text(ctx, 'ECHOSCOPE', x, y, 11 * dpr, FG, True, 'left', 'middle') + 10 * dpr
        st = self.engine.state
        if st == 'live':
            col, txt = LIVE, 'Listening: ' + (self.engine.device_name or 'default device')
        elif st == 'demo':
            col, txt = WARN, self.engine.device_name
        elif st in ('starting', 'restarting'):
            col, txt = WARN, 'Opening audio…'
        else:
            col, txt = DANGER, 'Retrying: ' + (self.engine.error or 'no device')
        ctx.set_source_rgba(col[0], col[1], col[2], 1)
        ctx.arc(x + 3.5 * dpr, y, 3.5 * dpr, 0, 2 * math.pi)
        ctx.fill()
        x += 11 * dpr
        right_txt = MODE_NAMES[S['mode']].upper() + ('  ·  BOOST' if S['boost'] else '') + '  ·  SENS %d' % S['sensitivity']
        if self.active_profile:
            right_txt = self.active_profile.replace('.exe', '').upper() + '  ·  ' + right_txt
        self._font(ctx, 10 * dpr, True)
        right_w = ctx.text_extents(right_txt).x_advance
        avail = W - 12 * dpr - right_w - 14 * dpr - x
        self._font(ctx, 10 * dpr, False)
        while txt and ctx.text_extents(txt).x_advance > avail and len(txt) > 4:
            txt = txt[:-2].rstrip() + '…' if not txt.endswith('…') else txt[:-3].rstrip() + '…'
        self._text(ctx, txt, x, y, 10 * dpr, MUTED, False, 'left', 'middle')
        self._text(ctx, right_txt, W - 12 * dpr, y, 10 * dpr, accent, True, 'right', 'middle')
        hint = 'Right-click: settings   ?: help   O: overlay'
        self._text(ctx, hint, W / 2.0, self.H - 5 * dpr, 9 * dpr, (MUTED[0], MUTED[1], MUTED[2], 0.75), False, 'center', 'bottom')

    def _draw_guides_full(self, ctx, ox, oy, R, dpr):
        """Full-circle guides for surround (5.1 / 7.1) input: forward is up, back is down."""
        ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.2)
        ctx.set_line_width(1 * dpr)
        ctx.new_sub_path()
        ctx.arc(ox, oy, R, 0, 2 * math.pi)
        ctx.stroke()
        ctx.set_dash([3 * dpr, 5 * dpr])
        ctx.new_sub_path()
        ctx.arc(ox, oy, R * 0.5, 0, 2 * math.pi)
        ctx.stroke()
        ctx.set_dash([])
        ctx.move_to(ox - R, oy)
        ctx.line_to(ox + R, oy)
        ctx.move_to(ox, oy - R)
        ctx.line_to(ox, oy + R)
        ctx.stroke()
        for deg in range(-180, 180, 45):
            a = deg * math.pi / 180.0
            x0, y0 = self._pt(ox, oy, R, a, 1.0)
            x1, y1 = self._pt(ox, oy, R, a, 1.0 + (0.07 if deg == 0 else 0.045))
            ctx.move_to(x0, y0)
            ctx.line_to(x1, y1)
            ctx.stroke()
        lab = (FG[0], FG[1], FG[2], 0.55)
        self._text(ctx, 'FWD', ox, oy - R - 9 * dpr, 11 * dpr, lab, True, 'center', 'bottom')
        if self.an.surround:
            self._text(ctx, 'BACK', ox, oy + R + 9 * dpr, 10 * dpr, lab, True, 'center', 'top')
        else:
            self._text(ctx, 'BACK  ·  stereo input: front half only', ox, oy + R + 9 * dpr, 8.5 * dpr,
                       (FG[0], FG[1], FG[2], 0.4), True, 'center', 'top')
        self._text(ctx, 'L', ox - R - 6 * dpr, oy, 11 * dpr, lab, True, 'right', 'middle')
        self._text(ctx, 'R', ox + R + 6 * dpr, oy, 11 * dpr, lab, True, 'left', 'middle')
        ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.7)
        ctx.arc(ox, oy, 2.2 * dpr, 0, 2 * math.pi)
        ctx.fill()

    def _draw_markers(self, ctx, ox, oy, R, dpr, cols, t):
        """Sudden sounds leave a dot just outside the rim that fades over a few seconds."""
        for mt, a, b, strength in self.an.markers:
            age = (t - mt) / MARKER_LIFE
            if age >= 1:
                continue
            col = cols[b]
            x, y = self._pt(ox, oy, R, a, 1.07)
            el = t - mt
            if el < 0.4:  # ping ring right after the event
                ctx.set_source_rgba(col[0], col[1], col[2], (1 - el / 0.4) * 0.6)
                ctx.set_line_width(1.5 * dpr)
                ctx.new_sub_path()
                ctx.arc(x, y, (3 + el / 0.4 * 12) * dpr, 0, 2 * math.pi)
                ctx.stroke()
            ctx.set_source_rgba(col[0], col[1], col[2], (1 - age) * (0.55 + 0.45 * strength))
            ctx.new_sub_path()
            ctx.arc(x, y, (2.2 + 2.3 * strength) * dpr, 0, 2 * math.pi)
            ctx.fill()

    def _draw_guides(self, ctx, ox, oy, R, dpr, full=False):
        if full:
            self._draw_guides_full(ctx, ox, oy, R, dpr)
            return
        ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.2)
        ctx.set_line_width(1 * dpr)
        ctx.new_sub_path()
        ctx.arc(ox, oy, R, math.pi, 2 * math.pi)
        ctx.stroke()
        ctx.set_dash([3 * dpr, 5 * dpr])
        ctx.new_sub_path()
        ctx.arc(ox, oy, R * 0.5, math.pi, 2 * math.pi)
        ctx.stroke()
        ctx.set_dash([])
        ctx.move_to(ox - R, oy)
        ctx.line_to(ox + R, oy)
        ctx.stroke()
        for deg in (-90, -45, 0, 45, 90):
            a = deg * math.pi / 180.0
            x0, y0 = self._pt(ox, oy, R, a, 1.0)
            x1, y1 = self._pt(ox, oy, R, a, 1.0 + (0.07 if deg == 0 else 0.045))
            ctx.move_to(x0, y0)
            ctx.line_to(x1, y1)
            ctx.stroke()
        lab = (FG[0], FG[1], FG[2], 0.55)
        self._text(ctx, 'L', ox - R, oy + 4 * dpr, 11 * dpr, lab, True, 'center', 'top')
        self._text(ctx, 'R', ox + R, oy + 4 * dpr, 11 * dpr, lab, True, 'center', 'top')
        self._text(ctx, 'FWD', ox, oy - R - 9 * dpr, 11 * dpr, lab, True, 'center', 'bottom')
        ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.7)
        ctx.arc(ox, oy, 2.2 * dpr, 0, 2 * math.pi)
        ctx.fill()

    def _draw_petals(self, ctx, ox, oy, R, lw, cols, t):
        life = self.S['life']
        for c in self.an.cues:
            age = (t - c.t) / life
            if age >= 1:
                continue
            r = self._vis(c.r)
            if r * R < 2:
                continue
            half = lw * (0.35 + c.n * 0.7)
            tx, ty = self._pt(ox, oy, R, c.a, r)
            mx, my = self._pt(ox, oy, R, c.a, r * 0.7)
            px, py = math.cos(c.a), math.sin(c.a)
            col = cols[c.b]
            ctx.set_source_rgba(col[0], col[1], col[2], (1 - age) * (0.3 + 0.6 * c.n))
            ctx.move_to(ox, oy)
            ctx.line_to(mx + px * half, my + py * half)
            ctx.line_to(tx, ty)
            ctx.line_to(mx - px * half, my - py * half)
            ctx.close_path()
            ctx.fill()

    def _draw_dots(self, ctx, ox, oy, R, lw, cols, t):
        life = self.S['life']
        for c in self.an.cues:
            age = (t - c.t) / life
            if age >= 1:
                continue
            r = self._vis(c.r)
            if r * R < 2:
                continue
            x, y = self._pt(ox, oy, R, c.a, r)
            rad = lw * (0.7 + c.n * 1.1)
            col = cols[c.b]
            ctx.set_source_rgba(col[0], col[1], col[2], (1 - age) * 0.18)
            ctx.arc(x, y, rad * 2.4, 0, 2 * math.pi)
            ctx.fill()
            ctx.set_source_rgba(col[0], col[1], col[2], (1 - age) * (0.5 + 0.5 * c.n))
            ctx.arc(x, y, rad, 0, 2 * math.pi)
            ctx.fill()

    def _draw_comet(self, ctx, ox, oy, R, lw, cols, t):
        life = self.S['life']
        tl = life * 2.5
        trail = self.an.trail
        for i in range(1, len(trail)):
            p0, p1 = trail[i - 1], trail[i]
            age = (t - p1.t) / tl
            if age >= 1:
                continue
            x0, y0 = self._pt(ox, oy, R, p0.a, self._vis(p0.r))
            x1, y1 = self._pt(ox, oy, R, p1.a, self._vis(p1.r))
            col = cols[p1.b]
            ctx.set_source_rgba(col[0], col[1], col[2], (1 - age) * 0.85)
            ctx.set_line_width(lw * (0.3 + (1 - age) * 2.2))
            ctx.move_to(x0, y0)
            ctx.line_to(x1, y1)
            ctx.stroke()
        d = self.an.dominant
        if d is not None:
            age = (t - d.t) / life
            if age < 1:
                x, y = self._pt(ox, oy, R, d.a, self._vis(d.r))
                col = cols[d.b]
                ctx.set_source_rgba(col[0], col[1], col[2], (1 - age) * 0.35)
                ctx.set_line_width(lw * 0.6)
                ctx.move_to(ox, oy)
                ctx.line_to(x, y)
                ctx.stroke()
                ctx.set_source_rgba(col[0], col[1], col[2], (1 - age) * 0.25)
                ctx.arc(x, y, lw * 3.2, 0, 2 * math.pi)
                ctx.fill()
                ctx.set_source_rgba(col[0], col[1], col[2], 1 - age)
                ctx.arc(x, y, lw * 1.4, 0, 2 * math.pi)
                ctx.fill()

    def _draw_fan(self, ctx, ox, oy, R, cols, t):
        life = self.S['life']
        for c in self.an.cues:
            age = (t - c.t) / life
            if age >= 1:
                continue
            r = self._vis(c.r)
            if r * R < 2:
                continue
            half = (5 + 12 * c.n) * math.pi / 180.0
            col = cols[c.b]
            ctx.set_source_rgba(col[0], col[1], col[2], (1 - age) * 0.2)
            ctx.move_to(ox, oy)
            ctx.arc(ox, oy, r * R, c.a - half - math.pi / 2, c.a + half - math.pi / 2)
            ctx.close_path()
            ctx.fill()

    def _draw_line(self, ctx, ox, oy, R, lw, cols, t):
        d = self.an.dominant
        if d is None:
            return
        age = (t - d.t) / self.S['life']
        if age >= 1:
            return
        x, y = self._pt(ox, oy, R, d.a, self._vis(d.r))
        col = cols[d.b]
        ctx.set_source_rgba(col[0], col[1], col[2], (1 - age * 0.7) * 0.3)
        ctx.set_line_width(lw * 5)
        ctx.move_to(ox, oy)
        ctx.line_to(x, y)
        ctx.stroke()
        ctx.set_source_rgba(col[0], col[1], col[2], 1 - age * 0.7)
        ctx.set_line_width(lw * 2.4)
        ctx.move_to(ox, oy)
        ctx.line_to(x, y)
        ctx.stroke()

    def _draw_arrow(self, ctx, ox, oy, R, dpr, t):
        d = self.an.dominant
        if d is None or d.n < 0.05:  # no arrow for a cue too faint to draw
            return
        age = (t - d.t) / self.S['life']
        if age >= 1:
            return
        a = d.a
        tx, ty = self._pt(ox, oy, R, a, 0.955)
        bx, by = self._pt(ox, oy, R, a, 1.045)
        px, py = math.cos(a), math.sin(a)
        half = 5 * dpr
        ctx.set_source_rgba(1, 1, 1, 1 - age)
        ctx.move_to(tx, ty)
        ctx.line_to(bx + px * half, by + py * half)
        ctx.line_to(bx - px * half, by - py * half)
        ctx.close_path()
        ctx.fill()

    def _draw_meters(self, ctx, W, H, oy, R, dpr, accent):
        w = 6 * dpr
        max_h = min(R * 0.55, 90 * dpr)
        xs = (14 * dpr, W - 14 * dpr - w)
        vals = (self.an.meterL, self.an.meterR)
        for i in range(2):
            ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.12)
            ctx.rectangle(xs[i], oy - max_h, w, max_h)
            ctx.fill()
            h = max_h * clamp(vals[i], 0, 1)
            ctx.set_source_rgba(accent[0], accent[1], accent[2], 1)
            ctx.rectangle(xs[i], oy - h, w, h)
            ctx.fill()
            if not self.S['guides']:
                self._text(ctx, 'LR'[i], xs[i] + w / 2.0, oy + 4 * dpr, 10 * dpr, (FG[0], FG[1], FG[2], 0.55), True, 'center', 'top')

    def _draw_toast(self, ctx, text):
        W, H, dpr = self.W, self.H, self.dpr
        self._font(ctx, 11 * dpr, True)
        tw = ctx.text_extents(text).x_advance
        maxw = W - 24 * dpr
        if tw > maxw:
            lines = self._wrap(ctx, text, 11 * dpr, True, maxw - 28 * dpr)
        else:
            lines = [text]
        lh = 15 * dpr
        bw = min(maxw, max(ctx.text_extents(l).x_advance for l in lines) + 28 * dpr)
        bh = lh * len(lines) + 10 * dpr
        x = (W - bw) / 2.0
        y = H - 12 * dpr - bh - (14 * dpr if not self.overlay else 0)
        self._rounded(ctx, x, y, bw, bh, bh / 2.0)
        ctx.set_source_rgba(PANEL[0], PANEL[1], PANEL[2], 0.95)
        ctx.fill_preserve()
        ctx.set_source_rgba(LINE[0], LINE[1], LINE[2], 1)
        ctx.set_line_width(1 * dpr)
        ctx.stroke()
        for i, l in enumerate(lines):
            self._text(ctx, l, W / 2.0, y + 5 * dpr + i * lh + lh / 2.0, 11 * dpr, FG, True, 'center', 'middle')

    def _panel_box(self, ctx, title, blocks, keys=None):
        """Draw a centred text panel; blocks are (kind, text) with kind in h/p/n/m. Scales down to fit."""
        W, H, dpr = self.W, self.H, self.dpr
        bw = min(W - 16 * dpr, 560 * dpr)
        scale = 1.0
        for _ in range(8):
            padx, pady = 16 * dpr * scale, 11 * dpr * scale
            inner = bw - 2 * padx
            title_sz, body_sz, head_sz = 16 * dpr * scale, 12 * dpr * scale, 10.5 * dpr * scale
            lh = body_sz * 1.32
            layout = []  # (kind, text or lines, x_offset)
            total = pady + title_sz * 1.25 + 6 * dpr * scale
            if keys:
                kcol = 78 * dpr * scale
                for k, d in keys:
                    lines = self._wrap(ctx, d, body_sz, False, inner - kcol)
                    layout.append(('k', (k, lines), 0))
                    total += lh * len(lines) + 2 * dpr * scale
                total += 4 * dpr * scale
            for kind, text in blocks:
                if kind == 'h':
                    layout.append(('h', [text], 0))
                    total += head_sz * 1.3 + 8 * dpr * scale
                else:
                    lines = self._wrap(ctx, text, body_sz, False, inner)
                    layout.append((kind, lines, 0))
                    total += lh * len(lines) + 6 * dpr * scale
            total += 4 * dpr * scale + body_sz * 1.3 + pady  # footer line
            if total <= H - 8 * dpr or scale <= 0.6:
                break
            scale *= 0.92
        bh = total
        x, y = (W - bw) / 2.0, max(4 * dpr, (H - bh) / 2.0)
        self._rounded(ctx, x, y, bw, bh, 10 * dpr)
        ctx.set_source_rgba(PANEL[0], PANEL[1], PANEL[2], 0.97)
        ctx.fill_preserve()
        ctx.set_source_rgba(LINE[0], LINE[1], LINE[2], 1)
        ctx.set_line_width(1 * dpr)
        ctx.stroke()
        cy = y + pady
        self._text(ctx, title.upper(), x + padx, cy, title_sz, FG, True, 'left', 'top')
        cy += title_sz * 1.25 + 6 * dpr * scale
        for kind, content, _ in layout:
            if kind == 'k':
                k, lines = content
                self._text(ctx, k, x + padx, cy, body_sz, THEMES[self.S['theme']], True, 'left', 'top')
                for l in lines:
                    self._text(ctx, l, x + padx + kcol, cy, body_sz, FG, False, 'left', 'top')
                    cy += lh
                cy += 2 * dpr * scale
            elif kind == 'h':
                self._text(ctx, content[0].upper(), x + padx, cy, head_sz, MUTED, True, 'left', 'top')
                cy += head_sz * 1.3 + 8 * dpr * scale
            else:
                col = MUTED if kind == 'm' else FG
                for l in content:
                    self._text(ctx, l, x + padx, cy, body_sz, col, False, 'left', 'top')
                    cy += lh
                cy += 6 * dpr * scale
        return x, y, bw, bh, padx, body_sz, scale

    def _draw_tune(self, ctx, accent):
        """Right-hand column of live sliders; the radar stays visible on the left while tuning."""
        W, H, dpr, S = self.W, self.H, self.dpr, self.S
        pw = min(210 * dpr, W * 0.55)
        top = (30 if not self.overlay else 6) * dpr
        x, y, ph = W - pw - 6 * dpr, top, H - top - 6 * dpr
        self.panel_rect = (x, y, pw, ph)
        self._rounded(ctx, x, y, pw, ph, 8 * dpr)
        ctx.set_source_rgba(PANEL[0], PANEL[1], PANEL[2], 0.93)
        ctx.fill_preserve()
        ctx.set_source_rgba(accent[0], accent[1], accent[2], 0.5)
        ctx.set_line_width(1 * dpr)
        ctx.stroke()
        pad = 10 * dpr
        title_h = 20 * dpr
        foot_h = 16 * dpr
        avail = ph - title_h - foot_h - pad
        rh = clamp(avail / len(TUNE_ROWS), 14 * dpr, 30 * dpr)
        fs = clamp(rh * 0.36, 7 * dpr, 10.5 * dpr)
        self._text(ctx, 'TUNING', x + pad, y + 7 * dpr, 11 * dpr, FG, True, 'left', 'top')
        self._text(ctx, 'Tab closes', x + pw - pad, y + 7 * dpr, 8.5 * dpr, MUTED, False, 'right', 'top')
        ry = y + title_h + 4 * dpr
        self.tune_rows = []
        for i, (key, label, kind, lo, hi, step, fmt, choices) in enumerate(TUNE_ROWS):
            self.tune_rows.append((ry, ry + rh, i, x + pad, pw - 2 * pad))
            v = S[key]
            sel = (i == self.tune_row)
            if sel:
                self._rounded(ctx, x + 4 * dpr, ry, pw - 8 * dpr, rh - 2 * dpr, 4 * dpr)
                ctx.set_source_rgba(accent[0], accent[1], accent[2], 0.16)
                ctx.fill()
            col = accent if sel else FG
            self._text(ctx, label, x + pad, ry + rh * 0.36, fs, col, sel, 'left', 'middle')
            self._text(ctx, fmt(v), x + pw - pad, ry + rh * 0.36, fs, col, True, 'right', 'middle')
            # slider track
            if kind == 'bool':
                frac = 1.0 if v else 0.0
            elif kind == 'choice':
                try:
                    j = min(range(len(choices)), key=lambda k: abs(float(choices[k]) - float(v)))
                except Exception:
                    j = 0
                frac = j / max(1, len(choices) - 1)
            else:
                frac = (float(v) - lo) / float(hi - lo) if hi > lo else 0.0
            tx, ty, tw = x + pad, ry + rh * 0.72, pw - 2 * pad
            ctx.set_source_rgba(FG[0], FG[1], FG[2], 0.12)
            ctx.rectangle(tx, ty, tw, 2.5 * dpr)
            ctx.fill()
            ctx.set_source_rgba(accent[0], accent[1], accent[2], 0.95 if sel else 0.6)
            ctx.rectangle(tx, ty, tw * clamp(frac, 0, 1), 2.5 * dpr)
            ctx.fill()
            if sel:
                ctx.arc(tx + tw * clamp(frac, 0, 1), ty + 1.25 * dpr, 3.5 * dpr, 0, 2 * math.pi)
                ctx.fill()
            ry += rh
        self._text(ctx, '↑↓ row     ←→ adjust     or click a slider', x + pw / 2, y + ph - 6 * dpr, 8.5 * dpr, MUTED, False, 'center', 'bottom')

    def _tune_mouse(self, x: float, y: float) -> None:
        """Click a row to select it; a click on its slider track sets the value by position."""
        for y0, y1, i, tx, tw in self.tune_rows:
            if y0 <= y < y1:
                self.tune_row = i
                key, label, kind, lo, hi, step, fmt, choices = TUNE_ROWS[i]
                frac = clamp((x - tx) / tw, 0.0, 1.0) if tw > 0 else 0.0
                if kind == 'bool':
                    self.tune_adjust(+1)  # toggles
                    return
                if kind == 'choice':
                    v = choices[int(round(frac * (len(choices) - 1)))]
                elif kind == 'int':
                    v = int(round(lo + frac * (hi - lo)))
                else:
                    v = clamp(round((lo + frac * (hi - lo)) / step) * step, lo, hi)
                    v = float('%.4g' % v)
                self.S[key] = v
                self._sync_profile()
                save_settings(self.S)
                return

    def _draw_help(self, ctx):
        if self.panel == 'help':
            x, y, bw, bh, padx, body_sz, scale = self._panel_box(ctx, 'Controls  (1 / 2)', [], HELP_KEYS)
            foot = 'Space: reading and tuning tips      Esc: close'
        else:
            x, y, bw, bh, padx, body_sz, scale = self._panel_box(ctx, 'Reading and tuning  (2 / 2)', HELP_TEXT)
            foot = 'Esc or Space: close'
        self._text(ctx, foot, x + bw - padx, y + bh - 12 * self.dpr * scale, body_sz, MUTED, True, 'right', 'bottom')

    def _draw_welcome(self, ctx):
        x, y, bw, bh, padx, body_sz, scale = self._panel_box(ctx, 'Welcome to EchoScope', WELCOME_TEXT)
        acc = THEMES[self.S['theme']]
        self._text(ctx, 'Enter: got it      Space: try the demo      D: don\'t show again', x + bw - padx, y + bh - 12 * self.dpr * scale, body_sz, acc, True, 'right', 'bottom')


# ---------------------------------------------------------------------------
def main(argv) -> int:
    stats = None
    if '--stats' in argv:
        i = argv.index('--stats')
        stats = argv[i + 1] if i + 1 < len(argv) else os.path.join(APP_DIR, 'stats.jsonl')
    log('%s %s starting (python %s, frozen=%s)' % (APP_NAME, VERSION, sys.version.split()[0], getattr(sys, 'frozen', False)))
    app = App(stats_path=stats)
    if '--capture-pid' in argv:  # debugging aid: capture one process from the start
        try:
            pid = int(argv[argv.index('--capture-pid') + 1])
            app.engine.set_process_target(pid, process_name(pid) or 'pid %d' % pid)
            app.engine.want_process = True
            app.last_app_pid, app.last_app_exe = pid, app.engine.process_name
        except Exception:
            log_exc('--capture-pid')
    try:
        app.create()
        app.run()
    except Exception as e:
        log_exc('main')
        try:
            user32.MessageBoxW(None, 'EchoScope hit an error and has to close:\n\n%s\n\nDetails were written to errorlog.txt next to the EXE.' % e, APP_NAME, 0x10)
        except Exception:
            pass
        return 1
    log('exit')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
