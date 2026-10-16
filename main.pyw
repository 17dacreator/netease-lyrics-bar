"""网易云桌面歌词 — entry point: tray icon, global hotkeys, single instance."""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from PySide6.QtCore import Qt, QAbstractNativeEventFilter, QPointF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction, QLinearGradient
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from config import Config, DATA_DIR
from ncm_reader import NcmReader
from audiosync import AudioSync
from overlay import LyricWindow

INSTANCE_KEY = "ncm-desktop-lyrics-" + os.environ.get("USERNAME", "user")
SHORTCUT_NAME = "网易云桌面歌词.lnk"
STARTUP_DIR = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")

user32 = ctypes.windll.user32
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
MOD_ALT, MOD_CTRL, MOD_SHIFT = 0x1, 0x2, 0x4
WM_HOTKEY = 0x0312
VK_PLUS, VK_MINUS, VK_ADD, VK_SUBTRACT = 0xBB, 0xBD, 0x6B, 0x6D

HOTKEYS = [  # (modifiers, key, action)
    (MOD_CTRL | MOD_ALT, VK_PLUS, ("nudge", 0.01)),
    (MOD_CTRL | MOD_ALT, VK_MINUS, ("nudge", -0.01)),
    (MOD_CTRL | MOD_ALT, VK_ADD, ("nudge", 0.01)),
    (MOD_CTRL | MOD_ALT, VK_SUBTRACT, ("nudge", -0.01)),
    (MOD_CTRL | MOD_ALT | MOD_SHIFT, VK_PLUS, ("nudge", 0.1)),
    (MOD_CTRL | MOD_ALT | MOD_SHIFT, VK_MINUS, ("nudge", -0.1)),
    (MOD_CTRL | MOD_ALT, ord("0"), ("reset", None)),
    (MOD_CTRL | MOD_ALT, ord("L"), ("lock", None)),
    (MOD_CTRL | MOD_ALT, ord("H"), ("show", None)),
]


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.failed = []
        for i, (mods, vk, _) in enumerate(HOTKEYS, start=1):
            if not user32.RegisterHotKey(None, i, mods, vk):
                self.failed.append(i)

    def nativeEventFilter(self, event_type, message):
        msg = wt.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY and 1 <= msg.wParam <= len(HOTKEYS):
            kind, arg = HOTKEYS[msg.wParam - 1][2]
            w = self.window
            if kind == "nudge":
                w.adjust_offset(arg)
            elif kind == "reset":
                w.reset_offset()
            elif kind == "lock":
                w.set_locked(not w.cfg["locked"])
            elif kind == "show":
                w.set_visible(not w.cfg["visible"])
            return True, 0
        return False, 0


