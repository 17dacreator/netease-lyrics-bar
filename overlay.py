"""Transparent, always-on-top desktop lyric window."""
import ctypes
import ctypes.wintypes as wt
import math
import threading
import time
from collections import OrderedDict

from PySide6.QtCore import Qt, QTimer, QRect, QRectF, QPointF, Signal
from PySide6.QtGui import (QPainter, QPainterPath, QFont, QFontMetricsF, QColor, QPen, QLinearGradient,
                           QCursor, QGuiApplication, QImage)
from PySide6.QtWidgets import QWidget, QPushButton, QHBoxLayout

import lyrics as lyr

user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]

FONT_FAMILY = "Microsoft YaHei UI"
TOOLBAR_H = 38
EDGE = 8
FONT_MIN, FONT_MAX = 18, 72

# name, sung gradient (top, bottom), unsung colour
THEMES = [
    ("网易红", "#ff8a9b", "#ff2d55", "#ffffff"),
    ("清新蓝", "#8ff0ff", "#3a8dff", "#ffffff"),
    ("暖金", "#fff1b8", "#ffa94d", "#ffffff"),
    ("薄荷绿", "#c8ffe4", "#20d39b", "#ffffff"),
    ("纯白", "#ffffff", "#f1f1f1", "#9a9a9a"),
]

BUTTON_CSS = """
QPushButton { color: #f4f4f4; background: rgba(255,255,255,30); border: none; border-radius: 7px;
              padding: 4px 9px; font: 13px "Microsoft YaHei UI"; }
QPushButton:hover { background: rgba(255,255,255,70); }
QPushButton:checked { background: rgba(255,70,105,170); }
QPushButton#offset { min-width: 104px; font-weight: bold; background: rgba(255,255,255,48); }
"""


def fmt_offset(v):
    if abs(v) < 0.005:
        return "不偏移"
    return f"{'提前' if v > 0 else '推迟'} {abs(v):.2f}s"


class _Shape:
    """A pre-rendered line of text in one colour role, plus its word boundaries."""
    __slots__ = ("image", "width", "height", "margin", "bounds")


class OffsetButton(QPushButton):
    nudge = Signal(float)
    reset = Signal()

    def wheelEvent(self, e):
        step = 0.1 if e.modifiers() & Qt.ShiftModifier else 0.01
        self.nudge.emit(step if e.angleDelta().y() > 0 else -step)

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.reset.emit()
        else:
            super().mousePressEvent(e)


class UnlockBubble(QWidget):
    clicked = Signal()

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        btn = QPushButton("解锁", self)
        btn.setStyleSheet(BUTTON_CSS + "QPushButton { background: rgba(20,20,24,200); padding: 5px 12px; }")
        btn.clicked.connect(self.clicked)
        btn.adjustSize()
        self.resize(btn.size())


