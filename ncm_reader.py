"""Read NetEase Cloud Music (PC 3.x) playback state straight from its process memory.

SMTC on this client only reports title/artist with an empty timeline, so the
position has to come from cloudmusic.dll itself. The two byte signatures are
from Kxnrl/NetEase-Cloud-Music-DiscordRPC; both land on RIP-relative operands,
which keeps them working across minor client updates.
"""
import ctypes
import ctypes.wintypes as wt
import re
import struct
import threading
import time
from collections import deque

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.WaitForSingleObject.restype = wt.DWORD
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD,
                                       ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]

INVALID_HANDLE = ctypes.c_void_p(-1).value
WAIT_TIMEOUT = 0x102
PROCESS_ACCESS = 0x0400 | 0x0010 | 0x00100000  # QUERY_INFORMATION | VM_READ | SYNCHRONIZE


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]


def _sig(text):
    return re.compile(b"".join(b"." if tok == "?" else re.escape(bytes([int(tok, 16)]))
                               for tok in text.split()), re.DOTALL)


AUDIO_PLAYER_SIG = _sig(
    "48 8D 0D ? ? ? ? E8 ? ? ? ? 48 8D 0D ? ? ? ? E8 ? ? ? ? 90 48 8D 0D ? ? ? ? E8 ? ? ? ? "
    "48 8D 05 ? ? ? ? 48 8D A5 ? ? ? ? 5F 5D C3 CC CC CC CC CC 48 89 4C 24 ? 55 57 48 81 EC "
    "? ? ? ? 48 8D 6C 24 ? 48 8D 7C 24")
SCHEDULE_SIG = _sig("66 0F 2E 0D ? ? ? ? 7A ? 75 ? 66 0F 2E 15")

# Offsets inside the static AudioPlayer object.
OFF_PLAY_INFO = 0x50   # -> object holding the "<songid>_<suffix>" std::string at +0x10
OFF_STATUS = 0x60      # int32: 0 idle, 1 playing, 2 paused
OFF_DURATION = 0xA8    # double, seconds

STATUS_PLAYING, STATUS_PAUSED = 1, 2