def make_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, 0, 64)
    grad.setColorAt(0, QColor("#ff5a6e"))
    grad.setColorAt(1, QColor("#d81e3c"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(38)
    f.setWeight(QFont.Weight.Bold)
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(pm.rect(), Qt.AlignCenter, "词")
    p.end()
    return pm


def pythonw_exe():
    exe = sys.executable
    alt = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return alt if os.path.exists(alt) else exe


def write_shortcut(path, icon_path):
    import win32com.client
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortCut(path)
    if getattr(sys, "frozen", False):            # packaged build: point at the exe itself
        sc.TargetPath = sys.executable
        sc.WorkingDirectory = os.path.dirname(sys.executable)
    else:
        sc.TargetPath = pythonw_exe()
        sc.Arguments = f'"{os.path.join(APP_DIR, "main.pyw")}"'
        sc.WorkingDirectory = APP_DIR
    if os.path.exists(icon_path):
        sc.IconLocation = icon_path
    sc.Description = "网易云桌面歌词"
    sc.save()


class Tray(QSystemTrayIcon):
    def __init__(self, window, hotkeys, icon, icon_path):
        super().__init__(icon)
        self.w, self.icon_path = window, icon_path
        self.setToolTip("网易云桌面歌词")
        menu = self.menu = QMenu()

        self.act_show = self._action("显示歌词", lambda on: window.set_visible(on), checkable=True)
        self.act_lock = self._action("锁定（鼠标穿透）", lambda on: window.set_locked(on), checkable=True)
        self.act_trans = self._action("显示翻译", lambda _: window.toggle_trans(), checkable=True)
        self.act_next = self._action("显示下一句", lambda _: window.toggle_next(), checkable=True)
        self.act_reveal = self._action("逐词出现", lambda _: window.toggle_reveal(), checkable=True)
        self.act_fill = self._action("网易云没词时自动补歌词", lambda _: window.toggle_autofill(),
                                     checkable=True)
        self.act_ai = self._action("哪儿都没词时用 AI 听写", lambda _: window.toggle_ai(), checkable=True)
        self.act_cal = self._action("按音频自动校准逐字时间", lambda _: window.toggle_calibrate(), checkable=True)
        self.act_sync = self._action("拖进度条后听声音自动对时", self._toggle_sync, checkable=True)
        if not window.worker.available:              # packaged build without the AI parts
            for act in (self.act_ai, self.act_cal):
                act.setEnabled(False)
                act.setText(act.text() + "（这个版本没有）")
        theme_menu = menu.addMenu("配色")
        theme_menu.aboutToShow.connect(lambda: window.fill_theme_menu(theme_menu))
        menu.addSeparator()
        self._action("全部歌曲偏移归零", lambda _: self._reset_global())
        self._action("回到默认位置", lambda _: window.reset_position())
        self.act_boot = self._action("开机自动启动", self._toggle_boot, checkable=True)
        menu.addSeparator()
        tips = ["Ctrl+Alt+ = / −　微调 0.01 秒", "Ctrl+Alt+Shift+ = / −　微调 0.1 秒",
                "Ctrl+Alt+0　偏移归零", "Ctrl+Alt+L　锁定 / 解锁", "Ctrl+Alt+H　显示 / 隐藏"]
        if hotkeys.failed:
            tips.append("（有快捷键被别的软件占用了）")
        for t in tips:
            menu.addAction(t).setEnabled(False)
        menu.addSeparator()
        self._action("退出", lambda _: QApplication.quit())
        self.setContextMenu(menu)
        menu.aboutToShow.connect(self.sync)
        self.activated.connect(self._on_click)
        window.changed.connect(self.sync)
        window.hidden_by_user.connect(self._hidden_tip)
        self.sync()

    def _action(self, text, slot, checkable=False):
        a = QAction(text, self.menu)
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        self.menu.addAction(a)
        return a

    def sync(self):
        cfg = self.w.cfg
        self.act_show.setChecked(cfg["visible"])
        self.act_lock.setChecked(cfg["locked"])
        self.act_trans.setChecked(cfg["show_trans"])
        self.act_next.setChecked(cfg["show_next"])
        self.act_reveal.setChecked(cfg["reveal"])
        self.act_fill.setChecked(cfg["autofill"])
        self.act_ai.setChecked(cfg["ai_transcribe"])
        self.act_cal.setChecked(cfg["calibrate"])
        self.act_sync.setChecked(cfg["audio_sync"])
        self.act_boot.setChecked(os.path.exists(os.path.join(STARTUP_DIR, SHORTCUT_NAME)))

    def _toggle_sync(self, on):
        self.w.cfg["audio_sync"] = bool(on)
        self.w.cfg.save()

    def _reset_global(self):
        scope = self.w.cfg["scope"]
        self.w.cfg["scope"] = "all"
        self.w.reset_offset()
        self.w.cfg["scope"] = scope
        self.w._refresh_toolbar()

    def _toggle_boot(self, on):
        path = os.path.join(STARTUP_DIR, SHORTCUT_NAME)
        try:
            if on:
                write_shortcut(path, self.icon_path)
            elif os.path.exists(path):
                os.remove(path)
        except Exception as e:
            self.showMessage("网易云桌面歌词", f"设置开机启动失败：{e}")
        self.sync()

    def _on_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.w.set_visible(True)
            if self.w.cfg["locked"]:
                self.w.set_locked(False)

    def _hidden_tip(self):
        if not self.w.cfg.get("hide_tip_shown"):
            self.showMessage("网易云桌面歌词", "歌词藏起来了，点右下角托盘里的「词」图标就能再打开。",
                             QSystemTrayIcon.Information, 4000)
            self.w.cfg["hide_tip_shown"] = True
            self.w.cfg.save()


def log_crashes():
    """Unhandled errors go to data/crash.log (a packaged build has no console to show them)."""
    import traceback

    def hook(kind, value, tb):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "crash.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "".join(traceback.format_exception(kind, value, tb)) + "\n")
        except OSError:
            pass
        sys.__excepthook__(kind, value, tb)
    sys.excepthook = hook


def install(target_dir=None):
    """`--install`: put a shortcut on the desktop (or in target_dir) and the icon next to the app."""
    app = QApplication(sys.argv)
    os.makedirs(DATA_DIR, exist_ok=True)
    icon_path = os.path.join(DATA_DIR, "icon.ico")
    make_icon().save(icon_path, "ICO")
    if target_dir is None:
        import win32com.client
        target_dir = win32com.client.Dispatch("WScript.Shell").SpecialFolders("Desktop")
    write_shortcut(os.path.join(target_dir, SHORTCUT_NAME), icon_path)
    del app
    return 0


def selftest():
    """`--selftest`: check the parts a friend's PC needs and write data/selftest.log (no lyrics in it)."""
    out = []

    def check(name, fn):
        try:
            out.append(f"OK   {name}: {fn()}")
        except Exception as e:
            out.append(f"FAIL {name}: {type(e).__name__}: {e}")

    reader = NcmReader()
    time.sleep(1.5)
    state, song, duration, pos = reader.snapshot()
    out.append(f"INFO NetEase: {state}, song id {'yes' if song else 'none'}, {pos:.1f}s / {duration:.0f}s")

    def decode():
        import audiosync
        if not song.isdigit():
            return "no song to decode (play one and rerun)"
        ref = audiosync.load_reference(song, duration, lambda: False, timeout=20)
        return "not cached yet" if ref is None else f"cached song decoded, {len(ref) / audiosync.FS:.0f}s"

    def loopback():
        import soundcard as sc
        devices = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
        return f"{len(devices)} loopback device(s), default speaker '{sc.default_speaker().name}'"

    def lyric_api():
        import lyrics
        return f"{len(lyrics.netease_lyrics(song, lyrics.song_meta(song)).lines)} lines" if song.isdigit() else "skipped"

    check("decode cached audio (PyAV)", decode)
    check("speaker loopback (soundcard)", loopback)
    check("NetEase lyric API", lyric_api)
    reader.stop()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "selftest.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return 0


def main():
    log_crashes()
    if "--selftest" in sys.argv:
        return selftest()
    if "--install" in sys.argv:
        i = sys.argv.index("--install")
        return install(sys.argv[i + 1] if i + 1 < len(sys.argv) else None)
    ctypes.windll.winmm.timeBeginPeriod(1)
    sys.setswitchinterval(0.001)     # background threads hand the UI thread its turn sooner
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    probe = QLocalSocket()
    probe.connectToServer(INSTANCE_KEY)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(INSTANCE_KEY)
    server = QLocalServer()
    server.listen(INSTANCE_KEY)

    os.makedirs(DATA_DIR, exist_ok=True)
    pm = make_icon()
    icon_path = os.path.join(DATA_DIR, "icon.ico")
    if not os.path.exists(icon_path):
        pm.save(icon_path, "ICO")
    icon = QIcon(pm)
    app.setWindowIcon(icon)

    cfg = Config()
    reader = NcmReader()
    sync = AudioSync(reader, cfg)
    window = LyricWindow(cfg, reader, sync)
    hotkeys = HotkeyFilter(window)
    app.installNativeEventFilter(hotkeys)
    tray = Tray(window, hotkeys, icon, icon_path)
    tray.show()

    def on_second_instance():
        sock = server.nextPendingConnection()
        if sock:
            sock.close()
        window.set_visible(True)
        if cfg["locked"]:
            window.set_locked(False)
    server.newConnection.connect(on_second_instance)

    window.start()
    if cfg["first_run"]:
        window.toast("歌词已就绪，在网易云放首歌试试", 5.0)
        cfg["first_run"] = False
        cfg.save()
    code = app.exec()
    window.stop_ai()
    window.worker.stop()
    sync.stop()
    reader.stop()
    cfg.save()
    return code


if __name__ == "__main__":
    sys.exit(main())