class LyricWindow(QWidget):
    changed = Signal()            # lock / visibility / theme / translation toggled
    hidden_by_user = Signal()
    _fetched = Signal(int, str, object, str, str)

    def __init__(self, cfg, reader):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.cfg, self.reader = cfg, reader
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("网易云桌面歌词")

        self.song_id = ""
        self.title = ""
        self.lyrics = None
        self.fetch_state = ""         # loading | ok | failed | none
        self._fetch_gen = 0
        self._fetched.connect(self._on_fetched)

        self.hovered = False
        self._hover_left_at = 0.0
        self.toast_text, self.toast_until = "", 0.0
        self._frame = None
        self._force_paint = True
        self._wake = 0.0
        self._shapes = OrderedDict()

        self._build_toolbar()
        self.bubble = UnlockBubble()
        self.bubble.clicked.connect(lambda: self.set_locked(False))

        self._save_timer = QTimer(self, singleShot=True, interval=400, timeout=self.cfg.save)
        self._tick_timer = QTimer(self, timerType=Qt.PreciseTimer, interval=8, timeout=self._tick)
        self._topmost_timer = QTimer(self, interval=2000, timeout=self._keep_topmost)
        self._apply_geometry(initial=True)

    # -- toolbar -----------------------------------------------------------

    def _build_toolbar(self):
        bar = self.toolbar = QWidget(self)
        bar.setStyleSheet(BUTTON_CSS)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 6, 10, 0)
        row.setSpacing(5)

        def button(text, tip, slot, checkable=False, name=None, cls=QPushButton):
            b = cls(text, bar)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setCheckable(checkable)
            if name:
                b.setObjectName(name)
            b.clicked.connect(slot)
            row.addWidget(b)
            return b

        button("−0.1", "歌词推迟 0.1 秒", lambda: self.adjust_offset(-0.1))
        button("−0.01", "歌词推迟 0.01 秒", lambda: self.adjust_offset(-0.01))
        self.offset_btn = button("", "滚轮 ±0.01 秒（按住 Shift ±0.1）· 右键归零", lambda: None,
                                 name="offset", cls=OffsetButton)
        self.offset_btn.nudge.connect(self.adjust_offset)
        self.offset_btn.reset.connect(self.reset_offset)
        button("+0.01", "歌词提前 0.01 秒", lambda: self.adjust_offset(0.01))
        button("+0.1", "歌词提前 0.1 秒", lambda: self.adjust_offset(0.1))
        self.scope_btn = button("", "点一下切换：调全部歌曲 / 只调这首", self.toggle_scope)
        row.addStretch(1)
        self.trans_btn = button("译", "显示翻译", self.toggle_trans, checkable=True)
        self.next_btn = button("下句", "在下面显示下一句歌词", self.toggle_next, checkable=True)
        button("A−", "字小一点", lambda: self.change_font(-2))
        button("A+", "字大一点", lambda: self.change_font(2))
        button("配色", "换一套颜色", self.next_theme)
        button("锁定", "锁定后鼠标可以穿透歌词", lambda: self.set_locked(True))
        button("✕", "隐藏歌词（托盘图标里可以再打开）", self.hide_by_user)
        bar.hide()
        self._refresh_toolbar()

    def _refresh_toolbar(self):
        self.offset_btn.setText(fmt_offset(self._scope_value()))
        self.scope_btn.setText("全部歌曲" if self.cfg["scope"] == "all" else "只调这首")
        self.trans_btn.setChecked(bool(self.cfg["show_trans"]))
        self.next_btn.setChecked(bool(self.cfg["show_next"]))

    # -- offsets -----------------------------------------------------------

    def effective_offset(self):
        return self.cfg["global_offset"] + self.cfg["song_offsets"].get(self.song_id, 0.0)

    def _scope_value(self):
        if self.cfg["scope"] == "song":
            return self.cfg["song_offsets"].get(self.song_id, 0.0)
        return self.cfg["global_offset"]

    def _set_scope_value(self, v):
        v = round(v, 2)
        if self.cfg["scope"] == "song":
            if not self.song_id:
                self.toast("现在没在放歌")
                return False
            if abs(v) < 0.005:
                self.cfg["song_offsets"].pop(self.song_id, None)
            else:
                self.cfg["song_offsets"][self.song_id] = v
        else:
            self.cfg["global_offset"] = 0.0 if abs(v) < 0.005 else v
        return True

    def adjust_offset(self, delta):
        if self._set_scope_value(self._scope_value() + delta):
            self._after_offset_change()

    def reset_offset(self):
        if self._set_scope_value(0.0):
            self._after_offset_change()

    def _after_offset_change(self):
        self._refresh_toolbar()
        who = "全部歌曲" if self.cfg["scope"] == "all" else "这首歌"
        self.toast(f"{who}：歌词{fmt_offset(self._scope_value())}".replace("歌词不偏移", "不偏移"))
        self._save_timer.start()

    def toggle_scope(self):
        self.cfg["scope"] = "song" if self.cfg["scope"] == "all" else "all"
        self._refresh_toolbar()
        self._save_timer.start()

    # -- other settings ------------------------------------------------------

    def toggle_trans(self):
        self.cfg["show_trans"] = not self.cfg["show_trans"]
        self._refresh_toolbar()
        self._save_timer.start()
        self.changed.emit()

    def toggle_next(self):
        self.cfg["show_next"] = not self.cfg["show_next"]
        self._refresh_toolbar()
        self._save_timer.start()
        self.changed.emit()

    def change_font(self, delta):
        size = max(FONT_MIN, min(FONT_MAX, self.cfg["font_size"] + delta))
        if size != self.cfg["font_size"]:
            bottom = self.y() + self.height()
            self.cfg["font_size"] = size
            self._shapes.clear()
            self.setFixedHeight(self._calc_height())
            self.move(self.x(), bottom - self.height())
            self._layout_toolbar()
            self._save_timer.start()

    def next_theme(self):
        self.set_theme((self.cfg["theme"] + 1) % len(THEMES))
        self.toast(THEMES[self.cfg["theme"]][0])

    def set_theme(self, idx):
        self.cfg["theme"] = idx
        self._shapes.clear()
        self._force_paint = True
        self._save_timer.start()
        self.changed.emit()

    def set_locked(self, on):
        self.cfg["locked"] = bool(on)
        handle = self.windowHandle()
        if handle:
            handle.setFlag(Qt.WindowTransparentForInput, bool(on))
        if on:
            self.toolbar.hide()
        else:
            self.bubble.hide()
        self.toast("已锁定" if on else "已解锁")
        self._save_timer.start()
        self.changed.emit()

    def set_visible(self, on):
        self.cfg["visible"] = bool(on)
        if on:
            self.show()
            self.set_locked(self.cfg["locked"])
            self._keep_topmost()
        else:
            self.hide()
            self.bubble.hide()
        self._save_timer.start()
        self.changed.emit()

    def hide_by_user(self):
        self.set_visible(False)
        self.hidden_by_user.emit()

    def reset_position(self):
        self.cfg["x"] = self.cfg["y"] = self.cfg["width"] = None
        self._apply_geometry(initial=True)
        self.set_visible(True)

    def toast(self, text, seconds=1.6):
        self.toast_text, self.toast_until = text, time.monotonic() + seconds

    def start(self):
        self._tick_timer.start()
        self._topmost_timer.start()
        if self.cfg["visible"]:
            self.set_visible(True)

    # -- geometry ------------------------------------------------------------

    def _sub_size(self):
        return max(14, round(self.cfg["font_size"] * 0.6))

    def _calc_height(self):
        fs = self.cfg["font_size"]
        return int(TOOLBAR_H + fs * 1.5 + self._sub_size() * 1.6 + 12)

    def _rows(self):
        fs = self.cfg["font_size"]
        top1 = TOOLBAR_H + 2
        h1 = fs * 1.5
        return (top1, h1), (top1 + h1, self._sub_size() * 1.6)

    def _apply_geometry(self, initial=False):
        h = self._calc_height()
        self.setFixedHeight(h)
        self.setMinimumWidth(640)
        avail = QGuiApplication.primaryScreen().availableGeometry()
        w = self.cfg["width"] or int(min(1100, max(700, avail.width() * 0.62)))
        x, y = self.cfg["x"], self.cfg["y"]
        if x is None or y is None or not any(s.availableGeometry().intersects(QRect(x, y, w, h))
                                             for s in QGuiApplication.screens()):
            x = avail.x() + (avail.width() - w) // 2
            y = avail.y() + avail.height() - h - 30
        self.setGeometry(x, y, w, h)
        self._layout_toolbar()

    def _layout_toolbar(self):
        self.toolbar.setGeometry(0, 0, self.width(), TOOLBAR_H)

    def resizeEvent(self, e):
        self._layout_toolbar()
        self._remember_geometry()

    def moveEvent(self, e):
        self._remember_geometry()

    def _remember_geometry(self):
        if self.isVisible():
            self.cfg["x"], self.cfg["y"], self.cfg["width"] = self.x(), self.y(), self.width()
            self._save_timer.start()

    def _keep_topmost(self):
        if self.isVisible():
            flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
            user32.SetWindowPos(int(self.winId()), wt.HWND(-1), 0, 0, 0, 0, flags)

    # -- mouse (unlocked only; locked windows are transparent for input) ----

    def _edge_at(self, x):
        if x < EDGE:
            return Qt.LeftEdge
        if x > self.width() - EDGE:
            return Qt.RightEdge
        return None

    def mouseMoveEvent(self, e):
        edge = self._edge_at(e.position().x())
        self.setCursor(Qt.SizeHorCursor if edge else Qt.SizeAllCursor)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self.cfg["locked"]:
            return
        edge = self._edge_at(e.position().x())
        handle = self.windowHandle()
        if edge:
            handle.startSystemResize(edge)
        else:
            handle.startSystemMove()

    # -- song / lyric loading ------------------------------------------------

    def _load_song(self, song_id, duration):
        self.song_id, self.title, self.lyrics = song_id, "", None
        self._fetch_gen += 1
        self._refresh_toolbar()
        if not song_id:
            self.fetch_state = ""
            return
        self.fetch_state = "loading"
        gen = self._fetch_gen
        threading.Thread(target=self._fetch_worker, args=(gen, song_id, duration), daemon=True).start()

    def _fetch_worker(self, gen, song_id, duration):
        title = lyr.song_title(song_id)
        if not song_id.isdigit():
            self._fetched.emit(gen, song_id, None, title, "none")
            return
        try:
            ly = lyr.fetch_lyrics(song_id, duration)
            state = "ok" if ly.lines else "none"
        except Exception:
            ly, state = None, "failed"
        self._fetched.emit(gen, song_id, ly, title, state)

    def _on_fetched(self, gen, song_id, ly, title, state):
        if gen != self._fetch_gen:
            return
        self.lyrics, self.title, self.fetch_state = ly, title, state
        self._force_paint = True
        if state == "failed":
            QTimer.singleShot(15000, lambda: self._retry(gen))

    def _retry(self, gen):
        if gen == self._fetch_gen and self.fetch_state == "failed":
            _, _, duration, _ = self.reader.snapshot()
            self._load_song(self.song_id, duration)

    # -- per-frame update ------------------------------------------------------

    def _tick(self):
        state, song_id, duration, pos = self.reader.snapshot()
        if song_id != self.song_id:
            self._load_song(song_id, duration)
        if not self.isVisible():
            self._tick_timer.setInterval(200)
            return
        self._update_hover()
        frame = self._compose(state, pos + self.effective_offset())
        if frame != self._frame or self._force_paint:
            self._frame, self._force_paint = frame, False
            self.update()
        # Fast frames only while something animates; otherwise sleep until the next line starts.
        wake = self._wake if state == "playing" else max(self._wake, 0.05)
        self._tick_timer.setInterval(8 if wake <= 0.008 else min(50, math.ceil(wake * 1000)))

    def _update_hover(self):
        now = time.monotonic()
        inside = self.frameGeometry().contains(QCursor.pos())
        if self.cfg["locked"] and self.bubble.isVisible():
            inside = inside or self.bubble.frameGeometry().contains(QCursor.pos())
        if inside:
            self._hover_left_at = 0.0
            if not self.hovered:
                self.hovered = True
                if self.cfg["locked"]:
                    g = self.frameGeometry()
                    self.bubble.move(g.right() - self.bubble.width() - 10, g.top() + 6)
                    self.bubble.show()
                    user32.SetWindowPos(int(self.bubble.winId()), wt.HWND(-1), 0, 0, 0, 0, 0x13)
                else:
                    self.toolbar.show()
        elif self.hovered:
            if not self._hover_left_at:
                self._hover_left_at = now
            elif now - self._hover_left_at > 0.5:
                self.hovered = False
                self.toolbar.hide()
                self.bubble.hide()

    def _compose(self, state, t):
        """Describe what to draw as a hashable tuple; repaint only when it changes."""
        now = time.monotonic()
        wake = 0.05                     # seconds until the picture can next change
        avail = self.width() - 2 * 18
        size1, size2 = self.cfg["font_size"], self._sub_size()
        line1 = line2 = None
        show_panel = self.hovered and not self.cfg["locked"]
        quiet_hint = show_panel  # status text that only appears while hovering

        if state == "unsupported":
            line1 = ("读不到网易云的播放进度", "plain", None, 0.0)
            line2 = ("网易云可能更新了版本，这个工具需要跟着更新", "next")
        elif state in ("no_player", "idle"):
            if quiet_hint:
                line1 = ("网易云没有打开" if state == "no_player" else "网易云没在放歌", "dim", None, 0.0)
        else:
            ly = self.lyrics
            if not ly or not ly.lines or ly.pure_music:
                line1 = (self.title or "…", "plain", None, 0.0)
                note = {"failed": "歌词加载失败，稍后自动重试", "none": "暂无歌词"}.get(self.fetch_state, "")
                if ly and ly.pure_music:
                    note = "纯音乐"
                line2 = (note, "next") if note else None
            else:
                i = ly.index_at(t)
                if i + 1 < len(ly.lines):
                    wake = min(wake, ly.lines[i + 1].start - t)
                nxt = next((ln for ln in ly.lines[max(i + 1, 0):] if ln.text), None)
                if not self.cfg["show_next"]:
                    nxt = None
                cur = ly.lines[i] if i >= 0 else None
                if cur is None:
                    line1 = (self.title or "", "dim", None, 0.0) if self.title else None
                    line2 = (nxt.text, "next") if nxt else None
                elif not cur.text or (ly.word_level and t > cur.end + 4.0):
                    line2 = (nxt.text, "next") if nxt else None
                else:
                    if ly.word_level:
                        shape = self._shape(cur.text, size1, "unsung", cur.words)
                        progress = self._word_progress(shape, cur.words, t)
                        line1 = (cur.text, "karaoke", round(progress, 1),
                                 self._scroll(shape.width, progress, avail))
                        wake = 0.0 if t < cur.end else min(wake, cur.end + 4.0 - t)
                    else:
                        shape = self._shape(cur.text, size1, "sung")
                        frac = (t - cur.start) / max(cur.end - cur.start, 0.1)
                        line1 = (cur.text, "sung", None,
                                 self._scroll(shape.width, frac * shape.width, avail))
                        if shape.width > avail and t < cur.end:
                            wake = 0.0
                    if self.cfg["show_trans"] and cur.trans:
                        line2 = (cur.trans, "sub")
                    elif nxt:
                        line2 = (nxt.text, "next")

        if now < self.toast_until:
            line2 = (self.toast_text, "toast")
            wake = min(wake, self.toast_until - now)
        self._wake = wake
        if line2:
            line2 = (self._elide(line2[0], size2, avail), line2[1])
        return (line1, line2, state == "paused", show_panel, self.cfg["locked"], self.width())

    @staticmethod
    def _scroll(width, progress_px, avail):
        if width <= avail:
            return 0.0
        return round(max(0.0, min(width - avail, progress_px - avail * 0.45)), 1)

    @staticmethod
    def _word_progress(shape, words, t):
        x = 0.0
        for (x0, x1), w in zip(shape.bounds, words):
            if t >= w.end:
                x = x1
            elif t > w.start:
                return x0 + (x1 - x0) * (t - w.start) / max(w.end - w.start, 1e-3)
            else:
                break
        return x

    # -- rendering ---------------------------------------------------------

    _fonts = {}

    def _font(self, size):
        f = self._fonts.get(size)
        if f is None:
            f = self._fonts[size] = QFont(FONT_FAMILY)
            f.setPixelSize(size)
            f.setWeight(QFont.Weight.Bold)
            f.setStyleStrategy(QFont.PreferAntialias)
        return f

    def _elide(self, text, size, avail):
        key = (text, size, avail)
        if key != getattr(self, "_elide_key", None):
            self._elide_key = key
            self._elide_val = QFontMetricsF(self._font(size)).elidedText(text, Qt.ElideRight, avail)
        return self._elide_val

    def _shape(self, text, size, role, words=None):
        dpr = self.devicePixelRatioF()
        key = (text, size, role, self.cfg["theme"], dpr)
        shape = self._shapes.get(key)
        if shape:
            self._shapes.move_to_end(key)
            return shape
        _, top, bottom, unsung = THEMES[self.cfg["theme"]]
        font = self._font(size)
        fm = QFontMetricsF(font)
        stroke = max(2.0, size * 0.075)
        margin = stroke * 2 + 2
        width, height = fm.horizontalAdvance(text), fm.height()

        img = QImage(int((width + 2 * margin) * dpr) + 1, int((height + 2 * margin) * dpr) + 1,
                     QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        path = QPainterPath()
        path.addText(QPointF(margin, margin + fm.ascent()), font, text)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        glow = {"next": 60, "sub": 70}.get(role, 85)
        p.strokePath(path, QPen(QColor(0, 0, 0, glow), stroke * 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.strokePath(path, QPen(QColor(0, 0, 0, 175), stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if role in ("sung", "toast"):
            grad = QLinearGradient(0, margin, 0, margin + height)
            grad.setColorAt(0, QColor(top))
            grad.setColorAt(1, QColor(bottom))
            p.fillPath(path, grad)
        else:
            c = QColor(unsung)
            c.setAlpha({"unsung": 235, "plain": 235, "sub": 225, "next": 150, "dim": 150}.get(role, 235))
            p.fillPath(path, c)
        p.end()

        shape = _Shape()
        shape.image, shape.width, shape.height, shape.margin = img, width, height, margin
        shape.bounds = []
        if words:
            acc = ""
            for w in words:
                x0 = fm.horizontalAdvance(acc)
                acc += w.text
                shape.bounds.append((x0, fm.horizontalAdvance(acc)))
        self._shapes[key] = shape
        while len(self._shapes) > 48:
            self._shapes.popitem(last=False)
        return shape

    def paintEvent(self, e):
        if not self._frame:
            return
        line1, line2, paused, show_panel, locked, _ = self._frame
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if show_panel:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(16, 16, 20, 170))
            p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 14, 14)
        elif not locked:
            p.fillRect(self.rect(), QColor(0, 0, 0, 1))  # keep the window hoverable

        (top1, h1), (top2, h2) = self._rows()
        size1, size2 = self.cfg["font_size"], self._sub_size()
        clip = QRectF(14, 0, w - 28, h)
        if paused:
            p.setOpacity(0.55)
        if line1:
            text, mode, progress, scroll = line1
            if mode == "karaoke":
                base = self._shape(text, size1, "unsung")
                lit = self._shape(text, size1, "sung")
            else:
                base, lit = self._shape(text, size1, mode), None
            x = self._x_for(base, scroll, w)
            y = top1 + (h1 - base.height) / 2 - base.margin
            p.save()
            p.setClipRect(clip)
            p.drawImage(QPointF(x, y), base.image)
            if lit and progress > 0:
                p.setClipRect(QRectF(x, 0, lit.margin + progress, h), Qt.IntersectClip)
                p.drawImage(QPointF(x, y), lit.image)
            p.restore()
        if line2:
            text, role = line2
            shape = self._shape(text, size2, role)
            x = self._x_for(shape, 0.0, w)
            y = top2 + (h2 - shape.height) / 2 - shape.margin
            p.save()
            p.setClipRect(clip)
            if role == "toast":
                p.setOpacity(1.0)
            p.drawImage(QPointF(x, y), shape.image)
            p.restore()
        p.end()

    @staticmethod
    def _x_for(shape, scroll, w):
        if shape.width <= w - 36:
            return (w - shape.width) / 2 - shape.margin
        return 18 - scroll - shape.margin