def list_processes(exe_name):
    """[(pid, parent_pid)] for every process whose image name matches."""
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (None, INVALID_HANDLE):
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe_name:
                out.append((entry.th32ProcessID, entry.th32ParentProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


class Process:
    def __init__(self, pid):
        self.pid = pid
        self.handle = k32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) failed")
        self._buf = ctypes.create_string_buffer(0x100)
        self._got = ctypes.c_size_t()

    def close(self):
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None

    def alive(self):
        return bool(self.handle) and k32.WaitForSingleObject(self.handle, 0) == WAIT_TIMEOUT

    def read(self, addr, size):
        buf = self._buf if size <= len(self._buf) else ctypes.create_string_buffer(size)
        if not k32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf, size,
                                     ctypes.byref(self._got)) or self._got.value != size:
            raise OSError(ctypes.get_last_error(), f"read {addr:#x}+{size:#x} failed")
        return buf.raw[:size]

    def module_base(self, name):
        arr = (ctypes.c_void_p * 1024)()
        needed = wt.DWORD()
        if not psapi.EnumProcessModulesEx(self.handle, arr, ctypes.sizeof(arr), ctypes.byref(needed), 0x3):
            return None
        name_buf = ctypes.create_unicode_buffer(260)
        for mod in arr[:needed.value // ctypes.sizeof(ctypes.c_void_p)]:
            if mod and psapi.GetModuleBaseNameW(self.handle, mod, name_buf, 260) \
                    and name_buf.value.lower() == name:
                return mod
        return None


def locate_addresses(proc, base):
    """Return (audio_player_addr, schedule_addr) or None if the signatures are gone."""
    hdr = proc.read(base, 0x1000)
    pe = struct.unpack_from("<I", hdr, 0x3C)[0]
    n_sections = struct.unpack_from("<H", hdr, pe + 6)[0]
    first = pe + 24 + struct.unpack_from("<H", hdr, pe + 20)[0]
    for i in range(n_sections):
        off = first + 40 * i
        vsize, vaddr = struct.unpack_from("<II", hdr, off + 8)
        if not struct.unpack_from("<I", hdr, off + 36)[0] & 0x20000000:  # IMAGE_SCN_MEM_EXECUTE
            continue
        code = proc.read(base + vaddr, vsize)
        m_ap, m_sc = AUDIO_PLAYER_SIG.search(code), SCHEDULE_SIG.search(code)
        if not (m_ap and m_sc):
            continue

        def rip_target(pos, disp_at):
            disp = struct.unpack_from("<i", code, pos + disp_at)[0]
            return base + vaddr + pos + disp_at + 4 + disp

        return rip_target(m_ap.start(), 3), rip_target(m_sc.start(), 4)
    return None


class NcmReader:
    """Polls the player in a background thread and keeps a smooth playback clock.

    The client writes its position roughly every 30 ms in 0.01 s steps. Each fresh
    value gives an anchor guess (perf_counter - position); reads can only be late,
    never early, so the minimum over a short window is the true anchor.
    """

    POLL = 0.010
    WINDOW = 1.0       # seconds of anchor samples kept
    JUMP = 0.25        # anchor change that counts as seek / resume
    STALL = 0.12       # don't extrapolate further than this past the last write

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._addrs = None
        self._unsupported_pid = None
        self.state = "no_player"        # no_player | unsupported | idle | playing | paused
        self.song_id = ""
        self.duration = 0.0
        self._anchor = None
        self._samples = deque()
        self._last_s = None
        self._frozen = 0.0
        self._next_id_read = 0.0
        self._last_info = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ncm-reader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        """(state, song_id, duration, position_seconds)."""
        with self._lock:
            pos = self._frozen
            if self.state == "playing" and self._anchor is not None:
                pos = min(time.perf_counter() - self._anchor, self._last_s + self.STALL)
            return self.state, self.song_id, self.duration, pos

    # -- background thread -------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            if self._proc is None:
                self._attach()
                if self._proc is None:
                    self._stop.wait(1.0)
                    continue
            try:
                self._poll()
            except OSError:
                if not self._proc.alive():
                    self._detach()
                    continue
            self._stop.wait(self.POLL)
        self._detach()

    def _set_state(self, state):
        with self._lock:
            self.state = state
            if state in ("no_player", "unsupported", "idle"):
                self.song_id, self.duration, self._frozen = "", 0.0, 0.0
                self._anchor, self._last_s = None, None
                self._samples.clear()

    def _attach(self):
        procs = list_processes("cloudmusic.exe")
        if not procs:
            self._unsupported_pid = None
            self._set_state("no_player")
            return
        pids = {pid for pid, _ in procs}
        # The audio player lives in the browser (root) process; renderers are its children.
        main = [pid for pid, parent in procs if parent not in pids] or [pid for pid, _ in procs]
        for pid in main:
            if pid == self._unsupported_pid:
                self._set_state("unsupported")
                return
            try:
                proc = Process(pid)
            except OSError:
                continue
            try:
                base = proc.module_base("cloudmusic.dll")
                addrs = locate_addresses(proc, base) if base else None
            except OSError:
                addrs = None
            if base and not addrs:
                proc.close()
                self._unsupported_pid = pid
                self._set_state("unsupported")
                return
            if addrs:
                self._proc, self._addrs = proc, addrs
                self._set_state("idle")
                return
            proc.close()
        self._set_state("no_player")

    def _detach(self):
        if self._proc:
            self._proc.close()
        self._proc, self._addrs = None, None
        self._set_state("no_player")

    def _read_song_id(self, info):
        if not info:
            return ""
        raw = self._proc.read(info + 0x10, 0x20)
        size, cap = struct.unpack_from("<QQ", raw, 0x10)
        if not 0 < size < 256:
            return ""
        data = raw[:size] if cap <= 15 else self._proc.read(struct.unpack_from("<Q", raw, 0)[0], size)
        text = data.decode("utf-8", "replace")
        return text.split("_", 1)[0]

    def _poll(self):
        ap, sp = self._addrs
        t_before = time.perf_counter()
        s = struct.unpack("<d", self._proc.read(sp, 8))[0]
        # Timestamp after the read so a sample can only ever look late, never early;
        # if the thread was preempted mid-read the sample is too vague to anchor on.
        t = time.perf_counter()
        clean = t - t_before < 0.001
        block = self._proc.read(ap + OFF_PLAY_INFO, OFF_DURATION + 8 - OFF_PLAY_INFO)
        info = struct.unpack_from("<Q", block, 0)[0]
        status = struct.unpack_from("<i", block, OFF_STATUS - OFF_PLAY_INFO)[0]
        duration = struct.unpack_from("<d", block, OFF_DURATION - OFF_PLAY_INFO)[0]

        song = self.song_id
        if info != self._last_info or t >= self._next_id_read:
            song = self._read_song_id(info)
            self._last_info, self._next_id_read = info, t + 0.05

        if status not in (STATUS_PLAYING, STATUS_PAUSED) or not song:
            if self.state != "idle":
                self._set_state("idle")
            return

        with self._lock:
            if song != self.song_id:
                self.song_id = song
                self._anchor, self._last_s = None, None
                self._samples.clear()
            self.duration = duration if 0 < duration < 36000 else 0.0
            if status == STATUS_PAUSED or s < 0:
                self.state = "paused"
                self._frozen = max(s, 0.0)
                self._anchor, self._last_s = None, None
                self._samples.clear()
                return
            self.state = "playing"
            if s == self._last_s:
                return
            # Fresh value: first sighting of a write is the least delayed one.
            guess = t - s
            if not clean and self._anchor is not None and abs(guess - self._anchor) <= self.JUMP:
                self._last_s = self._frozen = s
                return
            if self._anchor is None or abs(guess - self._anchor) > self.JUMP:
                self._samples.clear()
            self._samples.append((t, guess))
            while self._samples and t - self._samples[0][0] > self.WINDOW:
                self._samples.popleft()
            self._anchor = min(g for _, g in self._samples)
            self._last_s = s
            self._frozen = s
