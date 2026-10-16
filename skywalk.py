"""
Skywalk — программа нулл-биндов для CS2 KZ (Windows). Автор: blessed.
Установка:  pip install customtkinter pynput pillow pystray
Запуск:     py skywalk.py
"""
import ctypes
import json
import math
import os
import queue
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import struct
import sys
import tempfile
import webbrowser
import time
from collections import deque
import wave
import winsound
from ctypes import wintypes

import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageTk

try:
    from PIL import ImageGrab           # «снимок» части экрана — для плавной смены вкладок
except ImportError:
    ImageGrab = None

try:
    import pystray                     # иконка в трее (если библиотеки нет — программа работает и без неё)
except ImportError:
    pystray = None
from pynput import keyboard, mouse


# ================== Звук щелчка ==================
sound = {
    "on": True,          # звук при переключении вкл/выкл
    "cs2": True,         # щелчок, когда переключаешься в окно CS2 (если программа включена)
    "volume": 35,        # громкость, 0–100
    "pitch": 100,        # тон, 50–200 (%)
    "style": "мягкий",   # мягкий / щелчок / тик
}

# стиль: (скорость затухания, длительность в сек, добавить шум)
STYLES = {
    "мягкий": (140, 0.045, False),
    "щелчок": (320, 0.025, True),
    "тик":    (600, 0.015, False),
}


def make_click(path, freq):
    """Генерирует короткий звук по текущим настройкам и сохраняет в .wav."""
    decay, length, noise = STYLES[sound["style"]]
    rate = 44100
    vol = sound["volume"] / 100
    frames = bytearray()
    seed = 12345
    for i in range(int(rate * length)):
        t = i / rate
        fade = math.exp(-t * decay)
        sample = math.sin(2 * math.pi * freq * t)
        if noise:                                   # немного «шума» — звучит как механический щелчок
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            sample = sample * 0.6 + (seed / 0x7FFFFFFF * 2 - 1) * 0.4
        frames += struct.pack("<h", int(sample * fade * vol * 32767))
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(bytes(frames))


CLICK_ON = os.path.join(tempfile.gettempdir(), "skywalk_click_on.wav")
CLICK_OFF = os.path.join(tempfile.gettempdir(), "skywalk_click_off.wav")


def rebuild_sounds():
    k = sound["pitch"] / 100
    make_click(CLICK_ON, 1400 * k)      # включение — чуть выше
    make_click(CLICK_OFF, 900 * k)      # выключение — чуть ниже


rebuild_sounds()


CHIME = os.path.join(tempfile.gettempdir(), "skywalk_chime.wav")


def make_chime():
    """Звук «карта нашлась»: три глухих «тика» с повышением тона — тик-тик-тик.
    Глухость: низкие ноты + фильтр, который срезает высокие частоты. Громкость и тон — из настроек."""
    rate = 44100
    vol = max(0.25, sound["volume"] / 100) * 1.3
    k = sound["pitch"] / 100
    samples = []
    for freq in (700, 880, 1050):
        for i in range(int(rate * 0.08)):            # каждые 80 мс — следующий тик
            t = i / rate
            env = min(1.0, t / 0.002) * math.exp(-t * 260)     # мягкая атака, быстрое затухание
            samples.append(math.sin(2 * math.pi * freq * k * t) * env)
    out, y = bytearray(), 0.0
    for x in samples:                                # фильтр низких частот: сглаживаем сигнал — звук глуше
        y += (x - y) * 0.18
        out += struct.pack("<h", int(max(-1.0, min(1.0, y * vol)) * 32767))
    with wave.open(CHIME, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(bytes(out))


def play_chime():
    try:
        make_chime()
        winsound.PlaySound(CHIME, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        pass


def play_click(on, force=False):
    if not sound["on"] and not force:
        return
    winsound.PlaySound(CLICK_ON if on else CLICK_OFF,
                       winsound.SND_FILENAME | winsound.SND_ASYNC)


# ================== WinAPI: отправка нажатий ==================
user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
LLKHF_INJECTED = 0x10          # флаг «нажатие сделано программой, а не человеком»
KEYDOWN = (0x100, 0x104)       # WM_KEYDOWN, WM_SYSKEYDOWN
VK_F8 = 0x77


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


INPUT_MOUSE = 0
KEYEVENTF_EXTENDEDKEY = 0x0001
# кнопки мыши: (флаг «нажать», флаг «отпустить», mouseData) — для SendInput
MOUSE_FLAGS = {"MOUSE1": (0x0002, 0x0004, 0), "MOUSE2": (0x0008, 0x0010, 0), "MOUSE3": (0x0020, 0x0040, 0),
               "MOUSE4": (0x0080, 0x0100, 1), "MOUSE5": (0x0080, 0x0100, 2)}


def send_key(scan, up=False):
    """Эмулирует нажатие/отпускание клавиши по скан-коду (так игры видят его как реальное)."""
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=KEYBDINPUT(0, scan, flags, 0, 0)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    name = {0x11: "W", 0x1F: "S", 0x1E: "A", 0x20: "D", 0x39: "пробел", 0x1D: "ctrl"}.get(scan, hex(scan))
    log(f"    → в игру {name} {'↑' if up else '↓'}")


def make_input(key, up):
    """key — что нажимать: ("k", скан-код, расширенная?) — клавиша, ("m", "MOUSE1") — кнопка мыши."""
    if key[0] == "m":
        down_flag, up_flag, data = MOUSE_FLAGS[key[1]]
        return INPUT(INPUT_MOUSE, _INPUT_UNION(mi=MOUSEINPUT(0, 0, data, up_flag if up else down_flag, 0, 0)))
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if key[2] else 0) | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=KEYBDINPUT(0, key[1], flags, 0, 0)))


def send_batch(events):
    """Несколько нажатий ОДНИМ вызовом SendInput: игра получает их в один и тот же момент —
    значит, точно в один тик сервера. events: [(клавиша, отпустить?), ...]."""
    if not events:
        return
    arr = (INPUT * len(events))(*[make_input(k, up) for k, up in events])
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    log("    → в игру " + ", ".join(f"{key_label(k)}{'↑' if up else '↓'}" for k, up in events))


# Пауза между приседом и прыжком у «66»-биндов. Жмём не строго одновременно, а присед на 2 мс раньше:
# так порядок всегда «присед → прыжок» (как в консольном бинде +duck; +jump), а 2 мс — это 1/8 тика
# сервера (15,6 мс), поэтому почти всегда оба нажатия попадают в один тик. Если всё же разъедутся —
# присед окажется на тик раньше прыжка, и это всё равно полноценный crouch jump.
CJ_GAP = 0.002
macro_queue = queue.Queue()            # нажатия биндов ждут тут своей очереди


def macro_worker():
    """Отдельный поток: отправляет нажатия биндов и выдерживает паузы между ними. В самом перехвате
    клавиатуры ждать нельзя — Windows ждёт ответа на КАЖДОЕ нажатие в системе, клавиатура бы тормозила."""
    while True:
        for step in macro_queue.get():
            if isinstance(step, (int, float)):
                time.sleep(step)
            else:
                try:
                    send_batch(step)
                except Exception as e:                    # ошибка не должна «убить» поток биндов
                    log(f"бинд: ошибка отправки {e!r}")


def run_macro(*steps):
    """Поставить в очередь шаги: список нажатий [(клавиша, отпустить?), ...] или паузу в секундах."""
    macro_queue.put(steps)


threading.Thread(target=macro_worker, daemon=True).start()


def key_name(data):
    """Имя клавиши для интерфейса (например 'Space', 'Shift', 'Q')."""
    lparam = data.scanCode << 16
    if data.flags & 0x01:              # «расширенная» клавиша (стрелки, правый Ctrl и т.п.)
        lparam |= 1 << 24
    buf = ctypes.create_unicode_buffer(32)
    user32.GetKeyNameTextW(lparam, buf, 32)
    return buf.value or f"VK {data.vkCode}"


def active_window_title():
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value



VERSION = "demo 8.4"   # пока проект в демо — пишем «demo», без «v»

# ================== Журнал событий (для поиска багов) ==================
LOG = deque(maxlen=500)                 # последние 500 событий


def log(text):
    t = time.time()
    LOG.append(time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}  {text}")


def save_log():
    """Сохраняет журнал на рабочий стол (или рядом с программой) и открывает его."""
    for folder in (os.path.join(os.path.expanduser("~"), "Desktop"),
                   os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop"),
                   os.path.dirname(os.path.abspath(sys.argv[0]))):
        if os.path.isdir(folder):
            path = os.path.join(folder, "skywalk_log.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Skywalk {VERSION}\n" + "\n".join(LOG))
            os.startfile(path)
            return

# ================== Клавиши CS2: какие у человека прыжок, присед, чекпоинт ==================
# Программа жмёт клавиши так, как будто их нажал человек, — значит, должна знать, на какой клавише
# у него в CS2 прыжок и присед. Читаем настройки самой игры:
#   game/csgo/cfg/user_keys_default.vcfg — бинды по умолчанию,
#   Steam/userdata/<аккаунт>/730/local/cfg/cs2_user_keys_0_slot0.vcfg — то, что человек поменял.
# Названия клавиш CS2 → (виртуальный код Windows, скан-код, «расширенная» клавиша)
_LETTER_SCANS = dict(zip("QWERTYUIOP", range(0x10, 0x1A)))
_LETTER_SCANS.update(zip("ASDFGHJKL", range(0x1E, 0x27)))
_LETTER_SCANS.update(zip("ZXCVBNM", range(0x2C, 0x33)))
CS2_KEYS = {ch: (ord(ch), sc, False) for ch, sc in _LETTER_SCANS.items()}
CS2_KEYS.update({str(d): (ord(str(d)), 0x01 + d, False) for d in range(1, 10)})   # цифры 1..9 — скан-коды 0x02..0x0A
CS2_KEYS["0"] = (0x30, 0x0B, False)
CS2_KEYS.update({
    "SPACE": (0x20, 0x39, False), "CTRL": (0xA2, 0x1D, False), "RCTRL": (0xA3, 0x1D, True),
    "SHIFT": (0xA0, 0x2A, False), "RSHIFT": (0xA1, 0x36, False), "ALT": (0xA4, 0x38, False),
    "RALT": (0xA5, 0x38, True), "TAB": (0x09, 0x0F, False), "ENTER": (0x0D, 0x1C, False),
    "CAPSLOCK": (0x14, 0x3A, False), "BACKSPACE": (0x08, 0x0E, False),
    "INS": (0x2D, 0x52, True), "DEL": (0x2E, 0x53, True), "HOME": (0x24, 0x47, True), "END": (0x23, 0x4F, True),
    "PGUP": (0x21, 0x49, True), "PGDN": (0x22, 0x51, True), "UPARROW": (0x26, 0x48, True),
    "DOWNARROW": (0x28, 0x50, True), "LEFTARROW": (0x25, 0x4B, True), "RIGHTARROW": (0x27, 0x4D, True),
    "-": (0xBD, 0x0C, False), "=": (0xBB, 0x0D, False), "[": (0xDB, 0x1A, False), "]": (0xDD, 0x1B, False),
    "\\": (0xDC, 0x2B, False), "SEMICOLON": (0xBA, 0x27, False), "'": (0xDE, 0x28, False),
    ",": (0xBC, 0x33, False), ".": (0xBE, 0x34, False), "/": (0xBF, 0x35, False), "`": (0xC0, 0x29, False),
})
CS2_KEYS.update({f"F{i}": (0x6F + i, 0x3A + i, False) for i in range(1, 11)})
CS2_KEYS.update({"F11": (0x7A, 0x57, False), "F12": (0x7B, 0x58, False)})
VK_TO_CS2 = {vk: name for name, (vk, _, _) in CS2_KEYS.items()}
MOUSE_KEYS = ("MOUSE1", "MOUSE2", "MOUSE3", "MOUSE4", "MOUSE5")
# что ищем в биндах CS2: действие → команды, которые его делают
CS2_ACTIONS = {"jump": ("+jump",), "duck": ("+duck",), "cp": ("cs_cp", "sm_checkpoint"), "tp": ("cs_tp", "sm_gocheck")}


def cs2_spec(name):
    """Название клавиши CS2 → чем её нажимать: ("k", скан-код, расширенная) или ("m", "MOUSE1"). None — не умеем."""
    name = name.upper()
    if name in MOUSE_KEYS:
        return ("m", name)
    if name in CS2_KEYS:
        vk, scan, ext = CS2_KEYS[name]
        return ("k", scan, ext)
    return None


def key_label(key):
    """Короткое имя клавиши для журнала и интерфейса."""
    if key[0] == "m":
        return key[1].lower()
    for name, (vk, scan, ext) in CS2_KEYS.items():
        if scan == key[1] and ext == key[2]:
            return name.lower()
    return hex(key[1])


def read_vcfg(path):
    """Бинды из файла CS2 (.vcfg): {"SPACE": "+jump", ...}. Файла нет / битый — пусто."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*"([^"]+)"\s+"([^"]*)"\s*$', line)
                if m and m.group(1) not in ("config", "bindings"):
                    out[m.group(1).upper()] = m.group(2)
    except OSError:
        pass
    return out


def steam_dir():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
    except OSError:
        return r"C:\Program Files (x86)\Steam"


def cs2_files():
    """(файл биндов по умолчанию, файл биндов самого свежего аккаунта) — или None, если не нашли."""
    steam = steam_dir()
    libs = [steam]
    try:                                             # игра может стоять в другой библиотеке Steam
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as f:
            libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    except OSError:
        pass
    default = None
    for lib in libs:
        cand = os.path.join(lib, "steamapps", "common", "Counter-Strike Global Offensive",
                            "game", "csgo", "cfg", "user_keys_default.vcfg")
        if os.path.exists(cand):
            default = cand
            break
    user, newest = None, 0.0
    userdata = os.path.join(steam, "userdata")
    try:
        for acc in os.listdir(userdata):             # у человека может быть несколько аккаунтов — берём
            cand = os.path.join(userdata, acc, "730", "local", "cfg", "cs2_user_keys_0_slot0.vcfg")
            try:                                     # тот, чьи бинды менялись последними
                t = os.path.getmtime(cand)
            except OSError:
                continue
            if t > newest:
                user, newest = cand, t
    except OSError:
        pass
    return default, user


def read_cs2_binds():
    """Какие клавиши в CS2 делают прыжок, присед, чекпоинт и телепорт: {"jump": ["SPACE", ...], ...}."""
    default, user = cs2_files()
    binds = read_vcfg(default) if default else {}
    if user:
        binds.update(read_vcfg(user))                # то, что человек поменял, важнее стандартного
    found = {a: [] for a in CS2_ACTIONS}
    for key, cmd in binds.items():
        if cs2_spec(key) is None:
            continue
        words = {part.strip().split(" ")[0].lower() for part in cmd.split(";") if part.strip()}
        for action, cmds in CS2_ACTIONS.items():
            if words & set(cmds):
                found[action].append(key)
    for keys in found.values():                      # сначала клавиатура (её удобнее жать), пробел и Ctrl — первыми
        keys.sort(key=lambda k: (k.startswith("MOUSE"), k not in ("SPACE", "CTRL"), k))
    found["files"] = (default, user)
    return found


# что известно о биндах CS2 прямо сейчас (меняем целиком — так безопасно для других потоков)
cs2_binds = {"jump": ["SPACE"], "duck": ["CTRL"], "cp": [], "tp": [], "files": (None, None)}
cs2_binds_stamp = None


def refresh_cs2_binds():
    """Перечитать бинды CS2, если файл настроек поменялся (зовём при запуске и при входе в игру)."""
    global cs2_binds, cs2_binds_stamp
    default, user = cs2_files()
    stamp = tuple(os.path.getmtime(f) if f and os.path.exists(f) else None for f in (default, user)) + (user,)
    if stamp == cs2_binds_stamp:
        return
    cs2_binds_stamp = stamp
    found = read_cs2_binds()
    if not found["jump"]:
        found["jump"] = ["SPACE"]                    # не нашли настроек — стандартные клавиши CS2
    if not found["duck"]:
        found["duck"] = ["CTRL"]
    cs2_binds = found
    log(f"бинды cs2: прыжок {found['jump']}, присед {found['duck']}, cp {found['cp']}, tp {found['tp']} "
        f"(файл: {user or 'не найден'})")


def cs2_key(action):
    """Чем нажимать действие CS2. Для прыжка годится только клавиатура (колесо не «держится»)."""
    for name in cs2_binds[action]:
        spec = cs2_spec(name)
        if spec and (spec[0] == "k" or action == "duck"):
            return spec
    return {"jump": ("k", 0x39, False), "duck": ("k", 0x1D, False)}.get(action)


# ================== Рекорды прыжков (из консоли CS2) ==================
# Сервер Cybershoke пишет в чат (а значит, и в консоль CS2) статистику прыжков:
#   PlayerName jumped 285.8273 units with a Long Jump
#   CYBERSHOKE | LJ: 285.8 | 7 Strafes | 82% Sync | 286.00 Pre | 371.66 Max
#   BA 6% | OL 7% | DA 3% | 2.1 Deviation | 19.1 Width | 55.83 Height
# CS2 сохраняет консоль в файл game/csgo/console.log, если в параметрах запуска есть -condebug.
# Программа дочитывает этот файл и запоминает лучший прыжок каждого типа. Память игры НЕ читаем —
# это просто текстовый файл, который пишет сама игра (безопасно, VAC тут ни при чём).
JUMP_TYPES = (    # (сокращение, название, как тип пишет сервер — строчными и без пробелов)
    ("LJ", "long jump", ("longjump",)),
    ("BH", "bunnyhop", ("bunnyhop", "bhop")),
    ("MBH", "multi bunnyhop", ("multibunnyhop", "multibhop")),
    ("WJ", "weird jump", ("weirdjump",)),
    ("LAJ", "ladder jump", ("ladderjump",)),
    ("LAH", "ladderhop", ("ladderhop",)),
    ("JB", "jumpbug", ("jumpbug",)),
    ("LBH", "lowpre bunnyhop", ("lowprebunnyhop", "lowprebhop")),
    ("LWJ", "lowpre weird jump", ("lowpreweirdjump",)),
)
JUMP_BY_TEXT = {v: abbr for abbr, _, variants in JUMP_TYPES for v in variants}
JUMP_NAMES = {abbr: name for abbr, name, _ in JUMP_TYPES}
SHOW_TYPES = ("LJ", "BH", "MBH", "WJ", "LAJ", "LAH", "JB")     # строки на вкладке всегда (остальные — если есть)
JUMP_RE = re.compile(r"(?P<name>.+?) jumped (?P<dist>\d+(?:\.\d+)?) units with an? "
                     r"(?P<type>[A-Za-z][A-Za-z ]*?)\s*(?:[\[(!.,|]|$)")
STATS_RE = re.compile(r"\|\s*(?P<abbr>[A-Z]{2,4}):\s*(?P<dist>\d+(?:\.\d+)?)\s*\|")
NUM = r"(\d+(?:\.\d+)?)"
STAT_FIELDS = (("strafes", r"(\d+)\s*Strafes", int), ("sync", NUM + r"%\s*(?:Avg)?Sync", float),
               ("pre", NUM + r"\s*Pre\b", float), ("max", NUM + r"\s*Max\b", float),
               ("block", NUM + r"\s*Block", float), ("edge", NUM + r"\s*(?:Jump)?Edge\b", float),
               ("height", NUM + r"\s*Height", float),
               ("bad", NUM + r"%\s*AvgBadAngles", float), ("ovl", NUM + r"%\s*AvgOverlap", float),
               ("dead", NUM + r"%\s*AvgDeadAir", float), ("eff", NUM + r"%\s*AvgGainEff", float),
               ("airpath", NUM + r"\s*Airpath", float), ("dev", r"(-?\d+(?:\.\d+)?)\s*Deviation", float),
               ("width", NUM + r"°?\s*AvgWidth", float))


# Пороги тиров — из исходников CS2KZ (src/kz/mode/kz_mode_ckz.h и kz_mode_vnl.h), по ним сервер красит прыжки:
# дистанция ≥ порога — тир не ниже этого. Порядок: meh, impressive, perfect, godlike, ownage, wrecker.
TIER_NAMES = ("meh", "impressive", "perfect", "godlike", "ownage", "wrecker")
TIER_SHORT = ("meh", "imp", "perf", "god", "own", "wreck")
DIST_TIERS = {
    "CKZ": {"LJ": (217, 265, 270, 275, 280, 284), "BH": (217, 275, 280, 287, 292, 295),
            "MBH": (217, 275, 280, 287, 292, 295), "WJ": (217, 275, 280, 287, 292, 295),
            "LAJ": (120, 160, 170, 180, 190, 200), "LAH": (217, 260, 265, 270, 275, 278),
            "JB": (217, 275, 280, 287, 292, 295)},
    "VNL": {"LJ": (215, 230, 235, 240, 245, 248), "BH": (150, 230, 233, 238, 240, 242),
            "MBH": (150, 232, 237, 242, 245, 248), "WJ": (150, 230, 235, 240, 244, 246),
            "LAJ": (50, 80, 90, 100, 105, 108), "LAH": (215, 250, 253, 258, 261, 263),
            "JB": (215, 255, 260, 265, 270, 272)},
}


def jump_tier(abbr, dist, mode="CKZ"):
    """Тир прыжка: 0 — ниже meh (или у типа нет тиров, как у lowpre), 1 — meh … 6 — wrecker."""
    limits = DIST_TIERS.get(mode, DIST_TIERS["CKZ"]).get(abbr)
    if not limits or dist > 500:
        return 0
    return sum(1 for lim in limits if dist >= lim)


TIME_RE = re.compile(r"^(\d{1,2})/(\d{1,2}) (\d{1,2}):(\d{2}):\d{2}")


def clean_line(line):
    """Убираем служебные символы (коды цвета чата и т.п.) и время, которое CS2 пишет в начале строки."""
    line = re.sub(r"[\x00-\x1f\x7f]", "", line).strip()
    return re.sub(r"^\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2}\s+", "", line)   # «10/12 16:29:08 »


def parse_jump_line(line, nick):
    """«<ник> jumped 285.8273 units with a Long Jump» → ("LJ", 285.8273), если это прыжок ИМЕННО этого ника.
    Чужие прыжки и сообщения игроков в чате («Вася: <ник> jumped 400…») не подходят."""
    m = JUMP_RE.search(line)
    if not m or not nick:
        return None
    name = m.group("name").strip()
    if ": " in name:                                  # «кто-то: текст» — это чат игрока, а не сервер
        return None
    if not name.lower().endswith(nick.lower()):
        return None
    before = name[:len(name) - len(nick)]
    if before and before[-1] not in " ]":             # ник целиком, а не кусок чужого («xPlayerName»)
        return None
    abbr = JUMP_BY_TEXT.get(m.group("type").lower().replace(" ", ""))
    if abbr is None:
        return None
    return abbr, float(m.group("dist"))


def parse_stats_line(line):
    """Строка подробностей после прыжка → {"abbr": "LJ", "dist": 285.8, "strafes": 7, ...} или None."""
    if "|" not in line:
        return None
    m = STATS_RE.search(line + " |")
    out = {"abbr": m.group("abbr"), "dist": float(m.group("dist"))} if m else {}
    for key, rx, kind in STAT_FIELDS:
        f = re.search(rx, line)
        if f:
            out[key] = kind(f.group(1))
    # все поля строки как есть — для подробной карточки прыжка. Без цифр (метка режима «CKZ»,
    # «✓ W») не нужны; «LJ: 285.8» — это дистанция, она и так видна
    tokens = [t.strip() for t in line.split("|")]
    tokens = [t for t in tokens if any(ch.isdigit() for ch in t) and not STATS_RE.search("| " + t + " |")]
    if tokens:
        out["tokens"] = tokens
    return out or None


# ================== Тренер: разбор прыжка ==================
# Под каждым прыжком сервер пишет в консоль таблицу по стрейфам и графики клавиш и мыши по тикам:
#   #.  Sync     Gain    Loss   Max     Airtime  BadAngles  Overlap  DeadAir  Width   AvgGain  GainEff  AngRatio(Avg/Med/Max)
#   1.  100.00%  +14.28  -0.00  288.68  10.06%   0.00%      0.00%    0.00%    46.35°  1.43     89.52%   -0.02 | -0.00 |  0.20
#   LEFT KEY    | LLLLL .....L LLLLL ...      (L — A зажата, «.» — нет; один символ = один тик)
#   LEFT MOUSE  | MMMM. .....M MMMM. ...      (M — мышь едет влево)
# Что значат поля (из исходников CS2KZ, src/kz/jumpstats/kz_jumpstats.cpp):
#   BadAngles — клавиша зажата, а скорость не растёт (мышь стоит / повёрнута не туда);
#   Overlap — зажаты обе (A и D); DeadAir — не зажато ничего;
#   GainEff — доля от максимально возможного прироста скорости;
#   AngRatio — угол мыши относительно идеального: минус — ведёшь слишком МЕДЛЕННО, плюс — слишком БЫСТРО.
STRAFE_RE = re.compile(r"^(\d+)\.\s+([\d.]+)%\s+([+-]?[\d.]+)\s+([+-]?[\d.]+)\s+([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s+"
                       r"([\d.]+)%\s+([\d.]+)%\s+([\d.]+)°?\s+([\d.]+)\s+([\d.]+)%\s*(.*)$")
GRAPH_RE = re.compile(r"^(LEFT KEY|RIGHT KEY|LEFT MOUSE|RIGHT MOUSE)\s*\|\s?(.*)$")
TICK_MS = 15.625                                   # один тик сервера (64 тика в секунду)


def parse_strafe_row(line):
    """Строка таблицы стрейфов → {"n": 1, "sync": 100.0, "gain": 14.28, ...} или None."""
    m = STRAFE_RE.match(line)
    if not m:
        return None
    g = m.groups()
    ar = re.findall(r"-?\d+(?:\.\d+)?", g[12])
    return {"n": int(g[0]), "sync": float(g[1]), "gain": float(g[2]), "loss": float(g[3]), "max": float(g[4]),
            "air": float(g[5]), "bad": float(g[6]), "ovl": float(g[7]), "dead": float(g[8]), "width": float(g[9]),
            "avg_gain": float(g[10]), "eff": float(g[11]), "ar": float(ar[0]) if len(ar) == 3 else None}


def parse_graph_line(line):
    """«LEFT KEY | LLLLL ....» → ("LEFT KEY", "LLLLL....") — пробелы между группами убираем, остаются тики."""
    m = GRAPH_RE.match(line)
    return (m.group(1), m.group(2).replace(" ", "")) if m else None


def graph_timing(graph):
    """Когда жмёшь A/D относительно поворота мыши. Ответ: (сдвиг нажатия, сдвиг отпускания) в тиках:
    плюс — клавиша ПОЗЖЕ мыши, минус — РАНЬШЕ. None — графиков нет."""
    if len(graph) < 4:
        return None
    lk, rk, lm, rm = (graph[k] for k in ("LEFT KEY", "RIGHT KEY", "LEFT MOUSE", "RIGHT MOUSE"))
    n = min(len(lk), len(rk), len(lm), len(rm))
    if n < 6:
        return None
    key = ["O" if lk[t] == "L" and rk[t] == "R" else "L" if lk[t] == "L" else "R" if rk[t] == "R" else "." for t in range(n)]
    mouse = ["L" if lm[t] == "M" else "R" if rm[t] == "M" else "." for t in range(n)]

    def runs(seq):
        out, start = [], None
        for t in range(n + 1):
            cur = seq[t] if t < n else None
            if start is not None and cur != seq[start]:
                if seq[start] in "LR":
                    out.append((seq[start], start, t - 1))
                start = None
            if start is None and cur in ("L", "R"):
                start = t
        return out

    k_runs, presses, releases = runs(key), [], []
    for d, s, e in runs(mouse)[1:]:                       # первый поворот — это ещё престрейф, его не считаем
        near = [(abs(ks - s), ks, ke) for kd, ks, ke in k_runs if kd == d and abs(ks - s) <= 4]
        if near:
            _, ks, ke = min(near)
            presses.append(ks - s)
            releases.append(ke - e)
    if len(presses) < 2:
        return None
    return sum(presses) / len(presses), sum(releases) / len(releases)


def jump_target(abbr, mode="CKZ"):
    """Цель — порог wrecker для этого типа (для LJ в CKZ это 284)."""
    limits = DIST_TIERS.get(mode, DIST_TIERS["CKZ"]).get(abbr)
    return limits[-1] if limits else None


def next_tier(abbr, dist, mode="CKZ"):
    """(название следующего тира, сколько осталось) или None, если уже wrecker / тиров нет."""
    limits = DIST_TIERS.get(mode, DIST_TIERS["CKZ"]).get(abbr)
    if not limits:
        return None
    for name, lim in zip(TIER_NAMES, limits):
        if dist < lim:
            return name, lim - dist
    return None


def analyze_jump(j):
    """Советы по прыжку: [(важность, ключ, текст)], самые важные — первыми.
    j = {"abbr", "dist", "stats": {...}, "tokens": [...], "strafes": [...], "graph": {...}}."""
    abbr, st = j["abbr"], j.get("stats", {})
    real = [s for s in j.get("strafes", []) if s["air"] >= 3]   # последний «стрейф» в 1 тик — это уже приземление
    tips = []

    def add(weight, key, **kw):
        tips.append((weight, key, tr("tip_" + key, **kw)))

    def nums(lst):
        return ", ".join(str(s["n"]) for s in lst[:4])

    # W: «+3.00 W» — W держал ещё 3 тика после прыжка (✓ W — отпустил вовремя)
    for tok in j.get("tokens", []):
        w = re.fullmatch(r"\+(\d+(?:\.\d+)?) W", tok.strip())
        if w and abbr in ("LJ", "WJ", "BH", "MBH", "JB", "LAH"):
            add(3 + float(w.group(1)), "w_late", t=f"{float(w.group(1)):g}")
    pre = st.get("pre")
    if abbr == "LJ" and pre and pre < 274:
        add(2 + (276 - pre) / 2, "pre_low", pre=f"{pre:.1f}")
    if len(real) >= 3:
        rest = sorted(s["gain"] for s in real[1:])
        med = rest[len(rest) // 2]
        if med > 0 and real[0]["gain"] < 0.6 * med:
            add(4 + (1 - real[0]["gain"] / med) * 3, "first_gain", g=f"{real[0]['gain']:+.1f}", m=f"{med:+.1f}")
    low_sync = [s for s in real if s["sync"] < 80]
    if st.get("sync", 100) < 86 or low_sync:
        worst = sorted(low_sync, key=lambda s: s["sync"])[:4]          # 4 худших — по порядку номеров
        where = tr("tip_where", n=nums(sorted(worst, key=lambda s: s["n"])),
                   v=f"{worst[0]['sync']:.0f}") if worst else ""
        add(2 + (88 - min(st.get("sync", 88), 88)) / 3 + len(low_sync) * 0.5, "sync", s=f"{st.get('sync', 0):.0f}", where=where)
    bad = [s for s in real if s["bad"] >= 20]
    if st.get("bad", 0) >= 12 or bad:
        add(2 + st.get("bad", 0) / 5, "bad", b=f"{st.get('bad', 0):.0f}",
            where=tr("tip_where_n", n=nums(bad)) if bad else "")
    ovl = [s for s in real if s["ovl"] >= 5]
    if st.get("ovl", 0) >= 0.5 or ovl:
        add(3 + st.get("ovl", 0), "overlap", o=f"{st.get('ovl', 0):.1f}", where=tr("tip_where_n", n=nums(ovl)) if ovl else "")
    dead = [s for s in real if s["dead"] >= 8]
    if dead:
        add(2.5 + len(dead), "dead", where=tr("tip_where_n", n=nums(dead)))
    ars = [s["ar"] for s in real if s.get("ar") is not None]
    if len(ars) >= 3:
        mean = sum(ars) / len(ars)
        if mean <= -0.15:
            add(2 + abs(mean) * 6, "mouse_slow", r=f"{mean:+.2f}")
        elif mean >= 0.15:
            add(2 + mean * 6, "mouse_fast", r=f"{mean:+.2f}")
    timing = graph_timing(j.get("graph", {}))
    if timing:
        press, release = timing
        if press >= 1.0:
            add(2 + press, "key_late", t=f"{press:.1f}", ms=f"{press * TICK_MS:.0f}")
        elif press <= -1.0:
            add(2 + abs(press), "key_early", t=f"{abs(press):.1f}", ms=f"{abs(press) * TICK_MS:.0f}")
        if release <= -1.0:
            add(2 + abs(release), "release_early", t=f"{abs(release):.1f}")
    if st.get("airpath", 1) > 1.04 or abs(st.get("dev", 0)) > 20:
        add(1.5 + (st.get("airpath", 1) - 1) * 20, "path", a=f"{st.get('airpath', 1):.3f}", d=f"{st.get('dev', 0):.0f}")
    n_str = st.get("strafes")
    if abbr == "LJ" and n_str and (n_str < 7 or n_str > 13):
        add(1.5, "strafes_few" if n_str < 7 else "strafes_many", n=n_str)
    if st.get("eff") is not None and st["eff"] < 62 and not tips:
        add(1.5, "eff", e=f"{st['eff']:.0f}")
    tips.sort(key=lambda t: -t[0])
    return tips


def steam_nick():
    """Ник из Steam (CS2 берёт его же): аккаунт, в который входили последним."""
    try:
        with open(os.path.join(steam_dir(), "config", "loginusers.vdf"), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    best = ""
    for block in re.findall(r'"\d{17}"\s*\{([^}]*)\}', text):
        name = re.search(r'"PersonaName"\s+"([^"]*)"', block)
        if name and (not best or re.search(r'"MostRecent"\s+"1"', block)):
            best = name.group(1)
    return best


def current_nick():
    """Чьи прыжки считать: ник Steam-аккаунта, в котором сейчас человек (CS2 берёт ник оттуда же)."""
    return record_watcher.auto_nick


def stamp_epoch(stamp):
    """("2026-10-12", "17:07") из строки консоли → время в секундах (когда прыжок был на самом деле)."""
    try:
        return time.mktime(time.strptime(f"{stamp[0]} {stamp[1]}", "%Y-%m-%d %H:%M"))
    except (ValueError, TypeError, IndexError):
        return time.time()


class RecordWatcher:
    """Дочитывает console.log CS2 (раз в полсекунды) и обновляет рекорды в prefs["records"]."""

    def __init__(self):
        self.pos = 0                  # докуда файл уже прочитан
        self.buf = ""                 # недописанная последняя строка
        self.last = None              # последний свой прыжок: (тип, дистанция, запись-рекорд или None)
        self.new = []                 # новые рекорды — окно заберёт и покажет
        self.found = False            # файл консоли есть
        self.path = None
        self.next_path = 0.0
        self.auto_nick = steam_nick()
        self.unknown = set()          # непонятные типы прыжков — пишем в журнал по разу
        self.fresh = False            # следующая строка подробностей — первая для этого прыжка
        # --- сессия = текущий запуск CS2 (при каждом запуске CS2 начинает console.log заново) ---
        self.session = {}             # {"LJ": {"count": 12, "tiers": [7 чисел], "best": 278.3, "sum": …}}
        self.session_since = ""       # время первого прыжка сессии «16:25»
        self.run_jumps = 0            # своих прыжков с начала файла (для «новой сессии» после перезапуска программы)
        self.skip = 0                 # сколько первых прыжков файла не считать (нажали «новая сессия»)
        self.head = b""               # начало файла — по нему узнаём, тот же ли это запуск CS2
        self.mode = "CKZ"             # режим KZ (из строки подробностей) — от него зависят тиры
        self.last_session = None      # последний посчитанный прыжок: (тип, дистанция, тир) — поправить тир по режиму
        self.map = ""                 # карта, на которой сейчас человек («[Client] Map: "kz_…"» в консоли)
        self.jumps = []               # полные данные прыжков сессии (для тренера): см. feed
        self.cur = None               # прыжок, чьи строки (стрейфы, графики) ещё дочитываются
        self.version = 0              # растёт с каждым своим прыжком — окно понимает, что пора обновить тренер
        self.pending = deque()        # прочитанные, но ещё не разобранные строки (см. poll)
        self.behind = False           # файл дочитан не до конца

    def console_path(self):
        if self.path is None or time.time() >= self.next_path:
            self.next_path = time.time() + 30
            self.auto_nick = steam_nick()         # сменил аккаунт / ник в Steam — подхватываем
            default = cs2_files()[0]
            self.path = os.path.join(os.path.dirname(os.path.dirname(default)), "console.log") if default else None
        return self.path

    def poll(self, budget=None):
        """Дочитать консоль. budget — сколько секунд можно потратить (из окна: ~8 мс за кадр).
        При запуске программы файл бывает 4 МБ — разбор целиком занимал ~1,5 с, и окно «замирало».
        Теперь строки ждут в self.pending и разбираются кусками, пока не кончится время кадра;
        self.behind = True — ещё не всё прочитано (окно позовёт снова уже в следующем кадре)."""
        deadline = None if budget is None else time.perf_counter() + budget
        if self.pending:
            self.drain(deadline)
            if self.pending:
                self.behind = True
                return
        path = self.console_path()
        try:
            size = os.path.getsize(path) if path else -1
        except OSError:
            size = -1
        self.found = size >= 0
        if size < 0:
            return
        if size < self.pos:                       # CS2 перезапустили — файл начат заново (и сессия тоже)
            self.pos, self.buf, self.last = 0, "", None
            self.pending.clear()
        if size == self.pos:
            return
        if self.pos == 0:                         # читаем файл с начала — новая сессия
            self.start_session(path)
            if size > 4_000_000:                  # огромный старый файл: хватит последних 4 МБ
                self.pos = size - 4_000_000
                self.skip = 0                     # начало не читали — прыжки до «новой сессии» не сосчитать
        try:
            with open(path, "rb") as f:
                f.seek(self.pos)
                data = f.read(min(size - self.pos, 8_000_000))
        except OSError:
            return
        self.pos += len(data)
        lines = (self.buf + data.decode("utf-8", "replace")).split("\n")
        self.buf = lines.pop()                    # последняя строка может быть ещё не дописана
        self.pending = deque(lines)
        self.drain(deadline)
        self.behind = bool(self.pending)

    def drain(self, deadline):
        """Разобрать строки из очереди: все (deadline=None) или пока не кончилось время."""
        pending, n = self.pending, 0
        while pending:
            n += 1
            if deadline is not None and n % 64 == 0 and time.perf_counter() > deadline:
                return                             # время кадра вышло — остальное в следующий раз
            line = pending.popleft()
            t = TIME_RE.match(line.strip())
            if t:                                  # «10/12 17:07:11» → ("2026-10-12", "17:07")
                now = time.localtime()
                mon, day = int(t.group(1)), int(t.group(2))
                year = now.tm_year - (1 if (mon, day) > (now.tm_mon, now.tm_mday) else 0)
                stamp = (f"{year}-{mon:02d}-{day:02d}", f"{int(t.group(3)):02d}:{t.group(4)}")
            else:
                stamp = (time.strftime("%Y-%m-%d"), "")
            self.feed(clean_line(line), stamp)

    def start_session(self, path):
        """Файл консоли читается с начала (запуск программы или новый запуск CS2) — сессия заново.
        Если в этом же запуске CS2 уже нажимали «новая сессия», первые прыжки пропускаем."""
        self.session, self.session_since, self.run_jumps, self.last_session = {}, "", 0, None
        self.jumps, self.cur = [], None
        try:
            with open(path, "rb") as f:
                self.head = f.read(256)
        except OSError:
            self.head = b""
        mark = prefs.get("session_mark") or {}
        self.skip = mark.get("skip", 0) if mark.get("head") == self.head.hex() else 0

    def new_session(self):
        """Кнопка «новая сессия»: всё, что было до этого момента, не считаем."""
        self.skip = self.run_jumps
        self.session, self.session_since, self.last_session = {}, "", None
        self.jumps = []
        prefs["session_mark"] = {"head": self.head.hex(), "skip": self.run_jumps}

    def count_jump(self, abbr, dist, stamp):
        """Прыжок — в статистику сессии."""
        self.run_jumps += 1
        if self.run_jumps <= self.skip:
            return False
        s = self.session.setdefault(abbr, {"count": 0, "tiers": [0] * 7, "best": 0.0, "sum": 0.0})
        tier = jump_tier(abbr, dist, self.mode)
        s["count"] += 1
        s["tiers"][tier] += 1
        s["best"] = max(s["best"], dist)
        s["sum"] += dist
        self.last_session = (abbr, dist, tier)
        if not self.session_since:
            self.session_since = stamp
        return True

    def finish_jump(self):
        """Строки прыжка дочитаны (или начался следующий) — сохраняем стрейфы и графики у рекорда."""
        cur, self.cur = self.cur, None
        if cur and cur.get("rec") is not None and cur["strafes"]:
            cur["rec"]["strafe_rows"] = cur["strafes"]
            cur["rec"]["graph"] = cur["graph"]

    def feed(self, line, stamp=("", "")):
        if not line:
            return
        if isinstance(stamp, str):                # старый вызов: только время
            stamp = (time.strftime("%Y-%m-%d"), stamp)
        m = re.match(r'^\[Client\] Map: "([^"]+)"', line)
        if m:
            self.map = map_core(m.group(1))
            return
        if " jumped " in line and " units with " in line:
            self.finish_jump()                    # начался любой прыжок (и чужой) — прошлый закрыт
        nick = current_nick()
        jump = parse_jump_line(line, nick)
        if jump:
            abbr, dist = jump
            counted = self.count_jump(abbr, dist, stamp[1])
            self.cur = {"abbr": abbr, "dist": dist, "time": stamp[1], "map": self.map, "stats": {}, "tokens": [],
                        "strafes": [], "graph": {}, "rec": None}
            if counted:
                self.jumps.append(self.cur)
                del self.jumps[:-3000]            # длинная сессия — храним последние 3000
            self.version += 1
            # история: лучший прыжок каждого типа по дням (для графика прогресса)
            hist = prefs["history"].setdefault(abbr, {})
            if dist > hist.get(stamp[0], 0):
                hist[stamp[0]] = round(dist, 4)
            rec = prefs["records"].get(abbr)
            if rec is None or dist > rec["dist"] + 1e-6:
                prev = rec["dist"] if rec else None
                rec = {"dist": round(dist, 4), "time": stamp_epoch(stamp), "map": self.map}
                prefs["records"] = dict(prefs["records"], **{abbr: rec})
                self.cur["rec"] = rec
                self.new.append((abbr, dist, prev))
                log(f"рекорд {abbr}: {dist:.4f}")
                self.last = (abbr, dist, rec)
                self.fresh = True                 # первая строка подробностей заменит старые
            elif abs(dist - rec["dist"]) < 1e-3:
                self.last = (abbr, dist, rec)     # это и есть рекордный прыжок (файл читается заново) — подробности дописать
                self.fresh = True
                self.cur["rec"] = rec
                if self.map and not rec.get("map"):
                    rec["map"] = self.map
                if stamp[1]:                      # настоящее время прыжка — из строки консоли
                    rec["time"] = stamp_epoch(stamp)
            else:
                self.last = (abbr, dist, None)    # прыжок не лучше — подробности не нужны
            return
        if " jumped " in line and " units with " in line and nick and nick.lower() in line.lower():
            kind = line.split(" units with ", 1)[1][:40]
            if kind not in self.unknown:          # похоже на свой прыжок, но не разобрали — в журнал
                self.unknown.add(kind)
                log(f"рекорды: непонятная строка прыжка: {line[:160]}")
        if self.cur is not None:
            row = parse_strafe_row(line)
            if row:
                self.cur["strafes"].append(row)
                return
            graph = parse_graph_line(line)
            if graph:
                self.cur["graph"][graph[0]] = graph[1]
                if graph[0] == "RIGHT MOUSE":     # последний график — прыжок дочитан
                    self.finish_jump()
                return
            if "|" in line and not line.startswith("#."):
                stats = parse_stats_line(line)
                if stats:
                    self.cur["stats"].update({k: v for k, v in stats.items() if k not in ("abbr", "dist", "tokens")})
                    self.cur["tokens"] += stats.get("tokens", [])
        if self.last is None:
            return
        stats = parse_stats_line(line)
        if stats is None:
            return
        mode = line.split("|", 1)[0].strip()
        if mode in DIST_TIERS and mode != self.mode:   # режим KZ сменился — тир последнего прыжка пересчитать
            self.mode = mode
            if self.last_session:
                abbr0, dist0, tier0 = self.last_session
                tier1 = jump_tier(abbr0, dist0, mode)
                s = self.session.get(abbr0)
                if s and tier1 != tier0:
                    s["tiers"][tier0] -= 1
                    s["tiers"][tier1] += 1
                    self.last_session = (abbr0, dist0, tier1)
        abbr, dist, rec = self.last
        if "abbr" in stats and (stats["abbr"] != abbr or abs(stats["dist"] - dist) > 0.2):
            self.last = None                      # подробности чужого прыжка
            return
        if rec is not None:
            rec.update({k: v for k, v in stats.items() if k not in ("abbr", "dist", "tokens")})
            tokens = stats.get("tokens", [])
            rec["details"] = tokens if self.fresh else rec.get("details", []) + tokens
            self.fresh = False
        if "height" in stats:                     # строка с высотой — последняя строка подробностей
            self.last = None


record_watcher = RecordWatcher()


# ================== Логика нуллов ==================
VK_TO_KEY = {0x41: "a", 0x44: "d", 0x57: "w", 0x53: "s"}
SCAN = {"a": 0x1E, "d": 0x20, "w": 0x11, "s": 0x1F}
OPPOSITE = {"a": "d", "d": "a", "w": "s", "s": "w"}
PAIR = {"a": "ad", "d": "ad", "w": "ws", "s": "ws"}

state = {
    "enabled": True,
    "pairs": {"ad": True, "ws": True},
    "only_cs2": True,
    "held": {k: False for k in SCAN},   # что человек реально держит
    # --- клавиша вкл/выкл ---
    "toggle_vk": VK_F8,
    "toggle_name": "F8",
    "toggle_down": False,
    # --- бинды. Каждый: None / ("key", код) / ("wheel", "up"|"down") / ("mouse", "x1"|"x2"|"middle") ---
    "unw": None, "unw_name": "",   # -w: отпустить вперёд
    "lj": None, "lj_name": "",     # 66 с -w: присед + прыжок + отпустить W
    "cj": None, "cj_name": "",     # 66 без -w: присед + прыжок
    "jb": None, "jb_name": "",     # jumpbug: отпустить присед + прыжок в один тик
    "dv": None, "dv_name": "",     # danvari: прыжок + чекпоинт + телепорт в один тик
    # клавиши ЧЕКПОИНТА и ТЕЛЕПОРТА в самой CS2 (на них должны быть cs_cp и cs_tp) — для danvari.
    # None — берём из настроек CS2, если там нашлись. Иначе (название, ("k", скан-код, расширенная)).
    "dv_cp": None, "dv_tp": None,
    "macro_down": set(),   # какие бинды сейчас зажаты (чтобы не повторять при автоповторе и отпустить при F8)
    "keys_down": set(),    # какие клавиши человек держит прямо сейчас (виртуальные коды)
    "mouse_down": set(),   # какие кнопки мыши человек держит прямо сейчас ("MOUSE1", ...)
    "w_blocked": False,    # W отпущена биндом — игнорируем её, пока не нажмёшь W заново
    "phys": {k: False for k in SCAN},   # что зажато пальцем прямо сейчас (по событиям клавиатуры)
    # --- назначение клавиш ---
    "capturing": None,     # None / название бинда / "toggle" / "dv_cp" / "dv_tp" — что сейчас назначаем
}


def game_ok():
    """Программа включена и (если надо) открыто окно CS2."""
    if not state["enabled"]:
        return False
    if state["only_cs2"] and "Counter-Strike 2" not in active_window_title():
        return False
    return True


def is_active(key):
    return game_ok() and state["pairs"][PAIR[key]]


def release_w():
    """Бинд -w: отпускаем W, даже если физически она ещё зажата."""
    log(f"-w сработал: W зажата={state['phys']['w']} held={state['held']['w']} "
        f"окно='{active_window_title()}'")
    if not state["phys"]["w"]:         # W не зажата — отпускать нечего, ничего не блокируем
        return
    if state["held"]["w"]:
        on_up("w")                     # отпустит W и вернёт S, если она зажата
    else:
        send_key(SCAN["w"], up=True)
    state["w_blocked"] = True


# ================== Бинды KZ ==================
# Что делает каждый бинд (как консольные бинды KZ, только их жмёт программа, а не консоль):
#   lj  — «66 с -w»:   +duck; +jump; -forward     (crouch jump + отпустить W — для лонгджампов)
#   cj  — «66 без -w»: +duck; +jump               (crouch jump: присед в воздухе даёт +9 к высоте → 57+9=66)
#   jb  — jumpbug:     -duck; +jump в один тик    (держишь присед, у земли жмёшь бинд)
#   dv  — danvari:     +jump; -jump; cs_cp; cs_tp (всё в один тик — сдвиг на лестнице)
MACRO_BINDS = ("lj", "cj", "jb", "dv")
ALL_BINDS = ("unw",) + MACRO_BINDS
WHEEL_OK = ALL_BINDS                    # на колесо можно всё: у 66-биндов присед держит сама программа
# У колеса нет «отпускания» — только щелчок. Поэтому 66-бинд на колесе сам держит присед всё время
# полёта: прыжок высотой 57 длится ≈0,75 с (t = 2·√(2h/g), g = 800), с приседом чуть дольше → 0,8 с.
# Прыжок отпускаем почти сразу (через ~3 тика) — он нужен только в момент отрыва.
WHEEL_DUCK_HOLD = 0.8
WHEEL_JUMP_HOLD = 0.05
wheel_gen = {}                          # номер последнего щелчка колеса по каждому бинду (старые таймеры не мешают новым)
CAPTURE_MOUSE = ALL_BINDS               # что можно назначить на кнопку мыши


def bind_of(trigger):
    """Какой бинд висит на этой клавише / кнопке мыши / колесе (или None)."""
    for b in ALL_BINDS:
        if state[b] == trigger:
            return b
    return None


def held_duck_keys():
    """Клавиши приседа CS2, которые человек держит прямо сейчас (для jumpbug — их надо отпустить)."""
    out = []
    for name in cs2_binds["duck"]:
        spec = cs2_spec(name)
        if spec is None:
            continue
        if spec[0] == "m":
            if name in state["mouse_down"]:
                out.append(spec)
        elif CS2_KEYS[name][0] in state["keys_down"]:
            out.append(spec)
    return out


def dv_key(which):
    """Клавиша чекпоинта / телепорта в CS2 для danvari: своя (назначена в программе) или из настроек CS2."""
    own = state["dv_" + which]
    if own:
        return own[1]
    for name in cs2_binds[which]:
        spec = cs2_spec(name)
        if spec and spec[0] == "k":
            return spec
    return None


def dv_key_name(which):
    own = state["dv_" + which]
    if own:
        return own[0]
    spec = dv_key(which)
    return key_label(spec) if spec else ""


def bind_event(b, down):
    """Нажали (down=True) или отпустили бинд b. Зовётся из перехвата — тут нельзя ждать,
    поэтому нажатия с паузами уходят в очередь, их отправит поток macro_worker."""
    if down:
        if b in state["macro_down"]:              # автоповтор при удержании — ничего не делаем
            return
        state["macro_down"].add(b)
    else:
        if b not in state["macro_down"]:
            return
        state["macro_down"].discard(b)
    jump, duck = cs2_key("jump"), cs2_key("duck")
    if b in ("lj", "cj"):
        if down:
            if b == "lj":
                release_w()                          # 66 с -w: W отпускается и не вернётся, пока не нажмёшь её заново
            run_macro([(duck, False)], CJ_GAP, [(jump, False)])      # присед → 2 мс → прыжок
        else:
            run_macro([(jump, True)], CJ_GAP, [(duck, True)])        # прыжок ↑ → 2 мс → присед ↑
    elif b == "jb":
        if down:
            held = held_duck_keys()
            if not held:
                log("jumpbug: присед не зажат — будет обычный прыжок")
            run_macro([(k, True) for k in held] + [(jump, False)])   # отпустить присед + прыжок — одной пачкой
        else:
            run_macro([(jump, True)])
    elif b == "dv" and down:
        cp, tp = dv_key("cp"), dv_key("tp")
        if cp is None or tp is None:
            log("danvari: не назначены клавиши чекпоинта / телепорта (⚙ бинды → danvari: клавиши cs2)")
            return
        run_macro([(jump, False), (jump, True), (cp, False), (cp, True), (tp, False), (tp, True)])


def tap_bind(b):
    """Бинд на колесе: у колеса есть только «щелчок» — нажимаем и сразу отпускаем.
    66-бинды (lj, cj) — особый случай: присед держим WHEEL_DUCK_HOLD секунд, как будто человек держит кнопку."""
    if b not in ("lj", "cj"):
        bind_event(b, True)
        bind_event(b, False)
        return
    bind_event(b, True)                    # бинд уже «держится» (колесо щёлкнуло ещё раз) — просто продлеваем
    gen = wheel_gen[b] = wheel_gen.get(b, 0) + 1

    def jump_up():
        if wheel_gen.get(b) == gen and b in state["macro_down"]:
            run_macro([(cs2_key("jump"), True)])   # прыжок отпускаем сразу — чтобы не было лишнего прыжка при приземлении

    def all_up():
        if wheel_gen.get(b) == gen:                # не было нового щелчка — отпускаем всё (прыжок ↑ → присед ↑)
            bind_event(b, False)

    threading.Timer(WHEEL_JUMP_HOLD, jump_up).start()
    threading.Timer(WHEEL_DUCK_HOLD, all_up).start()


def release_all():
    for k, down in state["held"].items():
        if down:
            send_key(SCAN[k], up=True)
        state["held"][k] = False
    for b in list(state["macro_down"]):  # выключили программу посреди бинда — отпускаем его клавиши
        bind_event(b, False)


def toggle():
    state["enabled"] = not state["enabled"]
    state["w_blocked"] = False
    release_all()
    play_click(state["enabled"])


def on_down(key):
    held = state["held"]
    if held[key]:                      # автоповтор при удержании — игнор
        return
    held[key] = True
    opp = OPPOSITE[key]
    if held[opp]:                      # противоположная зажата → отпускаем её
        send_key(SCAN[opp], up=True)
    send_key(SCAN[key])


def on_up(key):
    held = state["held"]
    held[key] = False
    send_key(SCAN[key], up=True)
    opp = OPPOSITE[key]
    if held[opp]:                      # противоположная ещё зажата → возвращаем её
        send_key(SCAN[opp])


def capture_key(data):
    """Режим назначения: запоминаем нажатую клавишу для выбранного бинда (или для вкл/выкл)."""
    target, vk = state["capturing"], data.vkCode
    state["capturing"] = None
    if vk == 0x1B:                                   # Esc: у биндов убирает бинд, у вкл/выкл — просто отмена
        if target in ALL_BINDS:
            state[target], state[target + "_name"] = None, ""
        elif target in ("dv_cp", "dv_tp"):
            state[target] = None                     # снова берём из настроек CS2
        return
    if vk in VK_TO_KEY:                              # W A S D назначать нельзя
        return
    bind = ("key", vk)
    used = {state[b] for b in ALL_BINDS if b != target and state[b]}
    if target in ("dv_cp", "dv_tp"):                 # клавиша ИЗ CS2 (cs_cp / cs_tp), её не перехватываем
        if bind not in used and bind != state[target] and vk != state["toggle_vk"]:
            state[target] = (key_name(data), ("k", data.scanCode, bool(data.flags & 0x01)))
        return
    if target == "toggle":
        if bind not in used:
            state["toggle_vk"], state["toggle_name"] = vk, key_name(data)
        return
    if target in ALL_BINDS and vk != state["toggle_vk"] and bind not in used:   # одна клавиша — одно действие
        state[target], state[target + "_name"] = bind, key_name(data)


WATCH = {0x57: "W", 0x41: "A", 0x53: "S", 0x44: "D"}


def win_filter(msg, data):
    """Обёртка: пишет в журнал нажатия WASD и что программа с ними сделала."""
    name = WATCH.get(data.vkCode)
    if name is None or data.flags & LLKHF_INJECTED:
        return _win_filter(msg, data)
    before = f"{name} {'↓' if msg in KEYDOWN else '↑'}  (blocked={state['w_blocked']} " \
             f"held={state['held'][name.lower()]} вкл={state['enabled']})"
    try:
        result = _win_filter(msg, data)
        log(before + "  → пропущено как есть")
        return result
    except Exception:
        log(before + "  → перехвачено программой")
        raise


def _win_filter(msg, data):
    """Вызывается Windows на КАЖДОЕ нажатие клавиши в системе."""
    if data.flags & LLKHF_INJECTED:    # наши же нажатия — пропускаем
        return True

    # --- следим, что человек реально держит, и отличаем новое нажатие от автоповтора ---
    key = VK_TO_KEY.get(data.vkCode)
    down = msg in KEYDOWN
    repeat = False
    if down:                                          # все зажатые клавиши — нужно jumpbug'у (какой присед держат)
        state["keys_down"].add(data.vkCode)
    else:
        state["keys_down"].discard(data.vkCode)
    if key:
        repeat = down and state["phys"][key]          # была зажата и снова «нажата» — автоповтор
        state["phys"][key] = down
        if down and not repeat:                       # новое нажатие — сбрасываем всё «залипшее»
            state["held"][key] = False
            if key == "w":
                state["w_blocked"] = False

    if state["capturing"] and msg in KEYDOWN:
        capture_key(data)
        listener.suppress_event()      # эта клавиша никуда не уходит

    if data.vkCode == state["toggle_vk"]:          # клавиша вкл/выкл
        if msg in KEYDOWN and not state["toggle_down"]:
            toggle()
        state["toggle_down"] = msg in KEYDOWN
        return True

    # --- бинды на клавиатуре ---
    b = bind_of(("key", data.vkCode))
    if b == "unw":                                    # -w: сама клавиша тоже доходит до игры (можно на пробел)
        if msg in KEYDOWN and game_ok():
            release_w()
        return True
    if b in MACRO_BINDS:                              # 66 / jumpbug / danvari: сама клавиша в игру НЕ идёт
        if game_ok() or b in state["macro_down"]:     # отпускание пропускать нельзя, даже если вышли из игры
            bind_event(b, msg in KEYDOWN)
            listener.suppress_event()
        return True

    if key is None:
        return True
    if key is None:
        return True

    # W отпущена биндом: пока держишь её — не нажимаем снова
    if key == "w" and state["w_blocked"]:
        if msg in KEYDOWN:
            listener.suppress_event()
        state["w_blocked"] = False     # отпустил W — дальше работает как обычно
        return True

    if msg in KEYDOWN:
        if repeat:                     # автоповтор: если клавишей рулим мы — глушим, иначе пропускаем
            if state["held"][key]:
                listener.suppress_event()
            return True
        if not is_active(key):         # выключено / не CS2 — клавиша работает как обычно
            return True
        on_down(key)
    else:
        if not state["held"][key]:     # эту клавишу мы не перехватывали — пропускаем
            return True
        on_up(key)
    listener.suppress_event()          # оригинальное нажатие дальше не пускаем


# ================== Мышь: колесо и кнопки для биндов ==================
# Всё делаем прямо в фильтре pynput: только отсюда событие мыши можно «заглушить» (чтобы, например,
# mouse 4 с биндом не делал в игре ещё и своё действие). Тут же следим, какие кнопки человек держит.
LLMHF_INJECTED = 0x01
MOUSE_MSGS = {0x201: ("MOUSE1", True), 0x202: ("MOUSE1", False), 0x204: ("MOUSE2", True),
              0x205: ("MOUSE2", False), 0x207: ("MOUSE3", True), 0x208: ("MOUSE3", False)}
MOUSE_TRIGGERS = {"MOUSE3": ("mouse", "middle"), "MOUSE4": ("mouse", "x1"), "MOUSE5": ("mouse", "x2")}
MOUSE_LABELS = {("mouse", "middle"): "Wheel click", ("mouse", "x1"): "Mouse 4", ("mouse", "x2"): "Mouse 5",
                ("wheel", "up"): "Wheel up", ("wheel", "down"): "Wheel down"}


def mouse_capture(trigger):
    """Назначить бинд мышью, если сейчас режим выбора. True — событие «съели» (в игру не пускаем)."""
    target = state["capturing"]
    if target not in CAPTURE_MOUSE:
        return False
    if trigger[0] == "wheel" and target not in WHEEL_OK:
        return True                          # этот бинд на колесо не ставится — ждём другую кнопку
    if bind_of(trigger) not in (None, target):
        return True                          # эта кнопка уже занята другим биндом
    state["capturing"] = None
    state[target], state[target + "_name"] = trigger, MOUSE_LABELS[trigger]
    return True


def mouse_filter(msg, data):
    """Вызывается Windows на каждое событие мыши. Движения пропускаем сразу (их больше всего)."""
    if msg == 0x0200 or data.flags & LLMHF_INJECTED:      # движение или наш же клик — не трогаем
        return True
    hi = (data.mouseData >> 16) & 0xFFFF
    if msg == 0x020A:                                       # колесо
        trigger = ("wheel", "up" if hi < 0x8000 else "down")
        if mouse_capture(trigger):
            mouse_listener.suppress_event()
        b = bind_of(trigger)
        if b and game_ok():
            log(f"колесо {trigger[1]} → бинд {b}")
            if b == "unw":
                release_w()                                 # -w: колесо дальше идёт в игру как обычно
                return True
            tap_bind(b)
            mouse_listener.suppress_event()                 # 66 / jumpbug / danvari сами жмут прыжок
        return True
    if msg in (0x020B, 0x020C):                             # боковые кнопки
        name, down = ("MOUSE4" if hi == 1 else "MOUSE5"), msg == 0x020B
    elif msg in MOUSE_MSGS:
        name, down = MOUSE_MSGS[msg]
    else:
        return True
    if down:
        state["mouse_down"].add(name)
    else:
        state["mouse_down"].discard(name)
    trigger = MOUSE_TRIGGERS.get(name)
    if trigger is None:                                     # левая / правая кнопки не биндятся
        return True
    if down and mouse_capture(trigger):
        mouse_listener.suppress_event()
    b = bind_of(trigger)
    if b == "unw":
        if down and game_ok():
            release_w()
    elif b in MACRO_BINDS and (game_ok() or b in state["macro_down"]):
        bind_event(b, down)
        mouse_listener.suppress_event()
    return True


# Режим экономии: перехват мыши нужен, только когда он и правда нужен.
# Пока хук мыши включён, Windows ждёт ответа от Python на КАЖДОЕ событие мыши
# (у мыши 1000 Гц это до 1000 раз в секунду) — поэтому без надобности его не держим.
mouse_listener = None


def mouse_hook_needed():
    if state["capturing"] in CAPTURE_MOUSE:                 # ждём, что человек нажмёт кнопку мыши
        return True
    if any(state[b] is not None and state[b][0] in ("wheel", "mouse") for b in ALL_BINDS):
        return True
    # jumpbug, а присед в CS2 на кнопке мыши: надо знать, держит ли человек эту кнопку
    return state["jb"] is not None and any(k.startswith("MOUSE") for k in cs2_binds["duck"])


def update_mouse_hook():
    """Включить или выключить перехват мыши, если что-то поменялось. Зовём из главного цикла.
    Остановленный Listener в pynput запустить снова нельзя — поэтому каждый раз создаём новый."""
    global mouse_listener
    need = mouse_hook_needed()
    if need and mouse_listener is None:
        state["mouse_down"].clear()
        mouse_listener = mouse.Listener(win32_event_filter=mouse_filter)
        mouse_listener.start()
        log("хук мыши: включён")
    elif not need and mouse_listener is not None:
        mouse_listener.stop()
        mouse_listener = None
        log("хук мыши: выключен (мышь не нужна)")


# ================== Язык ==================
LANG = "ru"
TR = {
    "ru": {
        "tab_nulls": "нуллы", "tab_binds": "бинды",
        "fwd_back": "вперёд / назад", "left_right": "влево / вправо",
        "only_cs2": "только в cs2", "only_cs2_sub": "в других окнах клавиши обычные",
        "unw_sub": "отпустить вперёд",
        "toggle": "вкл / выкл", "toggle_sub": "включить или выключить программу",
        "bind_hint": "нажми кнопку, потом клавишу · esc — убрать\n"
                     "можно mouse 4/5 и колесо · 66 на колесе держит присед 0,8 с",
        "lj_name": "66 с -w", "lj_sub": "присед + прыжок + отпустить w",
        "cj_name": "66 без -w", "cj_sub": "присед + прыжок",
        "jb_sub": "держи присед, у земли жми бинд", "dv_sub": "прыжок + cp + tp в один тик",
        "dv_keys": "danvari: клавиши cs2", "dv_keys_sub": "на них в cs2 — cs_cp и cs_tp",
        "cs2_found": "в cs2: прыжок {jump} · присед {duck}", "cs2_none": "настройки cs2 не найдены — жму space и ctrl",
        "log": "журнал", "log_sub": "если что-то глючит — сохрани и скинь", "save": "сохранить",
        "sound": "звук", "click": "щелчок", "click_sub": "звук при вкл / выкл",
        "enter_cs2": "вход в cs2", "enter_cs2_sub": "щелчок, когда переключаешься в игру",
        "volume": "громкость", "pitch": "тон", "style": "стиль",
        "look": "вид", "language": "язык",
        "assign": "назначить", "waiting": "жду ввод…", "hint": "{key} — вкл / выкл",
        "мягкий": "мягкий", "щелчок": "щелчок", "тик": "тик",
        "set_sound": "звук", "set_ui": "интерфейс", "set_about": "о программе",
        "accent": "цвет", "overlay": "оверлей", "overlay_sub": "статус поверх игры, только в cs2",
        "set_overlay": "оверлей", "position": "где на экране",
        "move": "переместить", "done": "готово", "move_sub": "перетащи плашку мышкой куда удобно",
        "moving_sub": "тащи плашку · двойной клик по ней — готово",
        "tray": "трей", "tray_sub": "при закрытии прятать в трей",
        "tray_open": "открыть", "tray_toggle": "включена", "tray_exit": "выход",
        "tray_hint": "skywalk работает в трее",
        "tab_maps": "карты", "tab_records": "рекорды",
        "rec_on": "слежу за консолью cs2 · ник {nick}",
        "rec_nonick": "не нашёл ник steam — войди в steam и перезапусти программу",
        "rec_off": "нет console.log: добавь -condebug в параметры запуска cs2\n(steam → cs2 → свойства → параметры запуска)",
        "rec_reset": "сбросить рекорды", "rec_reset_sub": "удалить все лучшие прыжки",
        "rec_alltime": "рекорды", "rec_session": "сессия", "rec_progress": "прогресс", "rec_coach": "тренер",
        "card_to_next": "до {t}", "card_top": "wrecker ✓",
        "coach_last": "последний {abbr} · {dist}", "coach_record": "рекорд {abbr} · {dist}",
        "coach_gap": "до {target}: {gap}", "coach_done": "цель {target} ✓",
        "coach_none": "в этой сессии ещё нет {abbr}.\nпрыгни — и я разберу прыжок по стрейфам",
        "coach_summary": "сессия {abbr} ({n}): sync {sync}% · bad {bad}% · overlap {ovl}%\ndead air {dead}% · gain eff {eff}% · pre {pre}",
        "coach_often": "чаще всего: {list}",
        "prog_title": "лучший {abbr} по дням", "prog_few": "график появится, когда наберётся хотя бы два дня прыжков",
        "tip_where": " (хуже всего стрейфы {n}: до {v}%)", "tip_where_n": " — стрейфы {n}",
        "tip_w_late": "w отпущена на {t} тик. позже прыжка — скорость теряется в самом начале. отпускай w ровно в момент прыжка (или бинд «66 с -w»)",
        "tip_pre_low": "pre {pre} — низковато, для 280+ нужен ~276: на престрейфе веди мышь плавно и широко, без рывков",
        "tip_first_gain": "1-й стрейф дал {g} (остальные ~{m}) — начинай поворот мышью сразу с первой клавишей, без паузы после прыжка",
        "tip_sync": "sync {s}%{where} — мышь и клавиша расходятся: веди мышь, пока зажата клавиша, и меняй направление вместе с a/d",
        "tip_bad": "bad angles {b}%{where}: клавиша зажата, а скорость не растёт — мышь стоит или ведёшь её рывками",
        "tip_overlap": "overlap {o}%{where}: a и d зажаты вместе — отпускай прошлую клавишу раньше (или включи нуллы a/d)",
        "tip_dead": "dead air{where}: между стрейфами ничего не зажато — жми следующую клавишу сразу, как отпустил прошлую",
        "tip_mouse_slow": "мышь ведёшь медленно (ang ratio {r}) — поворачивай быстрее, gain недобирается",
        "tip_mouse_fast": "мышь ведёшь слишком быстро (ang ratio {r}) — плавнее, угол проскакивает идеальный",
        "tip_key_late": "клавишу жмёшь на {t} тика (~{ms} мс) позже поворота мыши — жми a/d одновременно с началом поворота",
        "tip_key_early": "клавишу жмёшь на {t} тика (~{ms} мс) раньше поворота мыши — сначала начинай вести мышь",
        "tip_release_early": "клавишу отпускаешь на {t} тика раньше конца поворота — держи её до конца стрейфа",
        "tip_path": "траектория кривая (airpath {a}, отклонение {d}) — делай стрейфы одинаковыми по ширине",
        "tip_strafes_few": "всего {n} стрейфов — для 280+ обычно 9–11: делай стрейфы короче и чаще",
        "tip_strafes_many": "{n} стрейфов — слишком много коротких, делай их длиннее",
        "tip_eff": "gain eff {e}% — веди мышь ровнее через весь стрейф",
        "tip_clean": "чистый прыжок — явных ошибок нет, дальше стабильность и повторение",
        "tipname": "w_late:поздний w|pre_low:низкий pre|first_gain:1-й стрейф|sync:sync|bad:bad angles|overlap:overlap|dead:dead air|mouse_slow:медленная мышь|mouse_fast:быстрая мышь|key_late:поздняя клавиша|key_early:ранняя клавиша|release_early:ранний отпуск|path:траектория|strafes_few:мало стрейфов|strafes_many:много стрейфов|eff:gain eff",
        "ses_status": "сессия с {since} · прыжков: {n}", "ses_empty": "в этой сессии ещё нет прыжков",
        "ses_new": "новая сессия", "ses_new_sub": "обнулить счёт прыжков", "ses_new_btn": "начать",
        "ses_title": "{abbr} · сессия", "ses_count": "прыжков", "ses_best": "лучший", "ses_avg": "средний",
        "rec_toast": "новый рекорд", "rec_toast_click": "клик — подробнее", "rec_first": "первый",
        "rec_str": "стр", "rec_block": "блок {b}",
        "months": "янв фев мар апр мая июн июл авг сен окт ноя дек",
        "watch_map": "карты", "watch_map_sub": "какие ждать · до 6",
        "regions": "регионы", "regions_sub": "пинг в мс · не выбрано — любой",
        "regions_empty": "нажми «проверить», чтобы загрузить регионы",
        "watch": "слежка", "watch_sub": "проверять каждые 45 сек",
        "steam_key": "steam api key", "steam_key_sub": "нужен для списка серверов",
        "check_now": "проверить", "click_hint": "клик по строке — подключиться",
        "st_nokey": "вставь steam api key: ⚙ → интерфейс",
        "st_badkey": "ключ steam не подходит", "st_net": "нет связи со steam",
        "st_wait": "проверяю…", "st_idle": "ещё не проверяли",
        "st_ok": "{n} kz серверов · {ago} сек назад",
        "st_nomap": "добавь карту сверху",
        "elsewhere": "{map} · только {where}",
        "nowhere": "нигде нет: {maps}", "more": "… и ещё {n}",
        "ms": "мс",
        "notify": "{map} появилась · #{num} [{region}] · {players}/{max} · {ping}",
        "toast_title": "карта нашлась", "toast_click": "клик — подключиться",
        "reset": "сброс", "reset_sub": "вернуть всё по умолчанию",
        "reset_btn": "сбросить", "reset_sure": "точно?",
        "author": "автор", "version": "версия",
        "changes": "что нового", "changes_sub": "что менялось от версии к версии", "open": "открыть",
            },
    "en": {
        "tab_nulls": "nulls", "tab_binds": "binds",
        "fwd_back": "forward / back", "left_right": "left / right",
        "only_cs2": "cs2 only", "only_cs2_sub": "other windows work as usual",
        "unw_sub": "release forward",
        "toggle": "on / off", "toggle_sub": "turn the program on or off",
        "bind_hint": "click a button, then a key · esc — clear\n"
                     "mouse 4/5 and wheel work too · 66 on wheel holds duck 0.8 s",
        "lj_name": "66 with -w", "lj_sub": "duck + jump + release w",
        "cj_name": "66 without -w", "cj_sub": "duck + jump",
        "jb_sub": "hold duck, press near the ground", "dv_sub": "jump + cp + tp in one tick",
        "dv_keys": "danvari: cs2 keys", "dv_keys_sub": "bound to cs_cp and cs_tp in cs2",
        "cs2_found": "in cs2: jump {jump} · duck {duck}", "cs2_none": "cs2 settings not found — using space and ctrl",
        "log": "log", "log_sub": "something buggy? save it and send it", "save": "save",
        "sound": "sound", "click": "click", "click_sub": "sound when toggling",
        "enter_cs2": "entering cs2", "enter_cs2_sub": "click when you switch to the game",
        "volume": "volume", "pitch": "pitch", "style": "style",
        "look": "look", "language": "language",
        "assign": "assign", "waiting": "waiting…", "hint": "{key} — on / off",
        "мягкий": "soft", "щелчок": "click", "тик": "tick",
        "set_sound": "sound", "set_ui": "interface", "set_about": "about",
        "accent": "color", "overlay": "overlay", "overlay_sub": "status over the game, cs2 only",
        "set_overlay": "overlay", "position": "position on screen",
        "move": "move", "done": "done", "move_sub": "drag the badge wherever you like",
        "moving_sub": "drag the badge · double-click it when done",
        "tray": "tray", "tray_sub": "hide to tray on close",
        "tray_open": "open", "tray_toggle": "enabled", "tray_exit": "exit",
        "tray_hint": "skywalk is running in the tray",
        "tab_maps": "maps", "tab_records": "records",
        "rec_on": "watching the cs2 console · nick {nick}",
        "rec_nonick": "steam nickname not found — log in to steam and restart the program",
        "rec_off": "no console.log: add -condebug to cs2 launch options\n(steam → cs2 → properties → launch options)",
        "rec_reset": "reset records", "rec_reset_sub": "delete all best jumps",
        "rec_alltime": "records", "rec_session": "session", "rec_progress": "progress", "rec_coach": "coach",
        "card_to_next": "to {t}", "card_top": "wrecker ✓",
        "coach_last": "last {abbr} · {dist}", "coach_record": "record {abbr} · {dist}",
        "coach_gap": "to {target}: {gap}", "coach_done": "goal {target} ✓",
        "coach_none": "no {abbr} in this session yet.\njump and I'll break it down strafe by strafe",
        "coach_summary": "session {abbr} ({n}): sync {sync}% · bad {bad}% · overlap {ovl}%\ndead air {dead}% · gain eff {eff}% · pre {pre}",
        "coach_often": "most often: {list}",
        "prog_title": "best {abbr} by day", "prog_few": "the chart appears once there are at least two days of jumps",
        "tip_where": " (worst strafes {n}: down to {v}%)", "tip_where_n": " — strafes {n}",
        "tip_w_late": "w released {t} ticks after the jump — speed is lost right away. release w exactly when you jump (or use the «66 with -w» bind)",
        "tip_pre_low": "pre {pre} is low, 280+ needs ~276: move the mouse smoothly and wide during prestrafe",
        "tip_first_gain": "strafe 1 gained {g} (others ~{m}) — start turning together with the first key, no pause after the jump",
        "tip_sync": "sync {s}%{where} — mouse and key disagree: turn while the key is held and switch with a/d",
        "tip_bad": "bad angles {b}%{where}: key held but no speed gained — the mouse stops or jerks",
        "tip_overlap": "overlap {o}%{where}: a and d held together — release the previous key earlier (or turn on a/d nulls)",
        "tip_dead": "dead air{where}: no key held between strafes — press the next key right as you release the last",
        "tip_mouse_slow": "you turn too slowly (ang ratio {r}) — turn faster, gain is left on the table",
        "tip_mouse_fast": "you turn too fast (ang ratio {r}) — smoother, you overshoot the ideal angle",
        "tip_key_late": "you press the key {t} ticks (~{ms} ms) after the mouse starts turning — press a/d with the turn",
        "tip_key_early": "you press the key {t} ticks (~{ms} ms) before the mouse turns — start the turn first",
        "tip_release_early": "you release the key {t} ticks before the turn ends — hold it to the end of the strafe",
        "tip_path": "curved path (airpath {a}, deviation {d}) — keep strafes equal in width",
        "tip_strafes_few": "only {n} strafes — 280+ usually takes 9–11: shorter, more frequent strafes",
        "tip_strafes_many": "{n} strafes — too many short ones, make them longer",
        "tip_eff": "gain eff {e}% — turn more evenly through the whole strafe",
        "tip_clean": "clean jump — no obvious mistakes, now it is about consistency",
        "tipname": "w_late:late w|pre_low:low pre|first_gain:strafe 1|sync:sync|bad:bad angles|overlap:overlap|dead:dead air|mouse_slow:slow mouse|mouse_fast:fast mouse|key_late:late key|key_early:early key|release_early:early release|path:path|strafes_few:few strafes|strafes_many:many strafes|eff:gain eff",
        "ses_status": "session since {since} · jumps: {n}", "ses_empty": "no jumps in this session yet",
        "ses_new": "new session", "ses_new_sub": "reset the jump counter", "ses_new_btn": "start",
        "ses_title": "{abbr} · session", "ses_count": "jumps", "ses_best": "best", "ses_avg": "average",
        "rec_toast": "new record", "rec_toast_click": "click for details", "rec_first": "first",
        "rec_str": "str", "rec_block": "block {b}",
        "months": "jan feb mar apr may jun jul aug sep oct nov dec",
        "watch_map": "maps", "watch_map_sub": "which to wait for · up to 6",
        "regions": "regions", "regions_sub": "ping in ms · none selected — any",
        "regions_empty": "press “check” to load regions",
        "watch": "watch", "watch_sub": "check every 45 s",
        "steam_key": "steam api key", "steam_key_sub": "needed for the server list",
        "check_now": "check", "click_hint": "click a line to connect",
        "st_nokey": "add steam api key: ⚙ → interface",
        "st_badkey": "steam key rejected", "st_net": "can't reach steam",
        "st_wait": "checking…", "st_idle": "not checked yet",
        "st_ok": "{n} kz servers · {ago}s ago",
        "st_nomap": "add a map above",
        "elsewhere": "{map} · only {where}",
        "nowhere": "nowhere: {maps}", "more": "… and {n} more",
        "ms": "ms",
        "notify": "{map} is up · #{num} [{region}] · {players}/{max} · {ping}",
        "toast_title": "map found", "toast_click": "click to connect",
        "reset": "reset", "reset_sub": "restore all defaults",
        "reset_btn": "reset", "reset_sure": "sure?",
        "author": "author", "version": "version",
        "changes": "what's new", "changes_sub": "what changed from version to version", "open": "open",
            },
}
LANG_LABELS = {"ru": "рус", "en": "eng"}


def tr(name, **kw):
    """Текст на текущем языке."""
    text = TR[LANG].get(name, name)
    return text.format(**kw) if kw else text


# ================== Сохранение настроек ==================
# Файл: C:\Users\<имя>\AppData\Roaming\Skywalk\settings.json
APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
SETTINGS_PATH = os.path.join(APPDATA, "Skywalk", "settings.json")
OLD_SETTINGS_PATH = os.path.join(APPDATA, "BlessKey", "settings.json")   # до переименования


# настройки внешнего вида
DEFAULT_PREFS = {"accent": "blue", "overlay": False, "overlay_pos": "mr",  # позиция: см. SCREEN_POS
                 "overlay_xy": None,         # своё место оверлея [x, y] в долях экрана (0..1), None — по умолчанию
                 "tray": True,
                 # слежка за картами на серверах Cybershoke
                 "steam_key": "", "watch_maps": [], "watch_regions": [], "watch_on": False,
                 "known_maps": [],           # все известные карты: с серверов + из базы CS2KZ
                 "maps_synced": 0,           # когда последний раз скачивали базу карт CS2KZ
                 "records": {},              # лучшие прыжки: {"LJ": {"dist": 285.8, "pre": 286.0, ...}}
                 "session_mark": {},         # «новая сессия»: {начало файла консоли, сколько прыжков пропустить}
                 "history": {},              # лучший прыжок по дням: {"LJ": {"2026-10-12": 281.84}}
                 "kz_servers": []}           # адреса KZ-серверов Cybershoke, которые программа уже видела

# 9 мест на экране: t/m/b — верх/середина/низ, l/c/r — лево/центр/право.
# Числа — доля пути от левого/верхнего края (0) до правого/нижнего (1).
SCREEN_POS = {"tl": (0, 0), "tc": (0.5, 0), "tr": (1, 0),
              "ml": (0, 0.5), "mc": (0.5, 0.5), "mr": (1, 0.5),
              "bl": (0, 1), "bc": (0.5, 1), "br": (1, 1)}
prefs = dict(DEFAULT_PREFS)

# значения по умолчанию — запоминаем при запуске, до загрузки файла (для кнопки «сброс»)
DEFAULT_SOUND = dict(sound)
DEFAULT_PAIRS = dict(state["pairs"])


def reset_settings():
    """Кнопка «сброс»: всё как при первом запуске."""
    global LANG
    LANG = "ru"
    sound.clear()
    sound.update(DEFAULT_SOUND)
    state["pairs"].update(DEFAULT_PAIRS)
    state.update(only_cs2=True, toggle_vk=VK_F8, toggle_name="F8", dv_cp=None, dv_tp=None)
    for b in ALL_BINDS:
        state[b], state[b + "_name"] = None, ""
    key = prefs.get("steam_key", "")        # ключ Steam при сбросе оставляем — его неудобно вводить заново
    known = prefs.get("known_maps", [])       # список карт тоже оставляем — это не настройка, а знания
    servers = prefs.get("kz_servers", [])     # и адреса серверов — тоже знания
    records = prefs.get("records", {})        # рекорды прыжков «сброс» не трогает — у них своя кнопка
    history = prefs.get("history", {})
    prefs.clear()
    prefs.update(DEFAULT_PREFS, watch_regions=[], watch_maps=[], steam_key=key, known_maps=known,
                 kz_servers=servers, records=records, history=history)
    set_accent(prefs["accent"])
    rebuild_sounds()


def snapshot():
    """Все настройки, которые надо помнить, одним словарём."""
    return {
        "lang": LANG,
        "sound": dict(sound),
        "pairs": dict(state["pairs"]),
        "only_cs2": state["only_cs2"],
        "toggle_vk": state["toggle_vk"], "toggle_name": state["toggle_name"],
        **{b: list(state[b]) if state[b] else None for b in ALL_BINDS},
        **{b + "_name": state[b + "_name"] for b in ALL_BINDS},
        **{k: [state[k][0], state[k][1][1], state[k][1][2]] if state[k] else None for k in ("dv_cp", "dv_tp")},
        "prefs": dict(prefs),
    }


CRASH_PATH = os.path.join(APPDATA, "Skywalk", "crash.txt")


def write_crash(text):
    """Ошибку Python пишем в файл — чтобы увидеть причину, даже если окно закрылось."""
    log("ОШИБКА:\n" + text)
    try:
        os.makedirs(os.path.dirname(CRASH_PATH), exist_ok=True)
        with open(CRASH_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {VERSION} =====\n{text}")
    except OSError:
        pass


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)      # сначала пишем во временный файл — так файл не побьётся
    except OSError as e:
        log(f"не удалось сохранить настройки: {e}")


def load_settings():
    """Читает файл настроек. Всё, что там странное или битое, просто пропускаем."""
    global LANG
    path = SETTINGS_PATH
    if not os.path.exists(path) and os.path.exists(OLD_SETTINGS_PATH):
        path = OLD_SETTINGS_PATH             # первый запуск после переименования — берём старые настройки
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return                               # файла ещё нет (первый запуск) или он битый
    if data.get("lang") in TR:
        LANG = data["lang"]
    snd = data.get("sound", {})
    for key in ("on", "cs2"):
        if isinstance(snd.get(key), bool):
            sound[key] = snd[key]
    for key, lo, hi in (("volume", 0, 100), ("pitch", 50, 200)):
        if isinstance(snd.get(key), int):
            sound[key] = max(lo, min(hi, snd[key]))
    if snd.get("style") in STYLES:
        sound["style"] = snd["style"]
    for pair in ("ad", "ws"):
        if isinstance(data.get("pairs", {}).get(pair), bool):
            state["pairs"][pair] = data["pairs"][pair]
    if isinstance(data.get("only_cs2"), bool):
        state["only_cs2"] = data["only_cs2"]
    if isinstance(data.get("toggle_vk"), int) and data.get("toggle_name"):
        state["toggle_vk"], state["toggle_name"] = data["toggle_vk"], str(data["toggle_name"])
    p = data.get("prefs", {})
    if p.get("accent") in ACCENTS:
        prefs["accent"] = p["accent"]
        set_accent(p["accent"])
    if isinstance(p.get("steam_key"), str):
        prefs["steam_key"] = p["steam_key"][:100]
    if isinstance(p.get("watch_maps"), list):
        prefs["watch_maps"] = [x[:100] for x in p["watch_maps"] if isinstance(x, str) and x.strip()][:MAX_WATCH]
    elif isinstance(p.get("watch_map"), str) and p["watch_map"].strip():
        prefs["watch_maps"] = [p["watch_map"].strip()[:100]]   # старые настройки: была одна карта
    if isinstance(p.get("known_maps"), list):
        prefs["known_maps"] = [x for x in p["known_maps"] if isinstance(x, str)][:3000]
    hist = p.get("history")
    if isinstance(hist, dict):
        prefs["history"] = {a: {d: float(v) for d, v in days.items() if isinstance(d, str) and isinstance(v, (int, float))}
                            for a, days in hist.items() if a in JUMP_NAMES and isinstance(days, dict)}
    mark = p.get("session_mark")
    if isinstance(mark, dict) and isinstance(mark.get("head"), str) and isinstance(mark.get("skip"), int):
        prefs["session_mark"] = {"head": mark["head"][:600], "skip": max(0, mark["skip"])}
    if isinstance(p.get("records"), dict):
        recs = {}
        for abbr, rec in p["records"].items():
            if abbr in JUMP_NAMES and isinstance(rec, dict) and isinstance(rec.get("dist"), (int, float)):
                recs[abbr] = {k: v for k, v in rec.items() if isinstance(v, (int, float))}
                details = rec.get("details")
                if isinstance(details, list):
                    recs[abbr]["details"] = [str(t)[:60] for t in details if isinstance(t, str)][:40]
                if isinstance(rec.get("map"), str):
                    recs[abbr]["map"] = rec["map"][:64]
                rows = rec.get("strafe_rows")       # таблица стрейфов рекордного прыжка (для тренера)
                if isinstance(rows, list) and all(isinstance(s, dict) for s in rows):
                    recs[abbr]["strafe_rows"] = [{k: v for k, v in s.items() if isinstance(v, (int, float)) or v is None}
                                                 for s in rows[:30]]
                graph = rec.get("graph")
                if isinstance(graph, dict):
                    recs[abbr]["graph"] = {k: v[:300] for k, v in graph.items() if isinstance(v, str)}
        prefs["records"] = recs
    if isinstance(p.get("kz_servers"), list):
        prefs["kz_servers"] = [x for x in p["kz_servers"] if isinstance(x, str) and ":" in x][:1000]
    if isinstance(p.get("maps_synced"), (int, float)):
        prefs["maps_synced"] = p["maps_synced"]
    if isinstance(p.get("watch_regions"), list):
        prefs["watch_regions"] = [x for x in p["watch_regions"] if isinstance(x, str)][:30]
    for key in ("overlay", "tray", "watch_on"):
        if isinstance(p.get(key), bool):
            prefs[key] = p[key]
    xy = p.get("overlay_xy")
    if isinstance(xy, list) and len(xy) == 2 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in xy):
        prefs["overlay_xy"] = [float(xy[0]), float(xy[1])]
    if p.get("overlay_pos") in SCREEN_POS:
        prefs["overlay_pos"] = p["overlay_pos"]
    used = set()
    for b in ALL_BINDS:                       # колесо — только там, где можно; одна клавиша — один бинд
        v = data.get(b)
        kinds = ("key", "wheel", "mouse") if b in WHEEL_OK else ("key", "mouse")
        ok = isinstance(v, list) and len(v) == 2 and v[0] in kinds and (
            isinstance(v[1], int) if v[0] == "key" else tuple(v) in MOUSE_LABELS)   # мышь/колесо — только известные
        if ok and tuple(v) not in used:
            state[b], state[b + "_name"] = tuple(v), str(data.get(b + "_name", ""))
            used.add(tuple(v))
    for k in ("dv_cp", "dv_tp"):
        v = data.get(k)
        if isinstance(v, list) and len(v) == 3 and isinstance(v[1], int) and 0 < v[1] < 0x80:
            state[k] = (str(v[0]), ("k", v[1], bool(v[2])))



# ================== Цвета ==================
BG = "#0b0b0d"          # фон окна
PANEL = "#151518"       # строки-«таблетки»
KEYCAP = "#0a0a0c"      # кнопки клавиш и переключатели
HOVER = "#1d1d22"
TEXT = "#ececf0"
MUTED = "#72727c"

# Цвет акцента можно выбрать в настройках. У каждого: сам цвет, тёмный вариант,
# цвет свечения и три цвета для переливания «sky» (тёмный → акцент → светлый).
ACCENTS = {
    "blue":   dict(ACCENT="#4f7cff", ACCENT_DIM="#1e3a8a", GLOW="#2f5cff",
                   SHIM=("#2f5cff", "#4f7cff", "#b3c4ff")),
    "violet": dict(ACCENT="#9b6bff", ACCENT_DIM="#3f2587", GLOW="#7c3aed",
                   SHIM=("#7c3aed", "#9b6bff", "#dccbff")),
    "red":    dict(ACCENT="#ff4d5e", ACCENT_DIM="#7f1d2a", GLOW="#e11d48",
                   SHIM=("#e11d48", "#ff4d5e", "#ffc4ca")),
    "green":  dict(ACCENT="#2ee6a6", ACCENT_DIM="#0f5e46", GLOW="#10b981",
                   SHIM=("#10b981", "#2ee6a6", "#bdf8e3")),
    "orange": dict(ACCENT="#ff8a3d", ACCENT_DIM="#7a3510", GLOW="#f97316",
                   SHIM=("#f97316", "#ff8a3d", "#ffd4b4")),
    "pink":   dict(ACCENT="#ff5fb0", ACCENT_DIM="#7a1f50", GLOW="#ec4899",
                   SHIM=("#ec4899", "#ff5fb0", "#ffc9e5")),
}


def set_accent(name):
    """Делает выбранный цвет «текущим» (глобальные ACCENT, ACCENT_DIM, GLOW, SHIM)."""
    globals().update(ACCENTS[name])


set_accent("blue")


# ================== Частота кадров под монитор ==================
REFRESH_HZ = 60          # герцовка монитора (узнаём при запуске)
FRAME_MS = 16            # пауза между кадрами в миллисекундах


def setup_timing():
    """Узнаём герцовку монитора и делаем таймер Windows точным (до 1 мс),
    иначе Windows «округляет» паузы до ~16 мс и больше 60 кадров не получить."""
    global REFRESH_HZ, FRAME_MS
    try:
        hdc = user32.GetDC(0)
        hz = ctypes.windll.gdi32.GetDeviceCaps(hdc, 116)          # 116 = VREFRESH, герцовка
        user32.ReleaseDC(0, hdc)
        REFRESH_HZ = hz if 30 <= hz <= 500 else 60
    except Exception:
        REFRESH_HZ = 60
    FRAME_MS = max(2, round(1000 / REFRESH_HZ))                   # 60 Гц → 17 мс, 144 Гц → 7 мс
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def ease(speed, dt):
    """Сколько пройти к цели за время dt. speed — «доля за кадр при 60 кадрах».
    Так анимации идут с одинаковой скоростью на 60, 144 или 240 Гц — просто плавнее."""
    return 1 - (1 - speed) ** (dt * 60)


# ================== Интерфейс: помощники ==================
def resource(name):
    """Путь к файлу рядом с программой (работает и в .exe от PyInstaller)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def set_icon(win):
    try:
        win.iconbitmap(resource("icon.ico"))
    except Exception:
        pass


user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND


def hwnd_of(win):
    """Системный номер окна (HWND) — нужен для функций Windows."""
    win.update_idletasks()
    return user32.GetParent(win.winfo_id())


def remove_titlebar(win):
    """Убираем стандартную полосу Windows сверху, но окно остаётся «настоящим»:
    с иконкой на панели задач, в Alt+Tab и со сворачиванием."""
    try:
        hwnd = hwnd_of(win)
        GWL_STYLE = -16
        WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
        WS_MINIMIZEBOX, WS_SYSMENU, WS_MAXIMIZEBOX = 0x00020000, 0x00080000, 0x00010000
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        # без заголовка, без рамки для растягивания и без «развернуть на весь экран»
        style = (style & ~WS_CAPTION & ~WS_THICKFRAME & ~WS_MAXIMIZEBOX) | WS_MINIMIZEBOX | WS_SYSMENU
        user32.SetWindowLongW(hwnd, GWL_STYLE, style)
        # «Слоистый» режим (от прозрачности) снимаем — с ним окно рвётся при перетаскивании.
        # WS_EX_COMPOSITED НЕ ставим: Tk рисует элементы в обход этой буферизации,
        # и тогда криво перерисовываются вкладки.
        GWL_EXSTYLE, WS_EX_LAYERED = -20, 0x00080000
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
        # SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED — применить новый стиль
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004 | 0x0020)
    except Exception:
        pass


def set_layered(win, on):
    """Включить/выключить «слоистый» режим окна Windows (WS_EX_LAYERED).
    Он нужен только на время плавного появления (прозрачность). Tk сам его НЕ снимает, даже когда
    прозрачность вернули на 100%, — а в слоистом окне при перетаскивании появляются чёрные блоки.
    Поэтому после появления снимаем его вручную и просим Windows перерисовать окно."""
    try:
        hwnd = hwnd_of(win)
        style = user32.GetWindowLongW(hwnd, -20)                     # GWL_EXSTYLE
        new = style | 0x80000 if on else style & ~0x80000             # WS_EX_LAYERED
        if new != style:
            user32.SetWindowLongW(hwnd, -20, new)
            # SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
            # RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_FRAME — перерисовать целиком, но в своём темпе.
            # RDW_UPDATENOW (рисовать прямо сейчас) не ставим: это замирание окна на ~0,1 с при каждом показе.
            user32.RedrawWindow(hwnd, None, None, 0x0001 | 0x0080 | 0x0400)
    except Exception:
        pass


def no_window_animation(win):
    """Отключить анимацию Windows при сворачивании/разворачивании (DWMWA_TRANSITIONS_FORCEDISABLED = 3).
    Наше окно перед сворачиванием становится невидимым, поэтому эта анимация — просто 0,25 с ожидания."""
    try:
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd_of(win), 3, ctypes.byref(on), 4)
    except Exception:
        pass


def set_window_alpha(win, a):
    """Прозрачность окна (0 — невидимое, 1 — обычное) напрямую через Windows.
    Через Tk («-alpha») не годится: заданная до появления окна, она теряется."""
    try:
        set_layered(win, True)
        user32.SetLayeredWindowAttributes(hwnd_of(win), 0, max(0, min(255, round(a * 255))), 2)   # LWA_ALPHA
    except Exception:
        pass


def round_corners(win, radius=14):
    """Скруглённые углы окна.
    Windows 11 умеет сама (гладко). В Windows 10 такого нет — там «вырезаем» окно
    по форме скруглённого прямоугольника (регион окна)."""
    try:
        hwnd = hwnd_of(win)
        build = getattr(sys, "getwindowsversion", lambda: None)()
        if build is not None and build.build >= 22000:                      # Windows 11
            corners = ctypes.c_int(2)                                        # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corners), 4)
            return
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        d = int(radius * 2 * ctk.ScalingTracker.get_window_scaling(win))    # диаметр скругления
        gdi32 = ctypes.windll.gdi32
        gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        user32.SetWindowRgn(hwnd, rgn, True)                                 # регион теперь «принадлежит» окну
    except Exception:
        pass


def font(size=13, weight="normal", family="Segoe UI"):
    return ctk.CTkFont(family=family, size=size, weight=weight)


def load_font(size, bold=False):
    """Шрифт для рисования через Pillow (ищем стандартные шрифты Windows).
    bold=True — самый жирный (для названия), bold="semi" — обычный полужирный."""
    if bold == "semi":
        names = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
    elif bold:
        names = ("seguibl.ttf", "ariblk.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
    else:
        names = ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


# ---------- цвета и анимации ----------
def hex2rgb(h):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def rgb2hex(c):
    return "#%02x%02x%02x" % tuple(int(round(v)) for v in c)


def mix(a, b, t):
    """Цвет между a и b (t=0 → a, t=1 → b)."""
    t = max(0.0, min(1.0, t))
    return rgb2hex([x + (y - x) * t for x, y in zip(hex2rgb(a), hex2rgb(b))])


def shimmer(wave):
    """Цвет переливающихся букв «sky»: тёмный → акцент → светлый."""
    lo, mid, hi = SHIM
    return mix(lo, mid, wave * 2) if wave < 0.5 else mix(mid, hi, wave * 2 - 1)


class Tweens:
    """Плавно переводит цвета виджетов к нужным значениям (каждый кадр — чуть ближе)."""

    def __init__(self):
        self.items = {}

    def to(self, widget, prop, color):
        key = (id(widget), prop)
        if key not in self.items:           # первый раз — ставим сразу, без анимации
            self.items[key] = [widget, prop, hex2rgb(color), hex2rgb(color)]
            widget.configure(**{prop: color})
        else:
            self.items[key][3] = hex2rgb(color)

    def forget(self, widget, prop):
        self.items.pop((id(widget), prop), None)

    def step(self, dt, speed=0.25):
        speed = ease(speed, dt)
        for item in self.items.values():
            w, prop, cur, target = item
            if cur == target:
                continue
            nxt = [c + (t - c) * speed for c, t in zip(cur, target)]
            if max(abs(a - b) for a, b in zip(nxt, target)) < 1.5:
                nxt = list(target)
            item[2] = nxt
            w.configure(**{prop: rgb2hex(nxt)})


def fade_in(win, alpha=0.0):
    alpha = min(alpha + 0.08, 1.0)
    win.attributes("-alpha", alpha)
    if alpha < 1.0:
        win.after(FRAME_MS, fade_in, win, alpha)


# ================== Быстрые картинки ==================
# CTkImage на каждый кадр — медленно: каждый раз создаётся новая картинка и пересчитывается размер.
# Поэтому анимированные элементы — это обычный tk.Label с ОДНОЙ картинкой (PhotoImage),
# в которую каждый кадр просто копируются новые пиксели (paste). Рисуем сразу в размере экрана.
def bg_of(widget):
    """Какого цвета фон под элементом (поднимаемся по родителям, пока не найдём непрозрачный)."""
    w = widget
    while w is not None:
        try:
            c = w.cget("fg_color")
        except Exception:
            c = None
        if c and c != "transparent":
            return c[1] if isinstance(c, (tuple, list)) else c
        w = getattr(w, "master", None)
    return BG


def dpi_scale(widget):
    """Масштаб экрана Windows (100% → 1.0, 125% → 1.25 и т.д.)."""
    try:
        return ctk.ScalingTracker.get_widget_scaling(widget)
    except Exception:
        return 1.0


# ================== Кнопка настроек ==================
class GearButton(tk.Label):
    """Кнопка настроек — контурная шестерёнка, нарисованная через Pillow (символ «⚙» из шрифта
    Windows мелкий и мутный). Обычно серая; при наведении светлеет и плавно поворачивается,
    в настройках — синяя. Рисуем сразу в размере экрана и обновляем одну картинку (paste)."""
    SS = 4                                   # рисуем в 4 раза крупнее и уменьшаем — края гладкие
    TEETH = 8

    def __init__(self, parent, command, size=34):
        self.bg = bg_of(parent)
        self.px = round(size * dpi_scale(parent))          # настоящие пиксели
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (self.px, self.px), self.bg))
        super().__init__(parent, image=self.photo, bd=0, highlightthickness=0, bg=self.bg, cursor="hand2")
        self.command = command
        self.hover = self.active = False
        self.angle = 0.0                                    # поворот сейчас (градусы)
        self.color = hex2rgb(MUTED)                         # цвет сейчас
        self.drawn = None                                   # что нарисовано (чтобы не рисовать зря)
        self.bind("<Enter>", lambda e: setattr(self, "hover", True))
        self.bind("<Leave>", lambda e: setattr(self, "hover", False))
        self.bind("<Button-1>", lambda e: self.command())
        self.draw()

    def set_active(self, on):
        self.active = on

    def step(self, dt):
        """Каждый кадр: цвет и поворот чуть ближе к нужным; перерисовываем, только если что-то сдвинулось."""
        target_color = hex2rgb(ACCENT if self.active else TEXT if self.hover else MUTED)
        target_angle = 45.0 if (self.hover or self.active) else 0.0   # 45° — ровно пол-зубца
        k = ease(0.2, dt)
        self.color = [c + (t - c) * k for c, t in zip(self.color, target_color)]
        self.angle += (target_angle - self.angle) * k
        if abs(self.angle - target_angle) < 0.3:
            self.angle = target_angle
        key = (tuple(round(c) for c in self.color), round(self.angle, 1))
        if key != self.drawn:
            self.drawn = key
            self.draw()

    def draw(self):
        S = self.px * self.SS
        img = Image.new("RGB", (S, S), self.bg)
        d = ImageDraw.Draw(img)
        c = S / 2
        r_out, r_in, hole = S * 0.30, S * 0.22, S * 0.09   # зубцы, «тело», дырка в центре
        pts = []
        for i in range(self.TEETH):                           # контур: у каждого зубца 4 точки-трапеция
            a = math.radians(i * 360 / self.TEETH + self.angle)
            for r, da in ((r_in, -17), (r_out, -10), (r_out, 10), (r_in, 17)):
                t = a + math.radians(da)
                pts.append((c + r * math.cos(t), c + r * math.sin(t)))
        w = max(1, round(S * 0.055))
        color = rgb2hex(self.color)
        d.line(pts + pts[:2], fill=color, width=w, joint="curve")      # +2 точки — чтобы угол замкнулся гладко
        d.ellipse([c - hole, c - hole, c + hole, c + hole], outline=color, width=w)
        self.photo.paste(img.resize((self.px, self.px), Image.LANCZOS))


# ================== Название со свечением ==================
class Title(tk.Label):
    """«skywalk» на размытом свечении. Свечение плавно разгорается/гаснет, «sky» переливается.
    Рисуем сразу в размере экрана и обновляем одну и ту же картинку (paste) — это быстро."""
    LEVELS = 24                              # сколько заранее готовых яркостей свечения
    MIN_P, MAX_P = 0.08, 0.85                # самая тусклая и самая яркая сила свечения

    def __init__(self, parent, w=440, h=150):
        k = dpi_scale(parent)
        self.pw, self.ph = round(w * k), round(h * k)          # размер в настоящих пикселях экрана
        self.glows = [self._glow(self.MIN_P + (self.MAX_P - self.MIN_P) * i / (self.LEVELS - 1))
                      for i in range(self.LEVELS)]
        self.font = load_font(round(38 * k), bold=True)
        self.parts = ["s", "k", "y", "walk"]   # каждая буква «sky» отдельно — чтобы шла волна
        self.shine = 3                         # первые 3 части переливаются, остальное белое
        widths = [self.font.getlength(p) for p in self.parts]
        self.xs, x = [], (self.pw - sum(widths)) / 2
        for wd in widths:
            self.xs.append(x)
            x += wd
        top, bottom = self.font.getbbox("skywalk", anchor="ls")[1::2]
        self.baseline = self.ph / 2 - (top + bottom) / 2      # чтобы надпись стояла ровно по центру
        self.photo = ImageTk.PhotoImage(self.glows[0])
        super().__init__(parent, image=self.photo, bd=0, highlightthickness=0, bg=BG)

    _mask = None                             # форма свечения (0..255) — считается один раз на всю программу

    def _glow(self, strength):
        """Свечение заданной силы. Форму пятна считаем по пикселям ОДИН раз (маска), а каждую яркость
        получаем из неё готовыми функциями Pillow. Раньше все 24 яркости считались по пикселям в Python —
        это добавляло ~0,5 с к запуску программы."""
        sw, sh = 160, 60                     # считаем мелко, потом растягиваем — выходит мягко
        if Title._mask is None:
            mask = Image.new("L", (sw, sh), 0)
            px = mask.load()
            for y in range(sh):
                for x in range(sw):
                    d = math.hypot((x - sw / 2) / (sw * 0.42), (y - sh / 2) / (sh * 0.5))
                    px[x, y] = round(max(0.0, 1 - d) ** 2 * 255)
            Title._mask = mask
        alpha = Title._mask.point(lambda v: int(v * strength))
        img = Image.composite(Image.new("RGB", (sw, sh), GLOW), Image.new("RGB", (sw, sh), BG), alpha)
        return img.resize((self.pw, self.ph), Image.BICUBIC)

    def draw(self, strength, s_colors):
        """strength — сила свечения, s_colors — цвета букв «s», «k», «y»."""
        i = round((strength - self.MIN_P) / (self.MAX_P - self.MIN_P) * (self.LEVELS - 1))
        img = self.glows[max(0, min(self.LEVELS - 1, i))].copy()
        d = ImageDraw.Draw(img)
        colors = list(s_colors) + [TEXT] * (len(self.parts) - self.shine)
        for part, x, color in zip(self.parts, self.xs, colors):
            d.text((x, self.baseline), part, font=self.font, fill=color, anchor="ls")
        self.photo.paste(img)                # те же пиксели в ту же картинку — без создания новой


# ================== Слежка за картами Cybershoke ==================
# Список серверов берём у Steam (официальный Steam Web API), пинг меряем сами.
# Но Steam знает не все сервера Cybershoke (примерно каждого седьмого KZ-сервера в его списке нет),
# поэтому сервера, которые мы уже знаем по адресу, дополнительно спрашиваем напрямую (A2S_INFO).
STEAM_URL = "https://api.steampowered.com/IGameServersService/GetServerList/v1/"
STEAM_FILTER = "\\appid\\730\\name_match\\*KZ #*CYBERSHOKE*"  # сразу только KZ-сервера Cybershoke
KEY_PAGE = "https://steamcommunity.com/dev/apikey"
A2S_INFO = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"       # стандартный запрос «кто ты?» к серверу
WATCH_EVERY = 45                                               # раз в сколько секунд проверять
MAX_WATCH = 6                                                  # сколько карт можно ждать одновременно
REGION_COLS = 5                                                # регионов у Cybershoke 14 — 3 ряда


def map_core(name):
    """Название карты без лишнего: «workshop/3461550188/KZ_Haste.vpk» → «kz_haste»."""
    m = (name or "").strip().lower().replace("\\", "/").split("/")[-1]
    return m[:-4] if m.endswith(".vpk") else m


# если в названии сервера нет региона в [скобках] — определяем страну по городу дата-центра
CITY_REGION = {
    "falkenstein": "DE", "frankfurt": "DE", "nuremberg": "DE", "berlin": "DE",
    "helsinki": "FI", "moscow": "RU", "saint petersburg": "RU", "warsaw": "PL",
    "kyiv": "UA", "kiev": "UA", "amsterdam": "NL", "stockholm": "SE", "london": "GB",
    "paris": "FR", "copenhagen": "DK", "vilnius": "LT", "luxembourg": "LU",
    "almaty": "KZ", "astana": "KZ", "tbilisi": "GE", "ulaanbaatar": "MN",
    "new york": "US", "chicago": "US", "dallas": "US", "los angeles": "US", "miami": "US",
}


def region_of(name):
    m = re.search(r"\[([A-Za-z]{2,4})\]", name)
    if m:
        return m.group(1).upper()
    low = name.lower()
    for city, code in CITY_REGION.items():
        if city in low:
            return code
    return "?"


# Steam скрывает некоторые машины Cybershoke целиком: ни одного сервера с них нет в его списке.
# Узнать такую машину можно только по адресу с сайта cybershoke.net/cs2/servers/kz (ссылки «+connect IP:порт»).
# Все сервера на ней программа находит сама: спрашивает у Steam «какие сервера на этом IP»
# (GetServersAtAddress — он их знает, хоть и не показывает в общем списке) и опрашивает каждый напрямую.
HIDDEN_HOSTS = ["135.181.58.32", "168.119.196.20", "185.175.46.248", "37.9.15.242", "5.9.66.207",
                "51.68.155.95", "88.99.11.135", "95.191.129.46"]
# Уже найденные скрытые KZ-сервера (01.10.2026) — чтобы были видны сразу, ещё до первого поиска
SEED_SERVERS = [
    "135.181.58.32:27034",
    "168.119.196.20:27016", "168.119.196.20:27023", "168.119.196.20:27024", "168.119.196.20:27049",
    "168.119.196.20:27052", "168.119.196.20:27059", "185.175.46.248:27031", "37.9.15.242:27039",
    "5.9.66.207:27062", "5.9.66.207:27064", "5.9.66.207:27065", "5.9.66.207:28066",
    "88.99.11.135:27030", "88.99.11.135:28028", "88.99.11.135:28029", "95.191.129.46:28027",
]
DISCOVER_EVERY = 6 * 3600                                      # поиск скрытых серверов — раз в 6 часов


def is_kz_server(name):
    """KZ-сервер Cybershoke? «CS2 KZ #7 [DE] — CYBERSHOKE.NET» — да.
    «CS2 DM #410 [KZ] — CYBERSHOKE.NET» — нет: тут KZ — это регион (Казахстан), а не режим."""
    return "CYBERSHOKE" in name.upper() and re.search(r"(?<!\[)\bKZ\b(?!\])", name, re.I) is not None


def make_server(addr, name, raw_map, players, max_players):
    num = re.search(r"#(\d+)", name)
    return {
        "addr": addr, "name": name, "map": map_core(raw_map),
        "raw_map": raw_map,                       # как прислал сервер — для журнала
        "players": players, "max": max_players,
        "region": region_of(name),
        "num": num.group(1) if num else "?",
    }


def parse_servers(data):
    """Из ответа Steam оставляем только KZ-сервера Cybershoke.
    Пример названия: «CS2 KZ #53 [DE] — CYBERSHOKE.NET (TIER 1-10)» → номер 53, регион DE."""
    return [make_server(s.get("addr", ""), s.get("name", ""), s.get("map", ""),
                        s.get("players", 0), s.get("max_players", 0))
            for s in data.get("response", {}).get("servers", []) or [] if is_kz_server(s.get("name", ""))]


def parse_a2s(data):
    """Разобрать ответ A2S_INFO: (название, карта, игроки, макс) или None."""
    try:
        if data[4:5] != b"I":
            return None
        fields, rest = [], data[6:]               # 4 байта FF, тип «I», версия протокола
        for _ in range(4):                        # название, карта, папка, игра — строки до нулевого байта
            i = rest.index(b"\x00")
            fields.append(rest[:i].decode("utf-8", "replace"))
            rest = rest[i + 1:]
        return fields[0], fields[1], rest[2], rest[3]   # после 2 байт id игры — игроки и максимум
    except (ValueError, IndexError):
        return None


def a2s_many(addrs, rate=40, wait=1.5):
    """Спросить много серверов «кто ты?» (A2S_INFO) — БЕРЕЖНО к домашней сети.
    Раньше каждый сервер спрашивался своим соединением и все сразу — сотни соединений за секунду.
    Домашний роутер от такого «захлёбывается», и на пару минут пропадает интернет
    (поэтому Steam в своём браузере серверов тоже ограничивает частоту).
    Теперь: ОДНО соединение на все запросы и не больше rate запросов в секунду.
    Ответ: {адрес: (название, карта, игроки, макс, пинг в мс)} — только для ответивших."""
    import select
    targets = []
    for a in addrs:
        try:
            host, port = a.rsplit(":", 1)
            targets.append((host, int(port)))
        except ValueError:
            pass
    if not targets:
        return {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sent, first, results = {}, {}, {}
    i, gap = 0, 1.0 / rate
    next_send = time.perf_counter()
    deadline = None
    try:
        while True:
            now = time.perf_counter()
            if i < len(targets) and now >= next_send:        # следующий запрос — строго по расписанию
                try:
                    sock.sendto(A2S_INFO, targets[i])
                    sent[targets[i]] = now
                except OSError:
                    pass
                i += 1
                next_send += gap
                if i == len(targets):
                    deadline = now + wait                     # всё отправили — ждём последние ответы
            if deadline is not None and now >= deadline:
                break
            until = next_send if i < len(targets) else deadline
            ready, _, _ = select.select([sock], [], [], max(0.0, min(until - time.perf_counter(), 0.05)))
            while ready:
                try:
                    data, src = sock.recvfrom(4096)
                except BlockingIOError:
                    break
                except OSError:                               # Windows: «порт недоступен» от прошлого запроса
                    continue
                t = time.perf_counter()
                if src not in sent:
                    continue
                first.setdefault(src, t)                      # пинг — по первому ответу (даже если это «вызов»)
                if data[4:5] == b"A":                         # «вызов» (challenge): повторяем запрос с ним
                    try:
                        sock.sendto(A2S_INFO + data[5:9], src)
                    except OSError:
                        pass
                    continue
                info = parse_a2s(data)
                if info:
                    ms = round((first[src] - sent[src]) * 1000)
                    results[f"{src[0]}:{src[1]}"] = info + (ms,)
    finally:
        sock.close()
    return results


def watch_targets():
    """Карты, которые ждём, в виде «ядра» имени: {"kz_grotto", "kz_haste"}."""
    return {map_core(m) for m in prefs["watch_maps"] if m.strip()}


def tier_of(name):
    """Тир карты на Cybershoke (1–9) или None, если карты нет в списке."""
    return CYBERSHOKE_TIERS.get(map_core(name))


class MapWatcher:
    """Скачивает список серверов и меряет пинг в фоновом потоке (окно при этом не зависает)."""

    def __init__(self):
        self.servers = []          # KZ-сервера Cybershoke
        self.ping = {}             # пинг по адресу сервера
        self.region_ping = {}      # лучший пинг по региону
        self.error = None          # None / "nokey" / "badkey" / "net"
        self.checked = 0.0         # когда проверяли последний раз
        self.busy = False
        self.version = 0           # растёт после каждой проверки — окно так узнаёт, что пора обновиться
        self.in_steam = set()      # адреса, которые прислал Steam в последний раз
        self.discovered = 0.0      # когда последний раз искали скрытые сервера
        self.discovering = False
        self.lock = threading.Lock()   # список адресов меняют два потока — проверка и поиск
        self.silent = {}           # сервера, которые не ответили: {адрес: когда спросить снова}

    def refresh(self):
        if not self.busy:
            self.busy = True
            threading.Thread(target=self._run, daemon=True).start()

    def _discover(self):
        """Поиск скрытых KZ-серверов (в фоне, ~30 сек). На известных «скрытых» машинах спрашиваем у Steam
        все сервера CS2 и каждый опрашиваем напрямую: какие из них KZ Cybershoke. Найденные — запоминаем."""
        try:
            hosts = set(HIDDEN_HOSTS) | {a.rsplit(":", 1)[0] for a in prefs["kz_servers"] if a not in self.in_steam}
            cand = set()
            for ip in sorted(hosts):                       # по одному, с паузой — чтобы Steam не отказал
                try:
                    url = "https://api.steampowered.com/ISteamApps/GetServersAtAddress/v1/?addr=" + ip
                    req = urllib.request.Request(url, headers={"User-Agent": "skywalk"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    cand.update(f"{ip}:{x['gameport']}" for x in data.get("response", {}).get("servers", [])
                                if x.get("appid") == 730)
                except Exception as e:
                    log(f"поиск серверов {ip}: {e!r}")
                time.sleep(0.3)
            cand -= self.in_steam                           # эти и так приходят от Steam
            cand = sorted(cand)
            replies = a2s_many(cand)
            found = {a for a, ans in replies.items() if is_kz_server(ans[0])}
            with self.lock:
                prefs["kz_servers"] = sorted(set(prefs["kz_servers"]) | found)[:1000]
            log(f"поиск скрытых серверов: машин {len(hosts)}, адресов {len(cand)}, KZ {len(found)}")
            self.discovered = time.time()
        except Exception as e:
            log(f"поиск скрытых серверов: {e!r}")
            self.discovered = time.time() - DISCOVER_EVERY + 600   # ошибка — попробуем через 10 минут
        finally:
            self.discovering = False

    def _run(self):
        try:
            key = prefs["steam_key"].strip()
            if not key:
                self.error = "nokey"
                return
            query = urllib.parse.urlencode({"key": key, "filter": STEAM_FILTER, "limit": 5000})
            req = urllib.request.Request(STEAM_URL + "?" + query, headers={"User-Agent": "skywalk"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            servers = parse_servers(raw)
            total = len(raw.get("response", {}).get("servers", []) or [])

            # Steam отдаёт карту с опозданием (на Cybershoke карты часто меняются), а некоторых серверов
            # не знает вовсе. Поэтому КАЖДЫЙ сервер спрашиваем напрямую: ответ — живая карта, игроки и пинг.
            # Кто не отвечает (с Украины, например, RU-сервера недоступны) — оставляем данные Steam
            # и следующие 5 минут не спрашиваем, чтобы проверка не ждала зря.
            from_steam = {s["addr"]: s for s in servers}
            self.in_steam = set(from_steam)
            known = set(prefs["kz_servers"]) | set(SEED_SERVERS)
            now = time.time()
            ask = sorted(a for a in set(from_steam) | known if self.silent.get(a, 0) <= now)
            replies = a2s_many(ask)
            live, ping, gone = {}, {}, set()
            for addr in ask:
                ans = replies.get(addr)
                if ans is None:
                    self.silent[addr] = now + 300  # молчит — спросим через 5 минут
                    continue
                self.silent.pop(addr, None)
                name, raw_map, players, max_players, ms = ans
                if not is_kz_server(name):
                    gone.add(addr)                # на этом адресе уже не KZ Cybershoke — забываем
                    continue
                live[addr] = make_server(addr, name, raw_map, players, max_players)
                ping[addr] = ms
            stale = [s for a, s in from_steam.items() if a not in live and a not in gone]   # не ответили
            servers = list(live.values()) + stale
            with self.lock:                       # запоминаем все адреса (поиск мог добавить новые — не теряем)
                prefs["kz_servers"] = sorted((set(prefs["kz_servers"]) | known | set(from_steam)) - gone)[:1000]
            log(f"напрямую: спросили {len(ask)}, ответили kz {len(live)}; из steam без ответа {len(stale)}")

            targets = watch_targets()
            for want in targets:
                similar = [f"#{s['num']} [{s['region']}] {s['raw_map']}" for s in servers if want in s["map"]]
                log(f"«{want}»: " + (", ".join(similar) or "нет"))
            log(f"steam: kz {total}, всего {len(servers)}")

            by_region = {}
            for s in servers:
                by_region.setdefault(s["region"], []).append(s)
            region_ping = {}
            for region, lst in by_region.items():
                vals = [ping[s["addr"]] for s in lst if ping.get(s["addr"]) is not None]
                region_ping[region] = min(vals) if vals else None

            self.servers, self.ping, self.region_ping, self.error = servers, ping, region_ping, None
            # поиск скрытых серверов — только ПОСЛЕ проверки: тогда уже известно, кого прислал Steam,
            # и обходить нужно лишь «скрытые» машины (иначе — все 70 машин и пара минут лишней работы)
            if time.time() - self.discovered >= DISCOVER_EVERY and not self.discovering:
                self.discovering = True
                threading.Thread(target=self._discover, daemon=True).start()
        except urllib.error.HTTPError as e:
            self.error = "badkey" if e.code in (401, 403) else "net"
            log(f"steam: HTTP {e.code}")
        except Exception as e:
            self.error = "net"
            log(f"steam: {e!r}")
        finally:
            self.checked = time.time()
            self.busy = False
            self.version += 1

    def regions(self):
        return sorted({s["region"] for s in self.servers})

    def maps(self):
        return sorted({s["map"] for s in self.servers if s["map"]})

    def matches(self):
        """Сервера, где сейчас нужная карта (в выбранных регионах), — сначала с лучшим пингом."""
        return self._sorted([s for s in self.with_map() if self.region_ok(s)])

    def elsewhere(self):
        """Сервера с нужной картой, но в регионах, которые НЕ выбраны."""
        return self._sorted([s for s in self.with_map() if not self.region_ok(s)])

    def with_map(self):
        targets = watch_targets()
        return [s for s in self.servers if s["map"] in targets]

    @staticmethod
    def region_ok(s):
        regions = set(prefs["watch_regions"])
        return not regions or s["region"] in regions

    def _sorted(self, lst):
        return sorted(lst, key=lambda s: (self.ping.get(s["addr"]) is None, self.ping.get(s["addr"]) or 0))


watcher = MapWatcher()


def suggest_maps(query, maps, live, limit=6):
    """Поиск карт по нескольким буквам.
    Сначала те, где название начинается с этих букв (приставку kz_/bkz_ не считаем: «le» → kz_leto),
    потом те, где буквы просто встречаются. Внутри — сначала карты, которые сейчас стоят на серверах."""
    q = query.strip().lower()
    if not q:
        return []

    def core(m):                             # kz_leto → leto
        return m.split("_", 1)[1].lower() if "_" in m else m.lower()

    starts = [m for m in maps if core(m).startswith(q) or m.lower().startswith(q)]
    rest = [m for m in maps if q in m.lower() and m not in starts]
    order = lambda m: (-live.get(m, 0), m)
    return (sorted(starts, key=order) + sorted(rest, key=order))[:limit]


# Все KZ-карты Cybershoke и их тиры (1–9) — со страницы рекордов
# cybershoke.net/cs2/leaderboard/kz/maps, 01.10.2026. Тир на сайте — картинка tiers/<номер>.svg.
# Вшиты в программу: у Cybershoke нет открытого API. Новые карты добавятся сами — с серверов
# и из базы CS2KZ, только без тира (пока не обновим этот список).
CYBERSHOKE_TIERS = {
    "bkz_evanstep": 4, "kz_16pillars": 2, "kz_2fast": 3, "kz_adv_cursedjourney": 4,
    "kz_aether_fix": 5, "kz_after_agitation_v2": 4, "kz_afterlife": 6, "kz_alfie": 5,
    "kz_alice_fix": 4, "kz_alien_city": 6, "kz_alouette_fix": 3, "kz_amber_od": 3, "kz_ambition": 8,
    "kz_angina_final": 7, "kz_angina_x": 8, "kz_antharas": 5, "kz_antimony": 4,
    "kz_bhop_apricity": 6, "kz_bhop_koki_niwa": 6, "kz_bhop_slide": 6, "kz_bigcastle": 3,
    "kz_blackness": 6, "kz_blockhardy2k": 3, "kz_bluekarko": 4, "kz_breezeblocks": 2,
    "kz_brightblock": 1, "kz_celestial": 3, "kz_cf_foliage": 7, "kz_cf_slide": 3, "kz_checkmate": 2,
    "kz_cheese": 2, "kz_cherry": 1, "kz_chloroplast": 6, "kz_colors": 3, "kz_combobreaker": 2,
    "kz_communitybhop": 4, "kz_communityjump3": 4, "kz_custos": 5, "kz_d_decompile": 6,
    "kz_dejavu": 3, "kz_demonhours": 4, "kz_dethroned": 5, "kz_devon": 3, "kz_divert": 6,
    "kz_dojo": 3, "kz_drunkards": 6, "kz_dyd_ladderjumps": 4, "kz_dzy_beyond_v2": 2, "kz_echo": 2,
    "kz_edp445": 2, "kz_ehcivec_final": 2, "kz_epiphany_v2": 4, "kz_fastcombowombo_v2": 3,
    "kz_fastmap": 2, "kz_futureblock": 3, "kz_ggsh": 6, "kz_gigablock_cs2": 2, "kz_greebochki": 3,
    "kz_grotto": 2, "kz_haste": 4, "kz_hb_smieszneznaczki": 3, "kz_hex": 3, "kz_highland": 2,
    "kz_hillside": 1, "kz_hitech": 5, "kz_hoist": 5, "kz_igneous": 2, "kz_illusion_gfix": 4,
    "kz_insomnia": 4, "kz_itz_updown": 4, "kz_jakke": 5, "kz_key": 2, "kz_kiwi_cod": 6,
    "kz_kiwi_hym": 7, "kz_kiwi_lars": 4, "kz_kiwibridge": 1, "kz_kiwiconsecration": 7,
    "kz_kiwideath": 7, "kz_kiwiexophoric": 7, "kz_kiwiexultation": 6, "kz_kiwifactory": 1,
    "kz_kiwifrozen": 6, "kz_kiwionerous": 2, "kz_kiwiparadise": 2, "kz_kiwipsychosis": 9,
    "kz_kiwiqualia": 5, "kz_kiwislide": 8, "kz_kiwitech": 5, "kz_kiwiterror": 8, "kz_kiwitown": 2,
    "kz_kiwivision": 2, "kz_kukkojapallokidutus": 6, "kz_kuutio": 3, "kz_kzro_hardhoodoo": 6,
    "kz_kzro_mountainroad": 5, "kz_kzro_syotiles": 2, "kz_labour": 3, "kz_ladderhell": 4,
    "kz_lean": 1, "kz_lego": 2, "kz_leto": 2, "kz_leviathan": 3, "kz_lionheart": 6, "kz_lmn": 1,
    "kz_loftroofs": 3, "kz_longjumps_easy": 3, "kz_longjumps_space": 4, "kz_lumen": 3, "kz_lust": 2,
    "kz_luv_less": 4, "kz_madhatter": 3, "kz_maga": 6, "kz_mandelbrot": 4, "kz_maya": 3,
    "kz_maze": 2, "kz_mescaline_f": 4, "kz_mess": 5, "kz_mjs_katharaxith": 8, "kz_monstrosity": 6,
    "kz_moon_fix": 7, "kz_moorerutan": 2, "kz_mz": 1, "kz_natureblock": 2, "kz_niche": 2,
    "kz_nightcastle": 4, "kz_noir": 2, "kz_obsidian": 2, "kz_okaychamp": 5, "kz_old_depression": 4,
    "kz_oloramasa": 6, "kz_opus": 2, "kz_otakuroom_v3": 3, "kz_outback": 3, "kz_overworld_a2": 2,
    "kz_pendulum": 6, "kz_persona_is_a_furry": 5, "kz_phamous": 2, "kz_psychosomatic": 6,
    "kz_rectangle": 4, "kz_retribution_v2_final": 6, "kz_royalcapital": 2, "kz_sanctum": 3,
    "kz_serfdom": 2, "kz_shift": 3, "kz_sillychicken": 4, "kz_skytower": 3, "kz_slide_agave": 8,
    "kz_slide_arid": 8, "kz_slide_deee": 6, "kz_slide_era": 5, "kz_slide_isnt_kz": 5,
    "kz_slidebober": 6, "kz_slowerrun": 7, "kz_slowrun": 6, "kz_sp1_behold": 3,
    "kz_sp1_bloodyljs_v2": 6, "kz_sp1_strafechampion": 7, "kz_spacemario_h": 6, "kz_spire": 2,
    "kz_spiritblockv2": 2, "kz_stacy_xt": 9, "kz_summercliff2": 2, "kz_sunstone": 2,
    "kz_sxb2_isbhop": 5, "kz_sxb2_onlyslide": 4, "kz_sxb2_remake": 4, "kz_topsecret": 4,
    "kz_unmake": 7, "kz_valley": 2, "kz_variety_fix": 4, "kz_variety_x": 9, "kz_victoria": 1,
    "kz_village": 3, "kz_wasabi": 2, "kz_wetlands": 2, "kz_why": 4, "kz_woodstock_v2": 1,
    "kz_zoomer_fix": 4, "kzpro_only_up_extended": 3, "skz_bananaysoda_2": 5,
}
CYBERSHOKE_MAPS = sorted(CYBERSHOKE_TIERS)


CS2KZ_MAPS = "https://api.cs2kz.org/maps"      # открытая база всех KZ-карт CS2 (без ключа)


def fetch_kz_maps():
    """Скачиваем названия всех KZ-карт из базы CS2KZ (страницами по 1000). Работает в фоновом потоке."""
    names, offset = set(), 0
    try:
        for _ in range(10):                                   # не больше 10 страниц — на всякий случай
            url = CS2KZ_MAPS + "?" + urllib.parse.urlencode({"limit": 1000, "offset": offset})
            req = urllib.request.Request(url, headers={"User-Agent": "skywalk"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            values = data.get("values", [])
            names.update(m["name"] for m in values if isinstance(m, dict) and m.get("name"))
            offset += len(values)
            if not values or offset >= data.get("total", 0):
                break
        if names:
            fetch_kz_maps.result = names                      # окно заберёт в главном цикле
            log(f"база CS2KZ: {len(names)} карт")
    except Exception as e:
        log(f"база CS2KZ: {e!r}")


fetch_kz_maps.result = None


def connect_to(addr):
    """Подключиться к серверу через Steam (откроет CS2 и зайдёт на сервер)."""
    try:
        os.startfile(f"steam://connect/{addr}")
    except Exception:
        webbrowser.open(f"steam://connect/{addr}")


# ================== Трей ==================
def tray_image():
    """Иконка для трея: наша icon.ico, а если её нет — синий кружок."""
    try:
        return Image.open(resource("icon.ico"))
    except Exception:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([8, 8, 56, 56], fill=ACCENT)
        return img


# ================== Оверлей поверх игры ==================
# Плашка рисуется картинкой с прозрачностью для КАЖДОГО пикселя (UpdateLayeredWindow).
# Так края скруглений и текст гладкие. Раньше окно «вырезалось» по одному цвету — отсюда была «лесенка».
gdi32 = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


# точные типы — чтобы 64-битные «ручки» Windows не обрезались
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
                                       wintypes.HDC, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def premultiply(img):
    """Windows хочет «предумноженные» цвета: цвет × прозрачность, байты в порядке BGRA."""
    r, g, b, a = img.split()
    return Image.merge("RGBA", [ImageChops.multiply(c, a) for c in (r, g, b)] + [a]).tobytes("raw", "BGRA")


def show_layered(hwnd, img, alpha, data=None):
    """Показать RGBA-картинку в окне с прозрачностью для каждого пикселя.
    alpha (0..1) — общая прозрачность плашки (для плавного появления).
    data — уже готовые байты из premultiply(img), чтобы не считать их каждый кадр."""
    w, h = img.size
    if data is None:
        data = premultiply(img)
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    try:
        if not bmp or not bits.value:
            return
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(mem, bmp)
        size, src = wintypes.SIZE(w, h), wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(alpha * 255))), 1)   # AC_SRC_OVER, AC_SRC_ALPHA
        user32.UpdateLayeredWindow(hwnd, screen, None, ctypes.byref(size), mem, ctypes.byref(src),
                                   0, ctypes.byref(blend), 2)                  # ULW_ALPHA
        gdi32.SelectObject(mem, old)
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def set_float_style(hwnd, click_through_on=True):
    """Окно-плашка: «слоистое», не в панели задач и Alt+Tab, не забирает фокус.
    click_through_on=True — клики проходят сквозь; False — плашку можно хватать мышкой."""
    GWL_EXSTYLE, WS_EX_TRANSPARENT = -20, 0x20
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= 0x80000 | 0x80 | 0x08000000                 # LAYERED | TOOLWINDOW | NOACTIVATE
    style = style | WS_EX_TRANSPARENT if click_through_on else style & ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


class Floating(tk.Toplevel):
    """Плашка поверх всех окон: «● skywalk · on». Гладкие края, текст со сглаживанием."""
    SS = 3                                       # рисуем в 3 раза крупнее и уменьшаем — края гладкие

    def __init__(self, master, pos_key):
        super().__init__(master, bg=KEYCAP)
        self.withdraw()                          # покажем, когда будет готова картинка
        self.overrideredirect(True)              # без рамки и заголовка
        self.attributes("-topmost", True)
        self.pos_key = pos_key
        self.placed_at = None
        self.alpha = 0.0
        self.editing = False
        self.hwnd = None
        self.k = dpi_scale(master)
        self.font = load_font(round(13 * self.k * self.SS), bold="semi")
        self.state_ = ("", MUTED, MUTED)          # что нарисовано: текст, цвет точки, цвет текста
        self.img = None
        self.data = None                         # байты картинки, готовые для Windows
        self.cache_key = None
        self.cache = {}                          # запас готовых картинок: {что нарисовано: (картинка, байты)}
        self.after(60, self.setup)

    def setup(self):
        try:
            self.update_idletasks()
            self.hwnd = int(self.wm_frame(), 16)  # настоящее окно Windows, в котором живёт плашка
            set_float_style(self.hwnd, True)
            self.render(force=True)
            self.deiconify()
            self.move()
        except Exception:
            import traceback
            log("оверлей: " + traceback.format_exc())

    def render(self, force=False):
        """Нарисовать плашку (только если что-то поменялось) и показать её."""
        text, dot, color = self.state_
        key = (text, dot, color, self.editing)
        if key != self.cache_key or force:
            self.cache_key = key
            if key in self.cache and not force:    # такая картинка уже рисовалась — берём готовую
                self.img, self.data = self.cache[key]
                self.show()
                return
            S, k = self.SS, self.k * self.SS
            pad_l, dot_d, gap, pad_r, H = 14 * k, 8 * k, 8 * k, 16 * k, 36 * k
            W = pad_l + dot_d + gap + self.font.getlength(text) + pad_r
            img = Image.new("RGBA", (round(W), round(H)), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            border = ACCENT if self.editing else HOVER
            d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=H / 2,
                                fill=tuple(hex2rgb(KEYCAP)) + (235,), outline=tuple(hex2rgb(border)) + (255,),
                                width=max(1, round(k)))
            cy = H / 2
            d.ellipse([pad_l, cy - dot_d / 2, pad_l + dot_d, cy + dot_d / 2], fill=dot)
            d.text((pad_l + dot_d + gap, cy), text, font=self.font, fill=color, anchor="lm")
            self.img = img.resize((max(1, round(W / S)), max(1, round(H / S))), Image.LANCZOS)
            self.data = premultiply(self.img)
            if len(self.cache) > 80:               # запас не должен расти бесконечно
                self.cache.clear()
            self.cache[key] = (self.img, self.data)
        self.show()

    def show(self):
        if self.hwnd and self.img is not None:
            if self.img.size != getattr(self, "shown_size", None):
                self.shown_size = self.img.size
                self.move()                      # размер поменялся (on ↔ off) — ставим заново
            show_layered(self.hwnd, self.img, self.alpha, self.data)

    def move(self):
        """Ставим плашку на её место: своё (перетащили мышкой) или по умолчанию."""
        if self.img is None:
            return
        w, h = self.img.size
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        xy = prefs.get("overlay_xy")
        if xy:                                   # своё место — в долях экрана
            x, y = int((sw - w) * xy[0]), int((sh - h) * xy[1])
        else:                                    # место по умолчанию
            fx, fy = SCREEN_POS[prefs[self.pos_key]]
            top, side, bottom = 24, 24, 64       # снизу побольше — там панель задач
            x = int(side + (sw - w - 2 * side) * fx)
            y = int(top + (sh - h - top - bottom) * fy)
            if fy == 0.5:                        # средняя строка — чуть выше середины,
                y -= int(sh * 0.08)              # чтобы не упираться в KZ-меню сервера
        self.placed_at = self.place_key()
        self.tk.call("wm", "geometry", self._w, f"{w}x{h}+{x}+{y}")   # напрямую, без масштабирования

    def place_key(self):
        xy = prefs.get("overlay_xy")
        return tuple(xy) if xy else prefs[self.pos_key]

    def set(self, text, dot_color, text_color):
        self.state_ = (text, dot_color, text_color)
        if getattr(self, "dragging", False):
            return                                # тащат мышкой — место не трогаем
        if self.placed_at != self.place_key():    # место поменялось (сброс и т.п.)
            self.move()
        if self.alpha > 0:
            self.render()


class Overlay(Floating):
    """Постоянный значок статуса. Виден только когда активно окно CS2 (плавно появляется/прячется)."""

    def __init__(self, master):
        super().__init__(master, "overlay_pos")
        self.dragging = False
        self.bind("<ButtonPress-1>", self.drag_start)
        self.bind("<B1-Motion>", self.drag_move)
        self.bind("<ButtonRelease-1>", self.drag_end)
        self.bind("<Double-Button-1>", lambda e: self.master.finish_move())

    def fade_to(self, target, dt):
        new = self.alpha + (target - self.alpha) * ease(0.2, dt)
        if abs(new - target) < 0.01:
            new = target
        if new != self.alpha:
            self.alpha = new
            self.render()

    def set_edit(self, on):
        """Включить/выключить режим перетаскивания."""
        self.editing = on
        if self.hwnd:
            set_float_style(self.hwnd, not on)   # в режиме перетаскивания плашку можно хватать
        self.configure(cursor="fleur" if on else "")
        self.render()

    def drag_start(self, e):
        if not self.editing:
            return
        self.dragging = True
        self.dx, self.dy = e.x_root - self.winfo_x(), e.y_root - self.winfo_y()

    def drag_move(self, e):
        if not self.dragging:
            return
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, min(sw - w, e.x_root - self.dx))       # не даём утащить за край экрана
        y = max(0, min(sh - h, e.y_root - self.dy))
        self.tk.call("wm", "geometry", self._w, f"+{x}+{y}")

    def drag_end(self, e):
        """Отпустили — запоминаем место в долях экрана (так оно верно и на другом разрешении)."""
        if not self.dragging:
            return
        self.dragging = False
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        fx = self.winfo_x() / max(1, sw - w)
        fy = self.winfo_y() / max(1, sh - h)
        prefs["overlay_xy"] = [round(min(1.0, max(0.0, fx)), 4), round(min(1.0, max(0.0, fy)), 4)]
        self.placed_at = self.place_key()


# ================== Уведомление «карта нашлась» ==================
def render_toast(k, map_name, info, ping, progress, logo=None, tier=None, title=None, hint=None, badge=None):
    """Карточка уведомления (RGBA). k — масштаб экрана, progress — сколько осталось (1 → 0).
    title / hint — надписи сверху (по умолчанию «карта нашлась» / «клик — подключиться»),
    badge — синяя надпись после названия (у рекорда — на сколько побит прошлый)."""
    S = 3                                        # рисуем в 3 раза крупнее — края гладкие
    z = k * S * 0.88                             # вся карточка на ~12% меньше
    W, H = round(365 * z), round(96 * z)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = 18 * z
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=rad, fill=tuple(hex2rgb(KEYCAP)) + (242,),
                        outline=tuple(hex2rgb(HOVER)) + (255,), width=max(1, round(z)))
    # полоска «сколько осталось» внизу карточки (обрезана по скруглению)
    bar = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(bar).rectangle([0, H - 3 * z, W * max(0.0, progress), H],
                                  fill=tuple(hex2rgb(ACCENT)) + (255,))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=rad, fill=255)
    img.alpha_composite(Image.composite(bar, Image.new("RGBA", (W, H), (0, 0, 0, 0)), mask))
    # логотип
    x0, size = 16 * z, round(52 * z)
    if logo is not None:
        lg = logo.convert("RGBA").resize((size, size), Image.LANCZOS)
        img.alpha_composite(lg, (round(x0), round((H - size) / 2)))
    tx = x0 + size + 14 * z
    f_small = load_font(round(11 * z), bold="semi")   # «карта нашлась» и «клик — подключиться» — пожирнее
    f_big = load_font(round(18 * z), bold="semi")
    f_info = load_font(round(12 * z))
    d.text((tx, 18 * z), title or tr("toast_title"), font=f_small, fill=ACCENT, anchor="lm")
    d.text((W - 18 * z, 18 * z), hint or tr("toast_click"), font=f_small, fill=MUTED, anchor="rm")
    d.text((tx, 44 * z), map_name, font=f_big, fill=TEXT, anchor="lm")
    if badge:                                    # «+1.70» — синим, сразу после названия
        d.text((tx + f_big.getlength(map_name) + 8 * z, 45 * z), badge, font=f_small, fill=ACCENT, anchor="lm")
    elif tier:                                   # тир сложности — рядом с названием, приглушённо
        d.text((tx + f_big.getlength(map_name) + 8 * z, 45 * z), f"t{tier}", font=f_small, fill=MUTED, anchor="lm")
    d.text((tx, 70 * z), info, font=f_info, fill=MUTED, anchor="lm")
    if ping:                                     # пинг — цветом акцента, сразу после остального текста
        d.text((tx + f_info.getlength(info + "  "), 70 * z), ping, font=f_info, fill=ACCENT, anchor="lm")
    return img.resize((round(W / S), round(H / S)), Image.LANCZOS)


class MapToast(tk.Toplevel):
    """Уведомление поверх всех окон сверху по центру: выезжает, висит 8 сек, исчезает.
    Клик по нему — подключиться к серверу."""
    SHOW = 8.0                                   # сколько секунд висит

    def __init__(self, master):
        super().__init__(master, bg=KEYCAP)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.k = dpi_scale(master)
        self.hwnd = None
        self.server = None
        self.start = None
        self.title = self.hint = self.badge = None   # своё у карточки рекорда
        self.y_shift = 0                             # насколько ниже обычного места (две карточки сразу)
        try:
            self.logo = Image.open(resource("icon.ico"))
        except Exception:
            self.logo = None
        self.bind("<ButtonPress-1>", self.click)
        self.after(60, self.setup)

    def setup(self):
        try:
            self.update_idletasks()
            self.hwnd = int(self.wm_frame(), 16)
            set_float_style(self.hwnd, False)    # по карточке можно кликнуть (фокус у игры не забирает)
        except Exception:
            import traceback
            log("уведомление: " + traceback.format_exc())

    def show(self, server, ping):
        self.server = server
        self.info = f"#{server['num']} [{server['region']}] · {server['players']}/{server['max']}"
        self.ping = f"{ping} {tr('ms')}" if ping is not None else ""
        self.map_name = server["map"]
        self.tier = tier_of(server["map"])
        first = self.start is None
        self.start = time.perf_counter()
        if first:
            self.deiconify()
            self.step()

    def step(self):
        if self.start is None or not self.winfo_exists():
            return
        t = time.perf_counter() - self.start
        if t >= self.SHOW + 0.35:               # время вышло и исчезли — прячем
            self.hide()
            return
        appear = smooth(min(1.0, t / 0.3))       # появление 0,3 сек
        vanish = smooth(min(1.0, max(0.0, (self.SHOW + 0.35 - t) / 0.35)))   # исчезновение 0,35 сек
        img = render_toast(self.k, self.map_name, self.info, self.ping, 1 - min(1.0, t / self.SHOW),
                           self.logo, self.tier, self.title, self.hint, self.badge)
        x = (self.winfo_screenwidth() - img.width) // 2
        # сверху по центру, но ниже игровых надписей (таймер KZ и т.п.); выезжает сверху
        y = round(self.winfo_screenheight() * 0.11 - (1 - appear) * 24 * self.k) + self.y_shift
        self.tk.call("wm", "geometry", self._w, f"{img.width}x{img.height}+{x}+{y}")
        if self.hwnd:
            show_layered(self.hwnd, img, min(appear, vanish))
        self.after(16, self.step)                 # 60 кадров — карточке больше не нужно

    def click(self, e=None):
        if self.server:
            connect_to(self.server["addr"])
        self.hide()

    def hide(self):
        self.start = None
        self.withdraw()


class RecordToast(MapToast):
    """Уведомление «новый рекорд» — такое же, как «карта нашлась». Клик — открыть программу
    сразу на подробностях этого прыжка."""

    def show_record(self, abbr, dist, prev, rec):
        self.abbr = abbr
        self.title, self.hint = tr("rec_toast"), tr("rec_toast_click")
        self.map_name = f"{abbr.lower()}  {dist:.2f}"
        self.badge = f"+{dist - prev:.2f}" if prev else tr("rec_first")
        self.tier, self.ping = None, ""
        parts = []
        if rec.get("block"):
            parts.append(tr("rec_block", b=f"{rec['block']:.0f}"))
        if "pre" in rec:
            parts.append(f"pre {rec['pre']:.0f}")
        if "sync" in rec:
            parts.append(f"{rec['sync']:.0f}% sync")
        if "strafes" in rec:
            parts.append(f"{rec['strafes']:.0f} {tr('rec_str')}")
        self.info = " · ".join(parts) or JUMP_NAMES.get(abbr, "")
        # «карта нашлась» уже висит — встаём под ней, чтобы не закрыть её
        other = self.master.toast
        self.y_shift = round(100 * self.k) if other.start is not None else 0
        first = self.start is None
        self.start = time.perf_counter()
        if first:
            self.deiconify()
            self.step()

    def click(self, e=None):
        app, abbr = self.master, self.abbr
        self.hide()
        if app.state() in ("withdrawn", "iconic"):
            app.show_tab("records")           # окно спрятано: сначала вкладка (дорисуется, пока окно невидимое),
            app.show_window()                 # потом плавное появление — уже готового окна
        else:
            app.show_window()
            app.show_tab("records")
        app.after(250, app.show_jump, abbr)


# ================== Тренер и прогресс: картинки ==================
COACH_TYPES = ("LJ", "BH", "MBH", "WJ", "LAJ")


# ================== Журнал версий («что нового») ==================
# Новые версии — СВЕРХУ. Пишем коротко и понятно для игрока, строчными, без технических слов.
CHANGELOG = [
    ("demo 8.4", ["окно открывается сразу — из трея, с панели задач и после «свернуть» (было с задержкой до 0,8 с)"],
                 ["the window opens instantly — from the tray, the taskbar and after minimizing"]),
    ("demo 8.3", ["журнал версий — вот он",
                  "всё стало плавнее: убраны подвисания окна раз в секунду (во всех анимациях)",
                  "окно больше не замирает при запуске, пока читается консоль cs2; запуск быстрее",
                  "карточка прыжка появляется целиком и плавно, длинная статистика больше не обрезается",
                  "в «рекордах» виды и типы прыжков переключаются плавно, как вкладки"],
                 ["this changelog",
                  "everything is smoother: the window no longer stutters once a second",
                  "no freeze on start while the cs2 console is read; faster start",
                  "the jump card appears whole and smoothly, long stats are no longer cut off",
                  "views in «records» switch smoothly, like tabs"]),
    ("demo 8.2", ["«66 с -w» и «66 без -w» можно поставить на колесо — присед держится 0,8 с"],
                 ["«66 with -w» and «66 without -w» work on the mouse wheel — duck is held 0.8 s"]),
    ("demo 8.0", ["тренер: разбор прыжка по стрейфам и советы, что исправить",
                  "прогресс: график лучшего прыжка по дням",
                  "сводка сессии: средние цифры и частые ошибки",
                  "на карточке рекорда — карта и сколько осталось до следующего тира"],
                 ["coach: strafe-by-strafe breakdown and tips",
                  "progress: best jump per day chart",
                  "session summary: averages and most common mistakes",
                  "record card shows the map and the distance to the next tier"]),
    ("demo 7.9", ["статистика сессии: сколько прыжков и каких тиров"],
                 ["session stats: how many jumps of each tier"]),
    ("demo 7.8", ["ник берётся из steam сам — поле ника убрано"],
                 ["the nick comes from steam — no nick field anymore"]),
    ("demo 7.5", ["уведомление на экране, когда поставил новый рекорд"],
                 ["on-screen notification for a new record"]),
    ("demo 7.4", ["клик по рекорду — вся статистика прыжка"],
                 ["click a record to see all jump stats"]),
    ("demo 7.2", ["вкладка «рекорды»: лучшие прыжки за всё время из консоли cs2"],
                 ["«records» tab: all-time best jumps from the cs2 console"]),
    ("demo 7.1", ["окно появляется плавно, а не «собирается из кусков»"],
                 ["the window fades in instead of assembling piece by piece"]),
    ("demo 7.0", ["бинды: 66 с -w, 66 без -w, jumpbug, danvari",
                  "клавиши прыжка и приседа программа берёт из настроек cs2"],
                 ["binds: 66 with -w, 66 without -w, jumpbug, danvari",
                  "jump and duck keys are read from cs2 settings"]),
    ("demo 6.8", ["бинд crouch jump и новая кнопка настроек"],
                 ["crouch jump bind and a new settings button"]),
    ("demo 6.7", ["щелчок при входе в cs2 и защита от двойного запуска"],
                 ["click sound when entering cs2, protection from running twice"]),
    ("demo 6.6", ["клик по строке итога — подключиться к серверу"],
                 ["click a result line to connect to the server"]),
    ("demo 6.0", ["слежка видит все kz-сервера cybershoke, даже скрытые в steam",
                  "карта на сервере всегда свежая, проверка не нагружает роутер"],
                 ["map watch sees every cybershoke kz server, even hidden ones",
                  "the server map is always fresh, checks don't overload the router"]),
    ("demo 5.7", ["несколько карт в слежке и тир каждой карты"],
                 ["several maps in the watch list and each map's tier"]),
    ("demo 5.4", ["режим экономии: в игре и в трее программа почти не тратит ресурсы"],
                 ["power saving: almost no load while in game or in the tray"]),
]


def wrap_text(text, fnt, width):
    """Разбить строку на строчки не шире width пикселей (по словам)."""
    lines, cur = [], ""
    for word in text.split():
        test = (cur + " " + word).strip()
        if cur and fnt.getlength(test) > width:
            lines.append(cur)
            cur = word
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def render_changelog(k, width, lang):
    """Весь журнал версий одной высокой картинкой (фон PANEL) — её потом только сдвигаем при прокрутке."""
    pad, gap = round(18 * k), round(4 * k)
    f_ver, f_item = load_font(round(12 * k), "semi"), load_font(round(11 * k))
    line_h, ver_h = round(16 * k), round(22 * k)
    blocks = []                                   # (версия, [(строчка, первая ли в пункте), ...])
    for ver, ru, en in CHANGELOG:
        rows = []
        for item in (ru if lang == "ru" else en):
            for i, line in enumerate(wrap_text(item, f_item, width - 2 * pad - round(14 * k))):
                rows.append((line, i == 0))
        blocks.append((ver, rows))
    height = sum(ver_h + len(rows) * line_h + round(12 * k) for _, rows in blocks) + gap
    img = Image.new("RGB", (width, height), PANEL)
    d = ImageDraw.Draw(img)
    y = gap
    for n, (ver, rows) in enumerate(blocks):
        d.text((pad, y + ver_h / 2), ver, font=f_ver, fill=ACCENT if n == 0 else TEXT, anchor="lm")
        y += ver_h
        for line, first in rows:
            if first:
                d.text((pad + round(2 * k), y + line_h / 2), "•", font=f_item, fill=MUTED, anchor="lm")
            d.text((pad + round(14 * k), y + line_h / 2), line, font=f_item, fill=TEXT if n == 0 else MUTED,
                   anchor="lm")
            y += line_h
        y += round(12 * k)
    return img


class ChangesPanel(tk.Label):
    """Панель «что нового» поверх страницы «о программе». Одна картинка: шапка (название и ×) неподвижна,
    список версий прокручивается колесом мыши — плавно, кадрами из уже готовой картинки (быстро).
    Клик по шапке — закрыть."""

    def __init__(self, app, parent):
        self.app = app
        app.update_idletasks()
        self.k = k = dpi_scale(parent)
        self.w, self.h = max(parent.winfo_width(), 50), max(parent.winfo_height(), 50)
        self.head = round(48 * k)                 # высота шапки
        self.bottom = round(14 * k)               # отступ снизу (там скругление)
        self.content = render_changelog(k, self.w, LANG)
        self.view_h = self.h - self.head - self.bottom
        self.max_off = max(0, self.content.height - self.view_h)
        self.off = self.target = 0.0
        self.anim = False
        # неподвижная основа: скруглённая панель + шапка; маска — чтобы углы оставались круглыми
        S = 2
        big = Image.new("RGB", (self.w * S, self.h * S), BG)
        d = ImageDraw.Draw(big)
        d.rounded_rectangle([0, 0, self.w * S - 1, self.h * S - 1], radius=round(20 * k * S), fill=PANEL,
                            outline=ACCENT_DIM, width=max(1, round(k * S)))
        self.base = big.resize((self.w, self.h), Image.LANCZOS)
        mask = Image.new("L", (self.w * S, self.h * S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([round(2 * k * S), 0, self.w * S - 1 - round(2 * k * S), self.h * S - 1],
                                               radius=round(18 * k * S), fill=255)
        self.mask = mask.resize((self.w, self.h), Image.LANCZOS)
        d = ImageDraw.Draw(self.base)
        f_t, f_x = load_font(round(15 * k), "semi"), load_font(round(16 * k))
        d.text((round(18 * k), self.head / 2 + round(2 * k)), tr("changes"), font=f_t, fill=TEXT, anchor="lm")
        d.text((self.w - round(18 * k), self.head / 2 + round(2 * k)), "×", font=f_x, fill=MUTED, anchor="rm")
        d.text((self.w - round(40 * k), self.head / 2 + round(2 * k)), VERSION, font=load_font(round(10 * k)),
               fill=MUTED, anchor="rm")
        self.img = self.frame()
        self.photo = ImageTk.PhotoImage(self.img)
        super().__init__(parent, image=self.photo, bd=0, highlightthickness=0, bg=BG)
        self.bind("<MouseWheel>", self.wheel)
        self.bind("<Button-1>", self.click)

    def frame(self):
        """Кадр: основа + видимая часть списка (со сдвигом off), углы — по маске."""
        img = self.base.copy()
        o = int(round(self.off))
        part = self.content.crop((0, o, self.w, o + self.view_h))
        img.paste(Image.composite(part, self.base.crop((0, self.head, self.w, self.head + self.view_h)),
                                  self.mask.crop((0, self.head, self.w, self.head + self.view_h))), (0, self.head))
        if o > 0:                                 # список уехал вверх — тонкая линия под шапкой
            ImageDraw.Draw(img).line([round(18 * self.k), self.head - 1, self.w - round(18 * self.k), self.head - 1],
                                     fill=HOVER, width=max(1, round(self.k)))
        return img

    def wheel(self, e):
        self.target = max(0.0, min(self.max_off, self.target - e.delta / 120 * 60 * self.k))
        if not self.anim:
            self.anim = True
            self.last = time.perf_counter()
            self.after(FRAME_MS, self.step)

    def step(self):
        if not self.winfo_exists():
            return
        now = time.perf_counter()
        dt, self.last = min(0.05, now - self.last), now
        self.off += (self.target - self.off) * ease(0.3, dt)
        if abs(self.target - self.off) < 0.5:
            self.off = self.target
        self.img = self.frame()
        self.photo.paste(self.img)
        if self.off != self.target:
            self.after(FRAME_MS, self.step)
        else:
            self.anim = False

    def click(self, e):
        if e.y <= self.head:                      # клик по шапке (там ×) — закрыть
            self.app.close_changes(animate=True)


def render_card(k, w, h, title, when, tokens):
    """Карточка прыжка/сессии одной картинкой (размер w×h — уже в пикселях экрана).
    Раньше она собиралась из ~70 отдельных элементов customtkinter: 130 мс и видно, как «собирается».
    Картинка рисуется за ~10 мс и появляется сразу целиком."""
    S = 2                                         # рисуем вдвое крупнее и уменьшаем — гладкие углы
    z = k * S
    W, H = w * S, h * S
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=round(20 * z), fill=PANEL, outline=ACCENT_DIM,
                        width=max(1, round(z)))
    pad = 18 * z
    f_title, f_x = load_font(round(15 * z), "semi"), load_font(round(16 * z))
    f_lab, f_val = load_font(round(10 * z)), load_font(round(11 * z), "semi")
    head_y = (12 + 13) * z                        # середина строки заголовка
    d.text((pad, head_y), title, font=f_title, fill=TEXT, anchor="lm")
    d.text((W - pad, head_y), "×", font=f_x, fill=MUTED, anchor="rm")
    if when:
        x_w = d.textlength("×", font=f_x)
        d.text((W - pad - x_w - 10 * z, head_y), when, font=f_lab, fill=MUTED, anchor="rm")
    top, row_h, gap = (12 + 26 + 6) * z, 19 * z, 14 * z
    col_w = (W - 2 * pad - gap) / 2
    for i, tok in enumerate(tokens[:24]):
        if not tok:                               # пустая ячейка (выравнивание)
            continue
        value, _, label = tok.partition(" ")     # «83.65% AvgSync» → «83.65%» + «AvgSync»
        if ":" in tok:                            # «JumpDirection: Forwards»
            label, _, value = tok.partition(":")
        x0 = pad + (i % 2) * (col_w + gap)
        y = top + (i // 2) * row_h + row_h / 2
        d.text((x0, y), label.strip(), font=f_lab, fill=MUTED, anchor="lm")
        d.text((x0 + col_w, y), value.strip(), font=f_val, fill=TEXT, anchor="rm")
    return img.resize((w, h), Image.LANCZOS)


def render_strafe_chart(k, strafes, width=352, height=96):
    """Столбики gain по стрейфам: высота — gain, цвет — sync (ярче — лучше). Под столбиком номер, над — gain."""
    S, z = 3, k * 3
    W, H = round(width * z), round(height * z)
    img = Image.new("RGB", (W, H), PANEL)
    d = ImageDraw.Draw(img)
    real = [s for s in strafes if s["air"] >= 3]
    f_small = load_font(round(9 * z))
    base = H - 16 * z
    d.line([0, base, W, base], fill=HOVER, width=max(1, round(z)))
    if real:
        top = max(max(s["gain"] for s in real), 1.0)
        step = W / len(real)
        bw = min(26 * z, step * 0.62)
        for i, s in enumerate(real):
            cx = step * (i + 0.5)
            h = max(2 * z, (base - 16 * z) * max(0.0, s["gain"]) / top)
            color = mix(ACCENT_DIM, ACCENT, (s["sync"] - 60) / 40)
            d.rounded_rectangle([cx - bw / 2, base - h, cx + bw / 2, base], radius=min(4 * z, bw / 2), fill=color)
            d.text((cx, base - h - 2 * z), f"{s['gain']:.1f}", font=f_small, fill=TEXT, anchor="mb")
            d.text((cx, base + 3 * z), str(s["n"]), font=f_small, fill=MUTED, anchor="mt")
    return img.resize((round(width * k), round(height * k)), Image.LANCZOS)


def render_progress_chart(k, days, limits, width=352, height=190):
    """Лучший результат по дням (последние 30) + линии ownage и wrecker."""
    S, z = 3, k * 3
    W, H = round(width * z), round(height * z)
    img = Image.new("RGB", (W, H), PANEL)
    d = ImageDraw.Draw(img)
    f = load_font(round(9 * z))
    f_big = load_font(round(11 * z), bold="semi")
    pts = days[-30:]
    vals = [v for _, v in pts]
    lo = min(vals + ([limits[3]] if limits else [])) - 3
    hi = max(vals + ([limits[5]] if limits else [])) + 2
    left, right, top, bottom = 34 * z, W - 12 * z, 12 * z, H - 20 * z

    def y_of(v):
        return bottom - (v - lo) / max(1e-6, hi - lo) * (bottom - top)

    if limits:                                   # линии тиров: ownage и wrecker (цель)
        for v, name, col in ((limits[4], "own", MUTED), (limits[5], "wreck", ACCENT)):
            y = y_of(v)
            for x in range(int(left), int(right), int(8 * z)):
                d.line([x, y, min(x + 4 * z, right), y], fill=col, width=max(1, round(z)))
            d.text((left - 4 * z, y), f"{v:g}", font=f, fill=col, anchor="rm")
    n = len(pts)
    xs = [left + (right - left) * (i / (n - 1) if n > 1 else 0.5) for i in range(n)]
    coords = [(x, y_of(v)) for x, v in zip(xs, vals)]
    if n > 1:
        d.line(coords, fill=ACCENT, width=max(1, round(2 * z)), joint="curve")
    r = 3 * z
    for x, y in coords:
        d.ellipse([x - r, y - r, x + r, y + r], fill=ACCENT)
    if coords:                                   # последнее значение — подписью
        x, y = coords[-1]
        d.text((x, y - 6 * z), f"{vals[-1]:.1f}", font=f_big, fill=TEXT, anchor="mb" if n == 1 else "rb")
        for i in sorted({0, n - 1}):              # даты: первая и последняя
            date = pts[i][0][8:10] + "." + pts[i][0][5:7]
            d.text((xs[i], H - 4 * z), date, font=f, fill=MUTED, anchor="mb" if n == 1 else ("lb" if i == 0 else "rb"))
    return img.resize((round(width * k), round(height * k)), Image.LANCZOS)


def tip_names():
    return dict(part.split(":", 1) for part in tr("tipname").split("|"))


# ================== Плавная смена вкладок ==================
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]


def grab_widget(w):
    """Снимок элемента — копируем пиксели прямо из его окна Windows (BitBlt).
    Это 1–2 мс. Раньше снимали ЭКРАН (ImageGrab) — 30–470 мс, и раз в секунду (освежение снимка
    страницы) все анимации подвисали. Не вышло — запасной путь через ImageGrab. None — снять нельзя."""
    try:
        ww, hh = w.winfo_width(), w.winfo_height()
        if ww < 10 or hh < 10:
            return None
        hwnd = w.winfo_id()
        hdc = user32.GetDC(hwnd)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, ww, hh)
        old = gdi32.SelectObject(mem, bmp)
        try:
            gdi32.BitBlt(mem, 0, 0, ww, hh, hdc, 0, 0, 0x00CC0020)         # 0x00CC0020 = SRCCOPY
            info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), ww, -hh, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(ww * hh * 4)
            ok = gdi32.GetDIBits(mem, bmp, 0, hh, buf, ctypes.byref(info), 0)
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(hwnd, hdc)
        if ok == hh:
            return Image.frombuffer("RGB", (ww, hh), buf, "raw", "BGRX", 0, 1).copy()
    except Exception:
        pass
    return grab_screen(w)


def grab_screen(w):
    """Запасной снимок — с экрана (медленно). None — если снять нельзя (окно не на главном мониторе и т.п.)."""
    if ImageGrab is None:
        return None
    try:
        x, y, ww, hh = w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()
        if ww < 10 or hh < 10 or x < 0 or y < 0 \
                or x + ww > w.winfo_screenwidth() or y + hh > w.winfo_screenheight():
            return None
        return ImageGrab.grab(bbox=(x, y, x + ww, y + hh)).convert("RGB")
    except Exception:
        return None


def smooth(k):
    """Плавный разгон и торможение: 0 → 1 по S-образной кривой."""
    return k * k * (3 - 2 * k)


# ================== Интерфейс: элементы ==================
class Segmented(tk.Label):
    """Гладкий переключатель (off / on, стили звука, язык, вкладки), нарисованный через Pillow.
    Выбранная «таблетка» плавно переезжает к новому варианту.
    Быстро: рисуем сразу в размере экрана, обновляем одну картинку (paste),
    а готовые кадры анимации запоминаем — повторная анимация вообще ничего не рисует."""
    SS = 3                                   # рисуем в 3 раза крупнее и уменьшаем — края гладкие
    STEPS = 40                               # на сколько кадров делим путь между соседними вариантами

    def __init__(self, parent, values, value, command, seg_w=46, h=30, labels=None, font_size=11):
        self.values, self.command = list(values), command
        self.labels = list(labels) if labels else self.values      # что писать (перевод)
        self.seg_w, self.h = seg_w, h
        self.w = seg_w * len(self.values) + 4
        self.pos = self.target = float(self.values.index(value))
        self.hidden = False                  # True — ничего не выбрано (для вкладок в настройках)
        self.last = None                     # время прошлого кадра анимации
        self.is_switch = self.values == ["off", "on"]
        self.k = dpi_scale(parent)
        self.pw, self.ph = round(self.w * self.k), round(self.h * self.k)   # настоящие пиксели
        self.font = load_font(round(font_size * self.k * self.SS))
        self.bg = bg_of(parent)              # фон под переключателем — рисуем прямо на нём
        self.frames = {}                     # готовые кадры: {(позиция, спрятан): картинка}
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (self.pw, self.ph), self.bg))
        super().__init__(parent, image=self.photo, bd=0, highlightthickness=0, bg=self.bg,
                         cursor="hand2")
        self.redraw()
        self.bind("<Button-1>", self.click)

    def click(self, e):
        width = max(self.winfo_width(), 1)
        i = min(int(e.x / width * len(self.values)), len(self.values) - 1)
        if self.is_switch:                   # у off/on — клик в любом месте переключает
            i = 1 - int(round(self.target))
        if self.hidden:                      # ничего не было выбрано — сразу ставим на место
            self.hidden = False
            self.pos = self.target = float(i)
            self.redraw()
            self.command(self.values[i])
        elif i != int(round(self.target)):
            self.target = float(i)
            self.animate()
            self.command(self.values[i])

    def hide(self):
        self.hidden = True
        self.redraw()

    def animate(self):
        if not self.winfo_exists():          # окно пересобрали (смена языка) — стоп
            return
        now = time.perf_counter()
        dt = min(0.05, now - (self.last or now - 1 / 60))   # первый кадр анимации — как 1/60 сек
        self.last = now
        self.pos += (self.target - self.pos) * ease(0.28, dt)
        if abs(self.target - self.pos) < 0.01:
            self.pos = self.target
        self.redraw()
        if self.pos != self.target:
            self.after(FRAME_MS, self.animate)
        else:
            self.last = None                 # анимация закончилась — следующая начнётся заново

    def redraw(self):
        key = (round(self.pos * self.STEPS), self.hidden)
        img = self.frames.get(key)
        if img is None:                      # такого кадра ещё не было — рисуем и запоминаем
            img = self.render(key[0] / self.STEPS)
            self.frames[key] = img
        self.photo.paste(img)

    def render(self, pos):
        S, k = self.SS, self.k * self.SS
        W, H = self.pw * S, self.ph * S
        img = Image.new("RGB", (W, H), self.bg)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=H // 2, fill=KEYCAP)          # дорожка
        if not self.hidden:                                                             # «таблетка»
            x0 = (2 + pos * self.seg_w) * k
            ring = mix(MUTED, ACCENT, pos) if self.is_switch else ACCENT
            d.rounded_rectangle([x0, 2 * k, x0 + self.seg_w * k, H - 2 * k],
                                radius=(H - 4 * k) / 2, fill=PANEL, outline=ring, width=max(1, round(k)))
        for i, v in enumerate(self.labels):                                             # подписи
            near = 0.0 if self.hidden else max(0.0, 1 - abs(pos - i))
            cx = (2 + self.seg_w * (i + 0.5)) * k
            d.text((cx, H / 2), v, font=self.font, fill=mix(MUTED, TEXT, near), anchor="mm")
        return img.resize((self.pw, self.ph), Image.LANCZOS)


def pill(parent, text, sub=None, height=52):
    """Строка-«таблетка»: подпись слева, справа место под кнопки."""
    f = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=height // 2, height=height)
    f.pack(fill="x", pady=4)
    f.pack_propagate(False)
    left = ctk.CTkFrame(f, fg_color="transparent")
    left.pack(side="left", padx=(20, 0))
    ctk.CTkLabel(left, text=text, text_color=TEXT, height=18, font=font(13)).pack(anchor="w")
    if sub:
        ctk.CTkLabel(left, text=sub, text_color=MUTED, height=14, font=font(10)).pack(anchor="w")
    right = ctk.CTkFrame(f, fg_color="transparent")
    right.pack(side="right", padx=(0, 10))
    return right


def section(parent, text):
    ctk.CTkLabel(parent, text=text, text_color=MUTED, height=16,
                 font=font(11, "bold")).pack(anchor="w", padx=12, pady=(4, 0))


def keycap(parent, text="", command=None, width=120):
    return ctk.CTkButton(parent, text=text, width=width, height=32, corner_radius=16,
                         fg_color=KEYCAP, hover_color=HOVER, border_width=1, border_color=KEYCAP,
                         text_color=TEXT, font=font(12), command=command)


def reset_btn(parent, command):
    return ctk.CTkButton(parent, text="↺", width=30, height=30, corner_radius=15,
                         fg_color="transparent", hover_color=HOVER, text_color=MUTED,
                         font=font(16), command=command)


def small_key(parent, letter):
    ctk.CTkLabel(parent, text=letter, width=30, height=28, corner_radius=10, fg_color=KEYCAP,
                 text_color=TEXT, font=font(12, "bold")).pack(side="left", padx=(0, 4))


class WakeQueue(queue.Queue):
    """Очередь команд из трея / второй копии программы, которая ещё и «будит» окно.
    Раньше команду забирал главный цикл, а в трее он крутится 10 раз в секунду и реже —
    «открыть» срабатывало через 0,1–0,6 с. Теперь окну сразу шлётся событие (это можно делать
    из другого потока: Tk сам передаёт его в поток окна), и команда выполняется тут же."""

    def __init__(self, win):
        super().__init__()
        self.win = win

    def put(self, item, *a, **k):
        super().put(item, *a, **k)
        try:
            self.win.event_generate("<<SkywalkCmd>>", when="tail")
        except Exception:                    # окно ещё не готово или уже закрыто — заберёт главный цикл
            pass


# ================== Главное окно ==================
WINDOW_SIZE = "440x700"     # высота — чтобы на «картах» влезли 6 карт, 14 регионов и итог
class App(ctk.CTk):
    TABS = ("nulls", "binds", "maps", "records")
    SETTINGS = "settings"

    def __init__(self):
        super().__init__(fg_color=BG)
        # Окно собираем СПРЯТАННЫМ: сотни элементов рисуются по очереди, и если окно уже на экране,
        # видно, как оно «собирается из кусков». Показываем, когда всё готово, — см. first_show.
        # customtkinter на старте сам прячет и снова показывает окно (перекрашивает заголовок Windows) —
        # это тоже мигание, а заголовка у нас всё равно нет: отключаем.
        self._deactivate_windows_window_header_manipulation = True
        self.withdraw()
        self.fade_on_map = False              # окно свернули «прозрачным» — при разворачивании проявить
        self.title("Skywalk")
        set_icon(self)
        self.geometry(WINDOW_SIZE)
        # resizable(False) тут НЕ ставим: Tk тогда считает минимальный размер со старой рамкой
        # Windows и растягивает окно при перетаскивании. Растянуть мышкой всё равно нельзя —
        # рамку для растягивания мы убираем в remove_titlebar.
        self.t = 0.0
        self.last_time = time.perf_counter()  # время прошлого кадра (для dt)
        self.next_title = self.next_cs2 = self.next_save = 0.0   # когда в следующий раз делать «редкие» дела
        self.next_snap = 0.0                  # когда освежить снимок текущей страницы
        self.dragging = False                 # окно сейчас перетаскивают мышкой
        self.notified = set()                 # о каких серверах с нужной картой уже оповестили
        if time.time() - prefs["maps_synced"] > 24 * 3600:   # базу карт обновляем раз в сутки
            threading.Thread(target=fetch_kz_maps, daemon=True).start()
        self.next_watch_ui = 0.0              # когда обновить надписи «… сек назад»
        self.glow = 0.1                       # текущая сила свечения (плавно тянется к нужной)
        self.burst = 0.0                      # вспышка при включении (быстро гаснет)
        self.was_on = state["enabled"]
        self.cs2_active = False               # активно ли сейчас окно CS2 (проверяем ~6 раз в секунду)
        self.preview_until = 0.0              # до какого момента показывать оверлей для примера
        self.overlay_edit = False             # режим «переместить оверлей» включён
        self.sub = "sound"                    # открытая подвкладка настроек
        self.reset_armed = False              # «сбросить» нажали один раз — ждём подтверждения
        self.overlay = None
        self.next_records = 0.0               # когда снова дочитать консоль CS2 (рекорды)
        self.next_rec_ui = 0.0                # когда обновить вкладку «рекорды», пока консоль дочитывается
        self.saved = snapshot()               # что сейчас лежит в файле настроек
        # без плавного появления: прозрачность делает окно «слоистым», и при перетаскивании
        # Windows не успевает отрисовывать элементы — появляются чёрные блоки
        remove_titlebar(self)
        self.geometry(WINDOW_SIZE)              # после смены стиля ещё раз задаём размер
        self.build()
        self.toast = MapToast(self)           # уведомление «карта нашлась»
        self.rec_toast = RecordToast(self)    # уведомление «новый рекорд»
        self.after(10, self.first_show)       # показать, когда окно соберётся
        self.bind("<Map>", self.on_map)       # окно развернули (из панели задач) — плавно проявляем
        self.tray_cmds = WakeQueue(self)      # команды из меню трея (оно работает в другом потоке)
        self.bind("<<SkywalkCmd>>", lambda e: self.handle_tray())   # «разбудить» окно — выполнить сразу
        self.told_tray = False                # подсказку «работает в трее» показываем один раз
        self.tray = None
        self.start_tray()
        self.protocol("WM_DELETE_WINDOW", self.on_close_button)
        self.refresh()

    # ---------- сборка интерфейса (заново — при смене языка) ----------
    def build(self, tab="nulls"):
        self.tw = Tweens()
        self._last = {}
        self.tab = None
        self.snapshots = {}                   # запомненные «снимки» страниц: {"nulls": картинка, ...}
        self.fade = None                      # идущий сейчас переход (или None)
        self.sub_ready = False                # подвкладки настроек уже построены

        self.title_img = Title(self)
        self.title_img.pack()
        self.title_img.bind("<ButtonPress-1>", self.drag_start)   # тянуть окно за название

        # свои кнопки «свернуть» и «закрыть» в правом верхнем углу
        close = ctk.CTkButton(self, text="✕", width=34, height=28, corner_radius=8,
                              fg_color="transparent", hover_color="#c42b1c", text_color=MUTED,
                              font=font(13), command=self.on_close_button)
        close.place(relx=1.0, x=-8, y=8, anchor="ne")
        mini = ctk.CTkButton(self, text="—", width=34, height=28, corner_radius=8,
                             fg_color="transparent", hover_color=HOVER, text_color=MUTED,
                             font=font(12), command=self.minimize)
        mini.place(relx=1.0, x=-46, y=8, anchor="ne")

        # --- вкладки: гладкий переключатель, выделение переезжает ---
        self.tabs = Segmented(self, self.TABS, "nulls", self.show_tab, seg_w=96, h=38,
                              labels=[tr("tab_" + n) for n in self.TABS], font_size=13)
        self.tabs.pack(pady=(0, 14))

        # --- страницы ---
        self.holder = ctk.CTkFrame(self, fg_color="transparent", height=430)   # на «картах» больше всего строк
        self.holder.pack(fill="x", padx=26)
        # все страницы лежат стопкой на одном месте; вкладка просто поднимает нужную наверх
        self.pages = {n: ctk.CTkFrame(self.holder, fg_color="transparent")
                      for n in self.TABS + (self.SETTINGS,)}
        self.build_nulls(self.pages["nulls"])
        self.build_binds(self.pages["binds"])
        self.build_maps(self.pages["maps"])
        self.build_records(self.pages["records"])
        self.build_settings(self.pages[self.SETTINGS])
        for pg in self.pages.values():
            pg.place(relx=0, rely=0, relwidth=1, relheight=1)

        # --- низ окна ---
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=22, pady=14)
        self.dot = ctk.CTkLabel(foot, text="●", height=20, font=font(14), cursor="hand2")
        self.dot.pack(side="left")
        self.status = ctk.CTkLabel(foot, text="", height=20, font=font(15, "bold"), cursor="hand2")
        self.status.pack(side="left", padx=(6, 0))
        for w in (self.dot, self.status):
            w.bind("<Button-1>", lambda e: toggle())
        self.gear = GearButton(foot, command=lambda: self.show_tab(self.SETTINGS))
        self.gear.pack(side="right")
        ctk.CTkLabel(foot, text=VERSION, text_color=MUTED, height=20, font=font(11)
                     ).pack(side="right", padx=(0, 10))
        self.hint = ctk.CTkLabel(foot, text="", text_color=MUTED, height=20, font=font(11))
        self.hint.pack(side="right", padx=(0, 14))

        self.show_tab(tab)

    # ---------- перетаскивание окна за название ----------
    def drag_start(self, e):
        """Просим Windows саму тащить окно, как за обычный заголовок: окно едет целиком,
        без перерисовки — плавно и без «разрывов».
        Важно: PostMessage ставит команду в очередь и сразу возвращается. SendMessage ждал бы,
        пока перетаскивание закончится, прямо внутри этого обработчика — из-за этого программа падала."""
        self.dragging = True                                    # пауза анимаций, пока окно тащат
        try:
            user32.ReleaseCapture()                             # Tk «держит» мышь после клика — отпускаем
            user32.PostMessageW(hwnd_of(self), 0x00A1, 2, 0)    # WM_NCLBUTTONDOWN + HTCAPTION («заголовок»)
        except Exception:
            import traceback
            write_crash(traceback.format_exc())

    def report_callback_exception(self, exc, val, tb):
        """Любая ошибка в кнопках/событиях — в журнал и в crash.txt, программа не падает."""
        import traceback
        write_crash("".join(traceback.format_exception(exc, val, tb)))

    def rebuild(self):
        """Смена языка: убираем всё и собираем заново с новыми надписями."""
        for w in self.winfo_children():
            if not isinstance(w, (Floating, MapToast)):   # оверлей и уведомление оставляем
                w.destroy()
        self.build(tab=self.SETTINGS)

    # ---------- вкладка «нуллы» ----------
    def build_nulls(self, page):
        for keys, label, pair in ((("W", "S"), tr("fwd_back"), "ws"),
                                  (("A", "D"), tr("left_right"), "ad")):
            f = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=30, height=60)
            f.pack(fill="x", pady=4)
            f.pack_propagate(False)
            left = ctk.CTkFrame(f, fg_color="transparent")
            left.pack(side="left", padx=(16, 0))
            for k in keys:
                small_key(left, k)
            ctk.CTkLabel(left, text=label, text_color=TEXT, font=font(13)).pack(side="left", padx=(8, 0))
            right = ctk.CTkFrame(f, fg_color="transparent")
            right.pack(side="right", padx=(0, 12))
            ctk.CTkLabel(right, text="nulls", text_color=MUTED, height=14, font=font(10)).pack()
            Segmented(right, ("off", "on"), "on" if state["pairs"][pair] else "off",
                      lambda v, p=pair: state["pairs"].__setitem__(p, v == "on")).pack()

        right = pill(page, tr("only_cs2"), tr("only_cs2_sub"))
        Segmented(right, ("off", "on"), "on" if state["only_cs2"] else "off",
                  lambda v: state.__setitem__("only_cs2", v == "on")).pack()

    # ---------- вкладка «бинды» ----------
    def build_binds(self, page):
        self.bind_btns = []                   # (кнопка, что назначаем) — текст и цвет обновляет tick

        def row(title, sub, target, reset):
            right = pill(page, title, sub, height=46)
            reset_btn(right, reset).pack(side="right", padx=(4, 0))
            btn = keycap(right, command=lambda: self.start_capture(target))
            btn.pack(side="right")
            self.bind_btns.append((btn, target))

        def clear(b):
            return lambda: state.update({b: None, b + "_name": ""})

        row("-w", tr("unw_sub"), "unw", clear("unw"))
        row(tr("lj_name"), tr("lj_sub"), "lj", clear("lj"))
        row(tr("cj_name"), tr("cj_sub"), "cj", clear("cj"))
        row("jumpbug", tr("jb_sub"), "jb", clear("jb"))
        row("danvari", tr("dv_sub"), "dv", clear("dv"))

        # клавиши чекпоинта и телепорта в самой CS2 — их danvari жмёт вместе с прыжком
        right = pill(page, tr("dv_keys"), tr("dv_keys_sub"), height=46)
        reset_btn(right, lambda: state.update(dv_cp=None, dv_tp=None)).pack(side="right", padx=(4, 0))
        for which in ("tp", "cp"):
            btn = keycap(right, command=lambda w=which: self.start_capture("dv_" + w), width=62)
            btn.pack(side="right", padx=(4, 0))
            self.bind_btns.append((btn, "dv_" + which))

        right = pill(page, tr("toggle"), tr("toggle_sub"), height=46)
        reset_btn(right, lambda: state.update(toggle_vk=VK_F8, toggle_name="F8")
                  ).pack(side="right", padx=(4, 0))
        btn = keycap(right, command=lambda: self.start_capture("toggle"))
        btn.pack(side="right")
        self.bind_btns.append((btn, "toggle"))

        ctk.CTkLabel(page, text=tr("bind_hint"),
                     text_color=MUTED, font=font(10), justify="center").pack(pady=(6, 0))
        self.cs2_label = ctk.CTkLabel(page, text="", text_color=MUTED, font=font(10), height=14)
        self.cs2_label.pack()

    def bind_text(self, target):
        """Что написать на кнопке бинда."""
        if target == "toggle":
            return state["toggle_name"]
        if target in ("dv_cp", "dv_tp"):
            name = dv_key_name(target[3:])
            return f"{target[3:]} · {name.lower()}" if name else f"{target[3:]} · —"
        return state[target + "_name"] or tr("assign")

    # ---------- вкладка «рекорды»: лучшие прыжки за всё время (из консоли CS2) ----------
    def build_records(self, page):
        self.rec_view = "all"                 # что показываем: рекорды / сессия / прогресс / тренер
        views = ("all", "session", "progress", "coach")
        Segmented(page, views, "all", self.show_rec_view, seg_w=96, h=32,
                  labels=[tr("rec_alltime"), tr("rec_session"), tr("rec_progress"), tr("rec_coach")]).pack(pady=(0, 6))
        # --- рекорды за всё время ---
        self.rec_all = ctk.CTkFrame(page, fg_color="transparent")
        card = ctk.CTkFrame(self.rec_all, fg_color=PANEL, corner_radius=20)
        card.pack(fill="x", pady=4)
        self.rec_card = card
        self.rec_rows = {}                    # {"LJ": (дистанция, подробности)}
        self.rec_types = None                 # какие строки сейчас нарисованы
        self.rec_status = ctk.CTkLabel(self.rec_all, text="", text_color=MUTED, font=font(10), justify="center")
        self.rec_status.pack(pady=(4, 2))
        right = pill(self.rec_all, tr("rec_reset"), tr("rec_reset_sub"), height=46)
        self.rec_reset_btn = keycap(right, tr("reset_btn"), self.on_reset_records, width=100)
        self.rec_reset_btn.pack()
        self.rec_reset_armed = False
        # --- сессия ---
        self.rec_ses = ctk.CTkFrame(page, fg_color="transparent")
        self.ses_card = ctk.CTkFrame(self.rec_ses, fg_color=PANEL, corner_radius=20)
        self.ses_card.pack(fill="x", pady=4)
        self.ses_rows = {}                    # {"LJ": (число прыжков, тиры, лучший)}
        self.ses_types = None
        self.ses_status = ctk.CTkLabel(self.rec_ses, text="", text_color=MUTED, font=font(10), justify="center")
        self.ses_status.pack(pady=(4, 2))
        right = pill(self.rec_ses, tr("ses_new"), tr("ses_new_sub"), height=46)
        keycap(right, tr("ses_new_btn"), self.on_new_session, width=100).pack()
        # --- прогресс по дням ---
        self.rec_prog = ctk.CTkFrame(page, fg_color="transparent")
        self.prog_type, self.prog_sig = "LJ", None
        Segmented(self.rec_prog, COACH_TYPES, "LJ", self.on_prog_type, seg_w=64, h=28,
                  labels=[a.lower() for a in COACH_TYPES]).pack(pady=(0, 6))
        card = ctk.CTkFrame(self.rec_prog, fg_color=PANEL, corner_radius=20)
        card.pack(fill="x", pady=4)
        self.prog_title = ctk.CTkLabel(card, text="", text_color=TEXT, font=font(13, "bold"), anchor="w")
        self.prog_title.pack(fill="x", padx=18, pady=(12, 4))
        self.prog_img = tk.Label(card, bd=0, highlightthickness=0, bg=PANEL)
        self.prog_img.pack(padx=14, pady=(0, 12))
        self.prog_note = ctk.CTkLabel(self.rec_prog, text="", text_color=MUTED, font=font(10), justify="center")
        self.prog_note.pack(pady=(4, 2))
        # --- тренер ---
        self.rec_coach = ctk.CTkFrame(page, fg_color="transparent")
        self.coach_type, self.coach_sig = "LJ", None
        Segmented(self.rec_coach, COACH_TYPES, "LJ", self.on_coach_type, seg_w=64, h=28,
                  labels=[a.lower() for a in COACH_TYPES]).pack(pady=(0, 6))
        card = ctk.CTkFrame(self.rec_coach, fg_color=PANEL, corner_radius=20)
        card.pack(fill="x", pady=4)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(12, 2))
        self.coach_title = ctk.CTkLabel(head, text="", text_color=TEXT, font=font(13, "bold"), anchor="w")
        self.coach_title.pack(side="left")
        self.coach_gap = ctk.CTkLabel(head, text="", text_color=ACCENT, font=font(12, "bold"), anchor="e")
        self.coach_gap.pack(side="right")
        self.coach_img = tk.Label(card, bd=0, highlightthickness=0, bg=PANEL)
        self.coach_img.pack(padx=14, pady=(2, 4))
        self.coach_tips = []
        for _ in range(4):
            lbl = ctk.CTkLabel(card, text="", text_color=TEXT, font=font(11), anchor="w", justify="left",
                               wraplength=round(340))
            self.coach_tips.append(lbl)
        self.coach_pad = ctk.CTkFrame(card, fg_color="transparent", height=8)
        self.coach_note = ctk.CTkLabel(self.rec_coach, text="", text_color=MUTED, font=font(10), justify="center",
                                       wraplength=380)
        self.coach_note.pack(pady=(4, 2))
        self.rec_frames = {"all": self.rec_all, "session": self.rec_ses, "progress": self.rec_prog, "coach": self.rec_coach}
        self.rec_all.pack(fill="x")
        self.show_records()

    def show_rec_view(self, view):
        """Переключатель «рекорды | сессия | прогресс | тренер». Новый вид собирается под снимком старого,
        а снимок плавно перетекает в него (как при смене вкладок) — ничего не «прыгает» и не собирается на глазах."""
        self.hide_jump()

        def apply():
            self.rec_view = view
            for name, frame in self.rec_frames.items():
                if name != view:
                    frame.pack_forget()
            self.rec_frames[view].pack(fill="x")
            self.prog_sig = self.coach_sig = None    # перерисовать при показе
            self.show_records()
        self.switch_page(self.holder, None, None, apply=apply)

    def on_prog_type(self, abbr):
        def apply():
            self.prog_type, self.prog_sig = abbr, None
            self.show_records()
        self.switch_page(self.holder, None, None, apply=apply)

    def on_coach_type(self, abbr):
        def apply():
            self.coach_type, self.coach_sig = abbr, None
            self.show_records()
        self.switch_page(self.holder, None, None, apply=apply)

    def page_key(self):
        """Под каким именем запомнен снимок того, что сейчас открыто в главной области окна.
        У «рекордов» свой снимок на каждый вид (и на каждый тип прыжка в тренере/прогрессе)."""
        if self.tab != "records":
            return self.tab
        key = "records:" + self.rec_view
        if self.rec_view == "coach":
            key += ":" + self.coach_type
        elif self.rec_view == "progress":
            key += ":" + self.prog_type
        return key

    def show_progress(self):
        """График: лучший прыжок выбранного типа по дням."""
        abbr = self.prog_type
        days = sorted(prefs["history"].get(abbr, {}).items())
        sig = (abbr, tuple(days[-30:]), ACCENT)
        if sig == self.prog_sig:
            return
        self.prog_sig = sig
        self._set(self.prog_title, "pt", text=tr("prog_title", abbr=abbr.lower()))
        if not days:
            self.prog_img.configure(image="", height=1)
            self._set(self.prog_note, "pn", text=tr("coach_none", abbr=abbr.lower()).split("\n")[0])
            return
        img = render_progress_chart(dpi_scale(self), days, DIST_TIERS["CKZ"].get(abbr))
        self.prog_photo = ImageTk.PhotoImage(img)          # ссылку держим — иначе картинка пропадёт
        self.prog_img.configure(image=self.prog_photo, height=img.height)
        self._set(self.prog_note, "pn", text=tr("prog_few") if len(days) < 2 else "")

    def coach_jump(self, abbr):
        """Какой прыжок разбирать: последний этого типа в сессии, иначе — рекордный (если есть стрейфы)."""
        for j in reversed(record_watcher.jumps):
            if j["abbr"] == abbr and len(j["strafes"]) >= 2 and j is not record_watcher.cur:
                return j, False
        rec = prefs["records"].get(abbr)
        if rec and rec.get("strafe_rows"):
            stats = {k: v for k, v in rec.items() if isinstance(v, (int, float))}
            return {"abbr": abbr, "dist": rec["dist"], "stats": stats, "tokens": rec.get("details", []),
                    "strafes": rec["strafe_rows"], "graph": rec.get("graph", {})}, True
        return None, False

    def show_coach(self):
        """Тренер: разбор прыжка по стрейфам + советы + сводка сессии."""
        abbr = self.coach_type
        j, is_record = self.coach_jump(abbr)
        sig = (abbr, id(j), j and len(j["strafes"]), len(record_watcher.jumps), ACCENT)
        if sig == self.coach_sig:
            return
        self.coach_sig = sig
        target = jump_target(abbr)
        for lbl in self.coach_tips:
            lbl.pack_forget()
        self.coach_pad.pack_forget()
        if j is None:
            self._set(self.coach_title, "ct", text=tr("coach_none", abbr=abbr.lower()))
            self._set(self.coach_gap, "cg", text="")
            self.coach_img.configure(image="", height=1)
            self._set(self.coach_note, "cn", text="")
            return
        key = "coach_record" if is_record else "coach_last"
        self._set(self.coach_title, "ct", text=tr(key, abbr=abbr.lower(), dist=f"{j['dist']:.2f}"))
        best = prefs["records"].get(abbr, {}).get("dist", j["dist"])
        if target:
            gap = target - best
            self._set(self.coach_gap, "cg", text=tr("coach_done", target=f"{target:g}") if gap <= 0
                      else tr("coach_gap", target=f"{target:g}", gap=f"{gap:.2f}"))
        img = render_strafe_chart(dpi_scale(self), j["strafes"])
        self.coach_photo = ImageTk.PhotoImage(img)
        self.coach_img.configure(image=self.coach_photo, height=img.height)
        tips = analyze_jump(j)[:4] or [(0, "clean", tr("tip_clean"))]
        for lbl, (_, _, text) in zip(self.coach_tips, tips):
            lbl.configure(text="•  " + text)
            lbl.pack(fill="x", padx=18, pady=(2, 0))
        self.coach_pad.pack()
        # сводка сессии по этому типу: средние и самые частые ошибки
        same = [x for x in record_watcher.jumps if x["abbr"] == abbr and x["stats"].get("sync") is not None]
        if not same:
            self._set(self.coach_note, "cn", text="")
            return

        def avg(k):
            vals = [x["stats"][k] for x in same if k in x["stats"]]
            return f"{sum(vals) / len(vals):.{0 if k == 'pre' else 1}f}" if vals else "—"

        text = tr("coach_summary", abbr=abbr.lower(), n=len(same), sync=avg("sync"), bad=avg("bad"),
                  ovl=avg("ovl"), dead=avg("dead"), eff=avg("eff"), pre=avg("pre"))
        counts = {}
        for x in same:
            if len(x["strafes"]) >= 2:
                top = x.get("_top3")          # разбор готового прыжка не меняется — считаем один раз
                if top is None:
                    top = [k for _, k, _ in analyze_jump(x)[:3]]
                    if x is not record_watcher.cur:
                        x["_top3"] = top
                for k in top:
                    counts[k] = counts.get(k, 0) + 1
        if counts:
            names = tip_names()
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
            text += "\n" + tr("coach_often", list=", ".join(f"{names.get(k, k)} ({n})" for k, n in top))
        self._set(self.coach_note, "cn", text=text)

    def draw_session_rows(self):
        """Строки сессии: только типы, которые уже прыгали в этой сессии."""
        types = [a for a, _, _ in JUMP_TYPES if a in record_watcher.session]
        if types == self.ses_types:
            return
        self.ses_types = types
        for w in self.ses_card.winfo_children():
            w.destroy()
        self.ses_rows = {}
        if not types:                         # пусто — одна строка-подсказка (и карточка не сжимается в ноль)
            ctk.CTkLabel(self.ses_card, text=tr("ses_empty"), text_color=MUTED, font=font(11),
                         height=40).pack(pady=12)
            return
        h = 30 if len(types) <= 7 else 27
        for i, abbr in enumerate(types):
            row = ctk.CTkFrame(self.ses_card, fg_color="transparent", height=h)
            row.pack(fill="x", padx=18, pady=(10 if i == 0 else 0, 10 if i == len(types) - 1 else 0))
            row.pack_propagate(False)
            name = ctk.CTkLabel(row, text=abbr.lower(), text_color=TEXT, width=42, anchor="w", font=font(13, "bold"))
            name.pack(side="left")
            count = ctk.CTkLabel(row, text="", text_color=TEXT, width=34, anchor="w", font=font(12, "bold"))
            count.pack(side="left")
            best = ctk.CTkLabel(row, text="", text_color=TEXT, width=64, anchor="e", font=font(14, "bold"))
            best.pack(side="right")
            tiers = ctk.CTkLabel(row, text="", text_color=MUTED, anchor="e", font=font(10))
            tiers.pack(side="right", padx=(0, 10))
            self.ses_rows[abbr] = (count, tiers, best)
            for w in (row, name, count, tiers, best):
                w.bind("<Button-1>", lambda e, a=abbr: self.show_session_type(a))
                w.bind("<Enter>", lambda e, n=name: n.configure(text_color=ACCENT))
                w.bind("<Leave>", lambda e, n=name: n.configure(text_color=TEXT))
                w.configure(cursor="hand2")

    def show_session(self):
        self.draw_session_rows()
        for abbr, (count_lbl, tiers_lbl, best_lbl) in self.ses_rows.items():
            s = record_watcher.session[abbr]
            # «imp 5 · perf 2 · god 1» — от лучшего тира к худшему, только те, что были (meh — в карточке)
            parts = [f"{TIER_SHORT[t - 1]} {s['tiers'][t]}" for t in range(6, 1, -1) if s["tiers"][t]]
            self._set(count_lbl, "sc_" + abbr, text=f"{s['count']}×")
            self._set(tiers_lbl, "st_" + abbr, text=" · ".join(parts[:3]))
            self._set(best_lbl, "sb_" + abbr, text=f"{s['best']:.1f}")
        total = sum(s["count"] for s in record_watcher.session.values())
        if not record_watcher.found:
            status = tr("rec_off")
        elif total:
            status = tr("ses_status", since=record_watcher.session_since or "—", n=total)
        else:
            status = tr("rec_on", nick=current_nick()) if current_nick() else tr("rec_nonick")
        self._set(self.ses_status, "sst", text=status)

    def show_session_type(self, abbr):
        """Карточка: полная разбивка сессии по тирам для этого типа прыжка."""
        s = record_watcher.session.get(abbr)
        if not s:
            return
        tokens = [f"{s['count']} {tr('ses_count')}", f"{s['best']:.2f} {tr('ses_best')}",
                  f"{s['sum'] / s['count']:.2f} {tr('ses_avg')}", ""]
        tokens += [f"{s['tiers'][t]} {TIER_NAMES[t - 1]}" for t in range(1, 7)]
        self.show_card(tr("ses_title", abbr=abbr.lower()), record_watcher.session_since, tokens)

    def on_new_session(self):
        record_watcher.new_session()
        self.hide_jump()
        self.show_records()

    def draw_record_rows(self):
        """Строки рекордов: всегда основные типы + редкие, если по ним уже есть рекорд."""
        types = list(SHOW_TYPES) + [a for a, _, _ in JUMP_TYPES if a not in SHOW_TYPES and a in prefs["records"]]
        if types == self.rec_types:
            return
        self.rec_types = types
        for w in self.rec_card.winfo_children():
            w.destroy()
        self.rec_rows = {}
        h = 30 if len(types) <= 7 else 27
        for i, abbr in enumerate(types):
            row = ctk.CTkFrame(self.rec_card, fg_color="transparent", height=h)
            row.pack(fill="x", padx=18, pady=(10 if i == 0 else 0, 10 if i == len(types) - 1 else 0))
            row.pack_propagate(False)
            name = ctk.CTkLabel(row, text=abbr.lower(), text_color=TEXT, width=42, anchor="w", font=font(13, "bold"))
            name.pack(side="left")
            full = ctk.CTkLabel(row, text=JUMP_NAMES[abbr], text_color=MUTED, anchor="w", font=font(10))
            full.pack(side="left")
            dist = ctk.CTkLabel(row, text="—", text_color=MUTED, width=64, anchor="e", font=font(14, "bold"))
            dist.pack(side="right")
            det = ctk.CTkLabel(row, text="", text_color=MUTED, anchor="e", font=font(10))
            det.pack(side="right", padx=(0, 10))
            self.rec_rows[abbr] = (dist, det)
            # клик по строке — подробная карточка прыжка; при наведении название подсвечивается
            for w in (row, name, full, dist, det):
                w.bind("<Button-1>", lambda e, a=abbr: self.show_jump(a))
                w.bind("<Enter>", lambda e, a=abbr, n=name: prefs["records"].get(a) and n.configure(text_color=ACCENT))
                w.bind("<Leave>", lambda e, n=name: n.configure(text_color=TEXT))
                w.configure(cursor="hand2")

    def show_records(self):
        """Обновить цифры на вкладке «рекорды» (открытый вид: рекорды или сессия)."""
        if self.rec_view == "session":
            self.show_session()
            return
        if self.rec_view == "progress":
            self.show_progress()
            return
        if self.rec_view == "coach":
            self.show_coach()
            return
        self.draw_record_rows()
        for abbr, (dist_lbl, det_lbl) in self.rec_rows.items():
            rec = prefs["records"].get(abbr)
            if rec is None:
                self._set(dist_lbl, "rd_" + abbr, text="—", text_color=MUTED)
                self._set(det_lbl, "rt_" + abbr, text="")
                continue
            parts = []
            if rec.get("block"):
                parts.append(tr("rec_block", b=f"{rec['block']:.0f}"))
            if "pre" in rec:
                parts.append(f"pre {rec['pre']:.0f}")
            if "sync" in rec:
                parts.append(f"{rec['sync']:.0f}%")
            if "time" in rec:                 # дата рекорда: «12 окт»
                t = time.localtime(rec["time"])
                parts.append(f"{t.tm_mday} {tr('months').split()[t.tm_mon - 1]}")
            self._set(dist_lbl, "rd_" + abbr, text=f"{rec['dist']:.1f}", text_color=TEXT)
            self._set(det_lbl, "rt_" + abbr, text=" · ".join(parts))
        if not record_watcher.found:
            status = tr("rec_off")
        elif not current_nick():
            status = tr("rec_nonick")
        else:
            status = tr("rec_on", nick=current_nick())
        self._set(self.rec_status, "rst", text=status)

    def show_jump(self, abbr):
        """Карточка с полной статистикой рекорда. Клик — закрыть."""
        rec = prefs["records"].get(abbr)
        if not rec:
            return
        when = ""
        if "time" in rec:
            t = time.localtime(rec["time"])
            when = f"{t.tm_mday} {tr('months').split()[t.tm_mon - 1]} {t.tm_hour:02d}:{t.tm_min:02d}"
        if rec.get("map"):                    # где поставлен рекорд
            when = (when + " · " if when else "") + rec["map"]
        tokens = list(rec.get("details") or [])
        if not tokens:                        # подробностей нет (старый рекорд) — что известно
            tokens = [f"{rec[k]:g} {n}" for k, n in (("strafes", "Strafes"), ("sync", "% AvgSync"), ("pre", "Pre"),
                                                      ("max", "Max"), ("block", "Block"), ("height", "Height")) if k in rec]
            tokens = [t.replace(" %", "%") for t in tokens]
        nxt = next_tier(abbr, rec["dist"])        # сколько до следующего тира — первой ячейкой
        if nxt:
            tokens.insert(0, f"{nxt[1]:.2f} {tr('card_to_next', t=nxt[0])}")
        elif abbr in DIST_TIERS["CKZ"]:
            tokens.insert(0, tr("card_top"))
        self.show_card(f"{abbr.lower()}  {rec['dist']:.4f}", when, tokens)

    def show_card(self, title, when, tokens):
        """Карточка поверх списка (рекордов или сессии), ровно на его месте: заголовок, время,
        поля «значение название» в две колонки. Одна картинка (render_card) — появляется целиком,
        плавно проявляясь поверх списка за 0,14 с. Клик — закрыть."""
        self.hide_jump()
        card = self.ses_card if self.rec_view == "session" else self.rec_card
        page = self.pages["records"]
        self.update_idletasks()
        w, h = card.winfo_width(), card.winfo_height()
        if w < 10 or h < 10:
            return
        x, y = card.winfo_rootx() - page.winfo_rootx(), card.winfo_rooty() - page.winfo_rooty()
        k = dpi_scale(self)
        rows = (min(len(tokens), 24) + 1) // 2                      # полей много — карточка выше списка
        h = min(max(h, round((12 + 26 + 6 + rows * 19 + 12) * k)), page.winfo_height() - y)
        img = render_card(k, w, h, title, when, tokens)
        under = None
        if self.is_front():                                          # что под карточкой — из этого проявляемся
            full = grab_widget(page)
            if full is not None and full.width >= x + w and full.height >= y + h:
                under = full.crop((x, y, x + w, y + h))
        photo = ImageTk.PhotoImage(under if under is not None else img)
        box = tk.Label(page, image=photo, bd=0, highlightthickness=0, bg=BG, cursor="hand2")
        box.photo = photo                                            # ссылку держим — иначе картинка пропадёт
        box.img, box.under = img, under                              # для растворения при закрытии
        box.place(x=x, y=y, width=w, height=h)
        box.lift()
        box.bind("<Button-1>", lambda e: self.hide_jump(animate=True))
        self.jump_box = box
        if under is not None:
            self.card_fade(box, under, img, time.perf_counter(), None)

    def card_fade(self, box, a, b, start, done):
        """Плавный переход картинки карточки a → b за 0,14 с; в конце — done() (если есть)."""
        if not box.winfo_exists():
            return
        k = min(1.0, (time.perf_counter() - start) / 0.14)
        box.photo.paste(Image.blend(a, b, smooth(k)) if k < 1.0 else b)
        if k < 1.0:
            self.after(FRAME_MS, self.card_fade, box, a, b, start, done)
        elif done:
            done()

    def hide_jump(self, animate=False):
        """Убрать карточку. animate=True (клик по ней) — плавно растворяется, иначе (смена вкладки/вида) — сразу."""
        box, self.jump_box = getattr(self, "jump_box", None), None
        if box is None or not box.winfo_exists():
            return
        if animate and box.under is not None and self.is_front():
            box.unbind("<Button-1>")                                 # растворяемся обратно в список под карточкой
            self.card_fade(box, box.img, box.under, time.perf_counter(), box.destroy)
            return
        box.destroy()

    def on_reset_records(self):
        if not self.rec_reset_armed:          # первое нажатие — просим подтвердить
            self.rec_reset_armed = True
            self.rec_reset_btn.configure(text=tr("reset_sure"), text_color=ACCENT)
            self.after(3000, self.disarm_reset_records)
            return
        self.rec_reset_armed = False
        self.hide_jump()
        prefs["records"] = {}
        record_watcher.last = None
        self.rec_reset_btn.configure(text=tr("reset_btn"), text_color=TEXT)
        self.show_records()

    def disarm_reset_records(self):
        if self.rec_reset_armed and self.rec_reset_btn.winfo_exists():
            self.rec_reset_armed = False
            self.rec_reset_btn.configure(text=tr("reset_btn"), text_color=TEXT)

    # ---------- вкладка «карты»: слежка за картами на серверах Cybershoke ----------
    def build_maps(self, page):
        # карты, которые ждём: поле ввода с «+» и под ним «чипсы» выбранных карт (× — убрать)
        card = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=20)
        card.pack(fill="x", pady=4)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=(20, 10), pady=10)
        self.maps_head = head
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(left, text=tr("watch_map"), text_color=TEXT, height=18, font=font(13)).pack(anchor="w")
        ctk.CTkLabel(left, text=tr("watch_map_sub"), text_color=MUTED, height=14, font=font(10)).pack(anchor="w")
        self.add_btn = keycap(head, "+", self.add_typed, width=32)
        self.add_btn.pack(side="right", padx=(6, 0))
        self.map_entry = ctk.CTkEntry(head, width=150, height=32, corner_radius=16, fg_color=KEYCAP,
                                      border_color=KEYCAP, text_color=TEXT, font=font(12),
                                      placeholder_text="kz_…", placeholder_text_color=MUTED)
        self.map_entry.bind("<KeyRelease>", self.on_type)
        self.map_entry.bind("<Return>", self.pick_first)
        self.map_entry.bind("<FocusOut>", lambda e: self.after(200, self.hide_suggest))
        self.map_entry.pack(side="right")
        # место под «чипсы». Показываем, только когда есть хоть одна карта: пустая рамка
        # в customtkinter не сжимается, а остаётся высотой 200 и раздувает весь блок
        self.map_chips = ctk.CTkFrame(card, fg_color="transparent", height=1)
        for c in range(3):                    # 3 одинаковые колонки — длинные названия не вылезают
            self.map_chips.grid_columnconfigure(c, weight=1, uniform="maps")
        self.maps_sig = None                  # какой список карт сейчас нарисован
        self.map_btns = {}                    # «чипсы» карт: {"kz_grotto": кнопка, ...}
        self.leaving = set()                  # карты, чьи «чипсы» сейчас плавно гаснут перед удалением
        self.maps_page = page

        # выпадающий список подсказок. Создаём ОДИН раз и потом только прячем/показываем:
        # если создавать заново на каждую букву, он на миг мигает чёрным
        self.sugg = None                      # список сейчас открыт (= self.sugg_box) или спрятан (None)
        self.sugg_found = []                  # какие карты сейчас в подсказках
        self.sugg_box = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=14, border_width=1, border_color=HOVER)
        self.sugg_rows = []
        for _ in range(6):
            row = ctk.CTkFrame(self.sugg_box, fg_color="transparent")
            live_lbl = ctk.CTkLabel(row, text="", text_color=ACCENT, width=0, height=26, font=font(10))
            live_lbl.pack(side="right", padx=(4, 8))
            tier_lbl = ctk.CTkLabel(row, text="", text_color=MUTED, width=0, height=26, font=font(10))
            tier_lbl.pack(side="right")
            btn = ctk.CTkButton(row, text="", anchor="w", width=60, height=26, corner_radius=10,
                                fg_color="transparent", hover_color=HOVER, text_color=TEXT, font=font(12))
            btn.pack(side="left", fill="x", expand=True)
            self.sugg_rows.append((row, btn, tier_lbl, live_lbl))

        # регионы: кнопки-«чипсы» с пингом, можно выбрать несколько
        card = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=20)
        card.pack(fill="x", pady=4)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(10, 4))
        ctk.CTkLabel(head, text=tr("regions"), text_color=TEXT, height=18, font=font(13)).pack(side="left")
        ctk.CTkLabel(head, text=tr("regions_sub"), text_color=MUTED, height=18, font=font(10)
                     ).pack(side="right")
        self.chips = ctk.CTkFrame(card, fg_color="transparent")
        self.chips.pack(fill="x", padx=14, pady=(0, 10))
        self.chips_sig = None                 # какой набор регионов сейчас нарисован
        self.chip_btns = {}                   # кнопки регионов: {"DE": кнопка, ...}
        for c in range(REGION_COLS):          # одинаковые колонки на всю ширину — ничего не вылезает
            self.chips.grid_columnconfigure(c, weight=1, uniform="chips")

        right = pill(page, tr("watch"), tr("watch_sub"), height=46)
        Segmented(right, ("off", "on"), "on" if prefs["watch_on"] else "off", self.on_watch).pack()

        # итог: статус проверки и по строчке на каждую карту. Найденная карта — кликабельная строка:
        # клик — подключиться к этому серверу (при наведении строка подсвечивается синим)
        card = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=20)
        card.pack(fill="x", pady=4)
        side = ctk.CTkFrame(card, fg_color="transparent")
        side.pack(side="right", anchor="n", padx=10, pady=10)
        keycap(side, tr("check_now"), watcher.refresh, width=110).pack()
        # подсказка — под кнопкой (там пусто), появляется, только когда есть на что кликнуть
        self.click_hint = ctk.CTkLabel(side, text=tr("click_hint"), text_color=MUTED, font=font(10),
                                       justify="center", wraplength=110)
        info = ctk.CTkFrame(card, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=(20, 0), pady=10)
        self.watch_status = ctk.CTkLabel(info, text="", text_color=MUTED, height=16, font=font(10),
                                         anchor="w", justify="left")
        self.watch_status.pack(anchor="w")
        self.result_rows = []                 # 4 строки итога: [надпись, адрес сервера или None]
        for i in range(4):
            lbl = ctk.CTkLabel(info, text="", text_color=TEXT, height=21, font=font(12), anchor="w")
            row = [lbl, None]
            lbl.bind("<Button-1>", lambda e, r=row: r[1] and connect_to(r[1]))
            lbl.bind("<Enter>", lambda e, r=row: r[1] and r[0].configure(text_color=ACCENT))
            lbl.bind("<Leave>", lambda e, r=row: r[0].configure(text_color=TEXT))
            self.result_rows.append(row)

        self.seen_version = -1                # какую проверку уже показали
        self.draw_maps()                      # «чипсы» карт — сразу, а не через секунду

    # ---------- поиск карты: подсказки под полем ----------
    def on_type(self, e=None):
        if e is not None and e.keysym in ("Return", "Escape"):
            if e.keysym == "Escape":
                self.hide_suggest()
            return
        text = self.map_entry.get()
        live = {}
        for s in watcher.servers:            # на скольких серверах карта стоит прямо сейчас
            live[s["map"]] = live.get(s["map"], 0) + 1
        maps = sorted(set(prefs["known_maps"]) | set(CYBERSHOKE_MAPS) | set(live))
        chosen = {map_core(m) for m in prefs["watch_maps"]}
        found = [m for m in suggest_maps(text, maps, live, limit=12) if map_core(m) not in chosen][:6]
        self.show_suggest(found, live)

    def show_suggest(self, found, live):
        """Показать подсказки ровно под полем ввода: шириной от поля до кнопки «+»."""
        self.sugg_found = found
        if not found:
            self.hide_suggest()
            return
        for i, (row, btn, tier_lbl, live_lbl) in enumerate(self.sugg_rows):
            row.pack_forget()
            if i >= len(found):
                continue
            m = found[i]
            tier, n = tier_of(m), live.get(m, 0)
            btn.configure(text=m, command=lambda x=m: self.choose_map(x))
            tier_lbl.configure(text=f"t{tier}" if tier else "")
            live_lbl.configure(text=f"● {n}" if n else "")
            row.pack(fill="x", padx=6, pady=(6 if i == 0 else 0, 6 if i == len(found) - 1 else 0))
        # место считаем в настоящих пикселях от края страницы и ставим обычным tk.place —
        # так рамка списка точно совпадает с краями поля и кнопки «+»
        page = self.maps_page
        x0 = self.map_entry.winfo_rootx() - page.winfo_rootx()
        x1 = self.add_btn.winfo_rootx() + self.add_btn.winfo_width() - page.winfo_rootx()
        y = self.map_entry.winfo_rooty() + self.map_entry.winfo_height() - page.winfo_rooty() + round(4 * dpi_scale(page))
        tk.Frame.place(self.sugg_box, x=x0, y=y, width=x1 - x0)
        self.sugg_box.lift()
        self.sugg_box.update_idletasks()      # дорисовать строки сразу — иначе на миг виден пустой прямоугольник
        self.sugg = self.sugg_box

    def hide_suggest(self):
        if self.sugg is not None:
            self.sugg_box.place_forget()
            self.sugg = None

    def choose_map(self, m):
        """Выбрали карту (подсказка, Enter или «+») — добавляем в список, поле очищаем."""
        self.hide_suggest()
        m = m.strip()
        if not m:
            return
        self.map_entry.delete(0, "end")
        if map_core(m) in {map_core(x) for x in prefs["watch_maps"]} or len(prefs["watch_maps"]) >= MAX_WATCH:
            return                            # уже есть или список полон
        prefs["watch_maps"] = prefs["watch_maps"] + [m]
        self.on_maps_changed()

    def add_typed(self):
        """Кнопка «+»: первая подсказка, если есть, иначе что вписано."""
        self.pick_first()

    def pick_first(self, e=None):
        """Enter — берём первую подсказку (или что вписано, если подсказок нет)."""
        if self.sugg is not None and self.sugg_found:
            self.choose_map(self.sugg_found[0])
        else:
            self.choose_map(self.map_entry.get())

    def remove_map(self, m):
        """Клик по «чипсе»: она плавно гаснет в цвет фона, потом карта убирается из списка."""
        b = self.map_btns.get(m)
        if b is None or m in self.leaving:
            return
        self.leaving.add(m)
        for prop in ("fg_color", "border_color", "text_color"):
            self.tw.to(b, prop, PANEL)
        self.after(180, self.finish_remove, m)

    def finish_remove(self, m):
        self.leaving.discard(m)
        prefs["watch_maps"] = [x for x in prefs["watch_maps"] if x != m]
        self.on_maps_changed()

    def draw_maps(self):
        """«Чипсы» выбранных карт: «grotto · t2  ×». Клик — убрать карту из списка.
        Кнопки не пересоздаём: новые плавно проявляются из цвета фона, убранные удаляются,
        остальные просто встают на новые места."""
        maps = list(prefs["watch_maps"])
        sig = (tuple(maps), LANG)
        if sig == self.maps_sig:
            return
        self.maps_sig = sig
        for m in [m for m in self.map_btns if m not in maps]:     # карту убрали — удаляем её кнопку
            b = self.map_btns.pop(m)
            for prop in ("fg_color", "border_color", "text_color"):
                self.tw.forget(b, prop)
            b.destroy()
        for i, m in enumerate(maps):
            b = self.map_btns.get(m)
            if b is None:                     # новая карта — кнопка появляется из цвета фона
                short = m.split("_", 1)[1] if m.lower().startswith(("kz_", "bkz_", "skz_")) else m
                if len(short) > 13:
                    short = short[:12] + "…"
                tier = tier_of(m)
                b = ctk.CTkButton(self.map_chips, text=f"{short} · t{tier}  ×" if tier else f"{short}  ×",
                                  width=10, height=26, corner_radius=13, border_width=1,
                                  hover_color=HOVER, font=font(10), command=lambda x=m: self.remove_map(x))
                for prop in ("fg_color", "border_color", "text_color"):
                    self.tw.to(b, prop, PANEL)        # первый вызов ставит цвет сразу — старт анимации
                self.tw.to(b, "fg_color", ACCENT_DIM)
                self.tw.to(b, "border_color", ACCENT)
                self.tw.to(b, "text_color", TEXT)
                self.map_btns[m] = b
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2, sticky="ew")
        # есть карты — показываем ряд «чипсов» под полем, нет — прячем его (блок становится одной строкой)
        if maps and not self.map_chips.winfo_manager():
            self.maps_head.pack_configure(pady=(10, 4))
            self.map_chips.pack(fill="x", padx=14, pady=(0, 10))
        elif not maps and self.map_chips.winfo_manager():
            self.map_chips.pack_forget()
            self.maps_head.pack_configure(pady=10)

    def draw_chips(self):
        """Кнопки регионов с пингом. Кнопки создаём один раз, а потом только меняем текст и цвет
        (плавно) — раньше они удалялись и создавались заново, и блок мигал после каждой проверки."""
        regions = watcher.regions()
        sig = (tuple(regions), LANG)
        if sig != self.chips_sig:             # поменялся сам набор регионов — строим заново
            if self.tab != "maps":            # 14 кнопок строятся ~0,3 с — окно бы «замерло» на чужой вкладке.
                return                        # Построим при открытии «карт» (под снимком перехода, незаметно)
            self.chips_sig = sig
            for w in self.chips.winfo_children():
                w.destroy()
            self.chip_btns = {}
            for k in [k for k in self._last if k.startswith("chip_")]:
                del self._last[k]             # новые кнопки — забываем, что писали на старых
            if not regions:
                ctk.CTkLabel(self.chips, text=tr("regions_empty"), text_color=MUTED, height=24,
                             font=font(11)).grid(row=0, column=0, columnspan=REGION_COLS, pady=(2, 4))
                return
            for i, region in enumerate(regions):
                b = ctk.CTkButton(self.chips, text="", width=10, height=26, corner_radius=13,
                                  border_width=1, hover_color=HOVER, font=font(10),
                                  command=lambda x=region: self.toggle_region(x))
                b.grid(row=i // REGION_COLS, column=i % REGION_COLS, padx=2, pady=2, sticky="ew")
                self.chip_btns[region] = b
        for region, b in self.chip_btns.items():          # обновляем только текст и цвета
            ping = watcher.region_ping.get(region)
            chosen = region in prefs["watch_regions"]
            self._set(b, "chip_" + region, text=f"{region}  {ping}" if ping is not None else f"{region}  —")
            self.tw.to(b, "fg_color", ACCENT_DIM if chosen else KEYCAP)
            self.tw.to(b, "border_color", ACCENT if chosen else KEYCAP)
            self.tw.to(b, "text_color", TEXT if chosen else MUTED)

    def toggle_region(self, region):
        regs = prefs["watch_regions"]
        prefs["watch_regions"] = [x for x in regs if x != region] if region in regs else regs + [region]
        self.notified = set()                 # поменяли условия — оповестим заново, если карта уже есть
        self.show_watch()

    def on_maps_changed(self):
        self.notified = set()                 # поменяли список — оповестим заново, если карта уже есть
        self.show_watch()
        if prefs["watch_on"] and not watcher.busy:
            watcher.refresh()                 # сразу проверяем новую карту (и меряем пинг до её серверов)

    def on_watch(self, v):
        prefs["watch_on"] = v == "on"
        self.notified = set()
        if prefs["watch_on"]:
            watcher.refresh()                 # сразу проверяем, не ждём 45 секунд

    def check_found(self):
        """Появилась нужная карта на новом сервере — оповещаем (один раз на сервер)."""
        if not prefs["watch_on"] or watcher.error:
            return
        found = watcher.matches()
        new = [s for s in found if s["addr"] not in self.notified]
        self.notified = {s["addr"] for s in found}   # карта ушла и вернулась — оповестим снова
        if not new:
            return
        s = new[0]
        ping = watcher.ping.get(s["addr"])
        text = tr("notify", map=s["map"], num=s["num"], region=s["region"], players=s["players"],
                  max=s["max"], ping=f"{ping} {tr('ms')}" if ping is not None else "—")
        log("карта найдена: " + text)
        play_chime()                          # свой сигнал вместо системного
        self.toast.show(s, ping)              # своя карточка сверху по центру экрана

    def show_watch(self):
        """Обновить надписи на вкладке «карты»."""
        self.draw_chips()
        self.draw_maps()
        targets = [m for m in prefs["watch_maps"] if m.strip()]
        err = watcher.error
        if watcher.busy:
            status = tr("st_wait")
        elif err in ("nokey", "badkey", "net"):
            status = tr("st_" + err)
        elif not watcher.checked:
            status = tr("st_nokey") if not prefs["steam_key"] else tr("st_idle")
        else:
            status = tr("st_ok", n=len(watcher.servers), ago=int(time.time() - watcher.checked))
        self._set(self.watch_status, "wst", text=status)

        found = watcher.matches() if watcher.checked and not err else []
        if not targets:
            lines = [(tr("st_nomap"), None)]
        elif not watcher.checked or err:
            lines = []
        else:
            lines = self.watch_lines(targets, found, watcher.elsewhere())
        sig = tuple(lines)
        if sig != self._last.get("wlines"):   # перестраиваем строки, только если что-то поменялось
            self._last["wlines"] = sig
            for lbl, _ in self.result_rows:
                lbl.pack_forget()
            self.click_hint.pack_forget()
            for row, (text, addr) in zip(self.result_rows, lines):
                row[1] = addr
                row[0].configure(text=text, text_color=TEXT, cursor="hand2" if addr else "")
                row[0].pack(anchor="w")
            if any(addr for _, addr in lines):
                self.click_hint.pack(pady=(6, 0))

    def watch_lines(self, targets, found, other, max_lines=4):
        """Итог по КАЖДОЙ карте из списка, по строчке:
             outback · #39 [LT] · 0/64 · 41 мс          — идёт в выбранных регионах (лучший по пингу)
             dzy_beyond_v2 · только [FI]                — есть, но в регионе, который не выбран
             нигде нет: luv_less, haste +1              — ненайденные одной строкой (2 названия + число)
        Строк не больше 4 (иначе не влезет в окно), лишнее — «… и ещё N».
        Ответ: [(текст, адрес сервера для подключения или None), ...]."""
        def short(m):                         # kz_dzy_beyond_v2 → dzy_beyond_v2 (место в строке дорогое)
            m = m.split("_", 1)[1] if m.lower().startswith(("kz_", "bkz_", "skz_")) else m
            return m if len(m) <= 16 else m[:15] + "…"

        lines, nowhere = [], []
        for m in targets:                     # found и other уже отсортированы по пингу — первый лучший
            core = map_core(m)
            here = [s for s in found if s["map"] == core]
            there = [s for s in other if s["map"] == core]
            if here:
                s = here[0]
                ping = watcher.ping.get(s["addr"])
                lines.append((0, ping or 0, f"{short(core)} · #{s['num']} [{s['region']}] · "
                              f"{s['players']}/{s['max']}" + (f" · {ping} {tr('ms')}" if ping is not None else ""),
                              s["addr"]))
            elif there:
                where = ", ".join(sorted({f"[{s['region']}]" for s in there}))
                lines.append((1, 0, tr("elsewhere", map=short(core), where=where), None))
            else:
                nowhere.append(short(core))
        lines = [(text, addr) for _, _, text, addr in sorted(lines, key=lambda x: (x[0], x[1]))]   # сначала найденные
        if nowhere:                           # строка узкая — влезают 2 названия, остальные числом
            names = ", ".join(m if len(m) <= 12 else m[:11] + "…" for m in nowhere[:2])
            lines.append((tr("nowhere", maps=names + (f" +{len(nowhere) - 2}" if len(nowhere) > 2 else "")), None))
        if len(lines) > max_lines:
            lines = lines[:max_lines - 1] + [(tr("more", n=len(lines) - max_lines + 1), None)]
        return lines

    # ---------- страница «настройки» (шестерёнка): подвкладки звук / интерфейс / о программе ----------
    def build_settings(self, page):
        subs = ("sound", "ui", "overlay", "about")
        Segmented(page, subs, self.sub, self.show_sub, seg_w=94, h=32,
                  labels=[tr("set_" + s) for s in subs]).pack(pady=(0, 8))
        holder = ctk.CTkFrame(page, fg_color="transparent")
        holder.pack(fill="both", expand=True)
        self.subholder = holder
        self.subpages = {s: ctk.CTkFrame(holder, fg_color="transparent") for s in subs}
        for sp in self.subpages.values():
            sp.place(relx=0, rely=0, relwidth=1, relheight=1)     # стопкой, как и главные страницы
        self.build_sound(self.subpages["sound"])
        self.build_ui(self.subpages["ui"])
        self.build_overlay(self.subpages["overlay"])
        self.build_about(self.subpages["about"])
        self.stack_show(self.subpages[self.sub])
        self.sub_ready = True

    def show_sub(self, name):
        old = self.sub
        self.sub = name
        self.switch_page(self.subholder, self.subpages[name], "sub:" + name, animate=old != name)

    def build_sound(self, page):
        right = pill(page, tr("click"), tr("click_sub"), height=46)
        Segmented(right, ("off", "on"), "on" if sound["on"] else "off",
                  lambda v: sound.__setitem__("on", v == "on")).pack()
        right = pill(page, tr("enter_cs2"), tr("enter_cs2_sub"), height=46)
        Segmented(right, ("off", "on"), "on" if sound["cs2"] else "off",
                  lambda v: sound.__setitem__("cs2", v == "on")).pack()
        self._slider(page, tr("volume"), 0, 100, "volume")
        self._slider(page, tr("pitch"), 50, 200, "pitch")
        right = pill(page, tr("style"), height=46)
        keycap(right, "▶", lambda: play_click(True, force=True), width=34).pack(side="right", padx=(6, 0))
        Segmented(right, tuple(STYLES), sound["style"], self.on_style, seg_w=64,
                  labels=[tr(s) for s in STYLES]).pack(side="right")

    def build_ui(self, page):
        right = pill(page, tr("language"), height=46)
        Segmented(right, tuple(TR), LANG, self.on_lang, seg_w=54,
                  labels=[LANG_LABELS[l] for l in TR]).pack()

        right = pill(page, tr("accent"), height=46)                 # кружки с цветами
        for name in reversed(list(ACCENTS)):
            color = ACCENTS[name]["ACCENT"]
            chosen = name == prefs["accent"]
            ctk.CTkButton(right, text="", width=22, height=22, corner_radius=11,
                          fg_color=color, hover_color=mix(color, "#ffffff", 0.25),
                          border_width=2 if chosen else 0, border_color=TEXT,
                          command=lambda n=name: self.on_accent(n)).pack(side="right", padx=3)

        right = pill(page, tr("steam_key"), tr("steam_key_sub"), height=46)
        keycap(right, "?", lambda: webbrowser.open(KEY_PAGE), width=32).pack(side="right", padx=(6, 0))
        key_entry = ctk.CTkEntry(right, width=150, height=30, corner_radius=15, fg_color=KEYCAP,
                                 border_color=KEYCAP, text_color=TEXT, show="•", font=font(12))
        key_entry.insert(0, prefs["steam_key"])
        key_entry.bind("<KeyRelease>", lambda e: prefs.__setitem__("steam_key", key_entry.get().strip()))
        key_entry.pack(side="right")

        right = pill(page, tr("tray"), tr("tray_sub"), height=46)
        Segmented(right, ("off", "on"), "on" if prefs["tray"] else "off",
                  lambda v: prefs.__setitem__("tray", v == "on")).pack()

        right = pill(page, tr("reset"), tr("reset_sub"), height=46)
        self.reset_btn = keycap(right, tr("reset_btn"), self.on_reset, width=100)
        self.reset_btn.pack()

    def build_overlay(self, page):
        right = pill(page, tr("overlay"), tr("overlay_sub"), height=46)
        Segmented(right, ("off", "on"), "on" if prefs["overlay"] else "off",
                  self.on_overlay_switch).pack()

        right = pill(page, tr("position"), height=52)
        reset_btn(right, self.reset_overlay_pos).pack(side="right", padx=(4, 0))
        self.move_btn = keycap(right, tr("move"), self.toggle_move, width=110)
        self.move_btn.pack(side="right")
        self.move_hint = ctk.CTkLabel(page, text="", text_color=MUTED, font=font(10))
        self.move_hint.pack(pady=(6, 0))

    def on_overlay_switch(self, v):
        prefs["overlay"] = v == "on"
        if prefs["overlay"]:
            self.preview_overlay()            # сразу показываем, где он будет
        else:
            self.overlay_edit = False

    # ---------- перемещение оверлея мышкой ----------
    def toggle_move(self):
        if self.overlay_edit:
            self.finish_move()
            return
        prefs["overlay"] = True               # чтобы было что таскать
        self.overlay_edit = True

    def finish_move(self):
        self.overlay_edit = False
        self.preview_overlay()                # покажем ещё пару секунд на новом месте

    def reset_overlay_pos(self):
        prefs["overlay_xy"] = None
        self.preview_overlay()

    def preview_overlay(self):
        """Выбрали место оверлея — показываем его там на пару секунд, даже вне CS2."""
        self.preview_until = self.t + 2.5

    def build_about(self, page):
        box = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=24)
        box.pack(fill="x", pady=4)
        ctk.CTkLabel(box, text="skywalk", text_color=TEXT, height=34,
                     font=font(26, "bold", "Segoe UI Black")).pack(pady=(16, 0))
        ctk.CTkLabel(box, text=VERSION, text_color=ACCENT, height=18, font=font(12)).pack(pady=(0, 14))

        right = pill(page, tr("author"), height=46)
        ctk.CTkLabel(right, text="blessed", text_color=TEXT, font=font(13, "bold")).pack(padx=10)

        right = pill(page, "telegram", height=46)
        keycap(right, "@nonelyyo", lambda: webbrowser.open("https://t.me/nonelyyo"), width=120).pack()

        right = pill(page, tr("log"), tr("log_sub"), height=46)
        keycap(right, tr("save"), save_log, width=110).pack()

        right = pill(page, tr("changes"), tr("changes_sub"), height=46)
        keycap(right, tr("open"), self.open_changes, width=110).pack()
        self.changes = None                   # открытая панель «что нового»

    def open_changes(self):
        """Панель «что нового» на всю страницу «о программе» — плавно проявляется поверх неё."""
        self.close_changes()
        page = self.subpages["about"]
        under = grab_widget(page) if self.is_front() else None
        panel = ChangesPanel(self, page)
        panel.place(x=0, y=0, relwidth=1, relheight=1)
        panel.lift()
        self.changes = panel
        if under is not None and under.size == panel.img.size:
            panel.under = under
            panel.photo.paste(under)
            self.card_fade(panel, under, panel.img, time.perf_counter(), None)
        else:
            panel.under = None

    def close_changes(self, animate=False):
        panel, self.changes = getattr(self, "changes", None), None
        if panel is None or not panel.winfo_exists():
            return
        if animate and panel.under is not None and self.is_front():
            panel.unbind("<Button-1>")
            panel.unbind("<MouseWheel>")
            self.card_fade(panel, panel.img, panel.under, time.perf_counter(), panel.destroy)
            return
        panel.destroy()

    def on_accent(self, name):
        prefs["accent"] = name
        set_accent(name)
        self.after(30, self.rebuild)          # перерисовываем всё новым цветом

    def on_reset(self):
        if not self.reset_armed:              # первое нажатие — просим подтвердить
            self.reset_armed = True
            self.reset_btn.configure(text=tr("reset_sure"), text_color=ACCENT)
            self.after(3000, self.disarm_reset)
            return
        self.reset_armed = False
        reset_settings()
        self.sub = "sound"
        self.rebuild()

    def disarm_reset(self):
        if self.reset_armed and self.reset_btn.winfo_exists():
            self.reset_armed = False
            self.reset_btn.configure(text=tr("reset_btn"), text_color=TEXT)

    def _slider(self, page, text, lo, hi, key):
        right = pill(page, text, height=46)
        val = ctk.CTkLabel(right, text=f"{sound[key]}%", text_color=MUTED, width=42, font=font(12))
        val.pack(side="right")

        def changed(v):
            sound[key] = int(v)
            val.configure(text=f"{int(v)}%")
            rebuild_sounds()

        sl = ctk.CTkSlider(right, from_=lo, to=hi, number_of_steps=hi - lo, width=150, height=14,
                           command=changed, progress_color=ACCENT_DIM, fg_color=KEYCAP,
                           button_color="#e0e0e4", button_hover_color="#ffffff")
        sl.set(sound[key])
        sl.pack(side="right", padx=8)

    def on_style(self, value):
        sound["style"] = value
        rebuild_sounds()
        play_click(True, force=True)

    def on_lang(self, lang):
        global LANG
        LANG = lang
        self.after(220, self.rebuild)        # даём переключателю доехать, потом меняем надписи

    # ---------- вкладки: страницы лежат стопкой, нужная поднимается наверх ----------
    def show_tab(self, name):
        if name == self.tab:
            return
        if getattr(self, "jump_box", None) is not None:
            self.hide_jump()                  # карточка прыжка — только на вкладке «рекорды»
        first = self.tab is None
        self.tab = name
        if name == "maps" and prefs["steam_key"] and time.time() - watcher.checked > 60:
            watcher.refresh()                 # данные устарели — подгружаем свежие
        page = self.pages[name]

        def apply():
            self.stack_show(page)
            if name == "maps":
                self.show_watch()             # кнопки регионов достраиваются тут — под снимком старой вкладки
        self.switch_page(self.holder, page, None, animate=not first, apply=apply)
        if name == self.SETTINGS:
            self.tabs.hide()                  # в настройках ни одна вкладка не выбрана
        self.gear.set_active(name == self.SETTINGS)

    def stack_show(self, page):
        """Показать страницу из стопки (вкладки или подвкладки настроек), а остальные — убрать совсем.
        Раньше все страницы лежали стопкой и просто перекрывали друг друга. Но Tk считает их все
        видимыми и при каждом показе окна (из трея, после «свернуть») перерисовывал ВСЕ — 0,5 с
        ожидания. Теперь «живая» только открытая страница: показ окна ~0,07 с."""
        group = self.pages if page in self.pages.values() else self.subpages
        page.place(relx=0, rely=0, relwidth=1, relheight=1)
        page.lift()
        for other in group.values():
            if other is not page:
                other.place_forget()

    # ---------- плавный переход: старая страница «растворяется» в новую ----------
    def switch_page(self, container, page, key, animate=True, apply=None):
        """1) снимок старой страницы кладём поверх  2) под ним поднимаем новую (или apply() меняет вид)
        3) снимок плавно перетекает в запомненный вид новой  4) убираем снимок — под ним настоящая.
        key=None — снимок главной области (имя берём из page_key уже ПОСЛЕ смены)."""
        self.finish_fade()
        old_img = grab_widget(container) if animate and self.is_front() else None
        if old_img is not None:               # снимок кладём поверх СРАЗУ: пока под ним всё перестраивается,
            photo = ImageTk.PhotoImage(old_img)   # человек видит старую страницу целиком, а не куски новой
            cover = tk.Label(container, image=photo, bd=0, highlightthickness=0, bg=BG)
            cover.place(x=0, y=0, relwidth=1, relheight=1)
            cover.lift()
        if apply is not None:
            apply()
        else:
            self.stack_show(page)
        if key is None:
            key = self.page_key()
        if old_img is not None:
            cover.lift()                      # новая страница поднялась — снимок снова наверх
            # (страница, которую только что показали, рисуется с нуля — это ~30–100 мс, но под снимком)
            self.update_idletasks()           # пусть всё тяжёлое (раскладка, перерисовка) случится ДО начала
                                              # перехода — иначе первые кадры растворения «проскочат»
        if old_img is None:                   # снять не получилось — просто переключаем
            self.after(300, self.remember, container, key)
            return
        new_img = self.snapshots.get(key)
        if new_img is None or new_img.size != old_img.size:
            new_img = Image.new("RGB", old_img.size, BG)   # новую ещё не видели — растворяемся в фон
        # снимок лежит в обычном tk.Label: одна картинка, в которую каждый кадр копируем пиксели
        self.fade = dict(cover=cover, photo=photo, a=old_img, b=new_img, start=time.perf_counter(),
                         container=container, key=key)
        self.after(FRAME_MS, self.fade_step)

    def fade_step(self):
        f = self.fade
        if f is None:
            return
        k = min(1.0, (time.perf_counter() - f["start"]) / 0.2)        # переход длится 0,2 сек
        if k >= 1.0:
            self.finish_fade()
            return
        f["photo"].paste(Image.blend(f["a"], f["b"], smooth(k)))
        self.after(FRAME_MS, self.fade_step)

    def finish_fade(self):
        """Убираем снимок — под ним уже настоящая новая страница."""
        f, self.fade = self.fade, None
        if f is None:
            return
        f["cover"].destroy()
        self.after(250, self.remember, f["container"], f["key"])   # запоминаем свежий вид страницы

    def remember(self, container, key):
        """Запоминаем, как выглядит страница, — для следующего перехода на неё."""
        current = self.page_key() if not key.startswith("sub:") else "sub:" + self.sub
        if key == current and self.fade is None and self.is_front():
            img = grab_widget(container)
            if img is not None:
                self.snapshots[key] = img

    def is_front(self):
        """Наше окно сейчас активно и видно (иначе на снимок попадут чужие окна)."""
        try:
            return self.state() == "normal" and user32.GetForegroundWindow() == hwnd_of(self)
        except Exception:
            return False

    def start_capture(self, target):
        state["capturing"] = None if state["capturing"] == target else target

    def _set(self, widget, key, **kw):
        if self._last.get(key) != kw:
            self._last[key] = kw
            widget.configure(**kw)

    # ---------- обновление ~60 раз в секунду ----------
    def refresh(self):
        try:
            self.tick()
        except Exception:                     # ошибка не должна «убить» анимации — пишем в журнал
            import traceback
            log("ошибка в анимации:\n" + traceback.format_exc())
        try:
            delay = self.pick_frame_ms()
        except Exception:
            delay = FRAME_MS
        try:
            self.after(delay, self.refresh)
        except Exception:                     # окно уже закрыли (выход из трея)
            pass

    def pick_frame_ms(self):
        """Режим экономии: как часто крутить главный цикл.
        Окно открыто и мы не в игре — с частотой монитора (анимации плавные).
        Окно в трее/свёрнуто или играем в CS2 — смотреть на окно некому:
          оверлей виден или плавно появляется/прячется — 30 кадров в секунду (точке хватит),
          иначе — 10 раз в секунду (только трей, слежка за картами и сохранение)."""
        if not self.winfo_exists():
            return FRAME_MS
        hidden = self.state() in ("withdrawn", "iconic")
        if not hidden and not self.cs2_active:
            return FRAME_MS
        ov = self.overlay
        if ov is not None and (ov.alpha > 0 or self.overlay_edit or self.t < self.preview_until
                               or self.cs2_active):
            return max(FRAME_MS, 33)          # ≤30 кадров в секунду
        return 100                            # 10 раз в секунду

    def tick(self):
        self.handle_tray()
        if not self.winfo_exists():
            return
        update_mouse_hook()                   # мышь перехватываем только когда она нужна для -w
        # окно тащат: ничего не перерисовываем, иначе Windows закрашивает элементы чёрным.
        # Как только левая кнопка мыши отпущена — продолжаем как обычно.
        if self.dragging:
            if user32.GetAsyncKeyState(0x01) & 0x8000:
                return
            self.dragging = False
        now = time.perf_counter()
        dt = min(0.05, now - self.last_time)  # сколько секунд прошло с прошлого кадра
        self.last_time = now
        self.t += dt
        pulse = (math.sin(self.t * 2.4) + 1) / 2
        blink = (math.sin(self.t * 8) + 1) / 2
        on = state["enabled"]
        tw = self.tw

        # свечение: при включении — вспышка, потом «дышит»; при выключении плавно гаснет
        if on and not self.was_on:
            self.burst = 0.35                 # вспышка свечения при включении
        if on != self.was_on and self.tray is not None:
            self.tray.update_menu()           # обновить галочку «включена» в меню трея
        self.was_on = on

        # оверлей: создаём/убираем по переключателю, обновляем статус
        if prefs["overlay"] and self.overlay is None:
            self.overlay = Overlay(self)
        elif not prefs["overlay"] and self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        # активно ли окно CS2 (~6 раз в секунду) — нужно и оверлею, и режиму экономии
        if now >= self.next_cs2:
            self.next_cs2 = now + 0.15
            was = self.cs2_active
            self.cs2_active = "Counter-Strike 2" in active_window_title()
            # переключился в игру, а программа включена — короткий щелчок: «нуллы работают»
            if self.cs2_active and not was and state["enabled"] and sound["cs2"]:
                play_click(True, force=True)
            if self.cs2_active and not was:
                refresh_cs2_binds()
        if self.overlay is not None:
            # «дыхание» точки округляем до 20 ступенек: так готовых картинок плашки всего
            # пара десятков, они берутся из запаса и не рисуются заново каждый кадр
            p = round(pulse * 20) / 20
            self.overlay.set(f"skywalk · {'on' if on else 'off'}",
                             mix(ACCENT_DIM, ACCENT, p) if on else MUTED, TEXT if on else MUTED)
            if self.overlay.editing != self.overlay_edit:
                self.overlay.set_edit(self.overlay_edit)
            visible = self.cs2_active or self.t < self.preview_until or self.overlay_edit
            self.overlay.fade_to(0.95 if self.overlay_edit else 0.85 if visible else 0.0, dt)
        if self.tab == self.SETTINGS and self.sub == "overlay":      # кнопка и подсказка на странице
            self._set(self.move_btn, "movebtn", text=tr("done") if self.overlay_edit else tr("move"))
            tw.to(self.move_btn, "border_color", ACCENT if self.overlay_edit else KEYCAP)
            self._set(self.move_hint, "movehint", text=tr("moving_sub") if self.overlay_edit else tr("move_sub"))
        self.burst *= 0.93 ** (dt * 60)
        target = (0.38 + 0.2 * pulse if on else 0.1) + self.burst
        self.glow += (target - self.glow) * ease(0.12, dt)
        hidden = self.state() in ("withdrawn", "iconic")  # окно в трее или свёрнуто — не тратим силы на анимации
        if now >= self.next_title and not hidden and self.fade is None:  # название — до 60 раз в секунду
            self.next_title = now + 1 / 60
            waves = [(math.sin(self.t * 3.2 - i * 0.9) + 1) / 2 for i in range(self.title_img.shine)]
            self.title_img.draw(self.glow, [shimmer(w) for w in waves])

        if on:
            self._set(self.status, "st", text="on")
            tw.to(self.status, "text_color", TEXT)
            self.dot.configure(text_color=mix(ACCENT_DIM, ACCENT, pulse))
        else:
            self._set(self.status, "st", text="off")
            tw.to(self.status, "text_color", MUTED)
            self.dot.configure(text_color=MUTED)
        self._set(self.hint, "hint", text=tr("hint", key=state["toggle_name"].lower()))

        for btn, target_name in self.bind_btns:
            text = self.bind_text(target_name)
            if state["capturing"] == target_name:
                self._set(btn, target_name, text=tr("waiting"))
                tw.forget(btn, "border_color")
                btn.configure(border_color=mix(KEYCAP, ACCENT, blink))
                tw.to(btn, "text_color", ACCENT)
            else:
                self._set(btn, target_name, text=text)
                tw.to(btn, "border_color", KEYCAP)
                tw.to(btn, "text_color", TEXT if text != tr("assign") and not text.endswith("—") else MUTED)
        # рекорды: дочитываем консоль CS2 два раза в секунду
        if now >= self.next_records or record_watcher.behind:
            self.next_records = now + 0.5
            try:                              # не больше ~8 мс за кадр: большой файл дочитается за пару секунд,
                record_watcher.poll(0.008)    # зато окно не «замирает» (раньше при запуске — на 1,5 с)
            except Exception as e:
                log(f"рекорды: {e!r}")
            if record_watcher.new:
                abbr, dist, prev = record_watcher.new[-1]   # несколько сразу (редко) — показываем последний
                record_watcher.new.clear()
                play_chime()
                self.rec_toast.show_record(abbr, dist, prev, prefs["records"].get(abbr, {}))
            # пока файл дочитывается кусками — вкладку обновляем не каждый кадр, а раз в полсекунды
            if self.tab == "records" and (not record_watcher.behind or now >= self.next_rec_ui):
                self.next_rec_ui = now + 0.5
                self.show_records()
        if self.tab == "binds":                   # какие клавиши CS2 программа будет жать
            if cs2_binds["files"][1]:
                status = tr("cs2_found", jump=", ".join(cs2_binds["jump"]).lower(),
                            duck=", ".join(cs2_binds["duck"]).lower())
            else:
                status = tr("cs2_none")
            self._set(self.cs2_label, "cs2", text=status)

        tw.step(dt)
        if not hidden:
            self.gear.step(dt)                # шестерёнка: цвет и поворот

        # скачалась база карт CS2KZ — добавляем к известным
        if fetch_kz_maps.result:
            prefs["known_maps"] = sorted(set(prefs["known_maps"]) | fetch_kz_maps.result)[:3000]
            prefs["maps_synced"] = time.time()
            fetch_kz_maps.result = None

        # слежка за картами: проверяем раз в 45 сек, после проверки — обновляем вкладку и оповещаем
        if prefs["watch_on"] and not watcher.busy and time.time() - watcher.checked >= WATCH_EVERY:
            watcher.refresh()
        if watcher.version != self.seen_version:
            self.seen_version = watcher.version
            new_maps = set(watcher.maps()) - set(prefs["known_maps"])
            if new_maps:                      # запоминаем карты — чтобы искать и те, которых сейчас нет
                prefs["known_maps"] = sorted(set(prefs["known_maps"]) | new_maps)[:3000]
            self.check_found()
            self.show_watch()
        elif now >= self.next_watch_ui:        # раз в секунду обновляем «… сек назад»
            self.next_watch_ui = now + 1.0
            self.show_watch()

        # раз в секунду освежаем снимок открытой страницы — чтобы переход на неё потом
        # шёл к её настоящему виду и в конце ничего не «прыгало»
        if now >= self.next_snap and self.fade is None and not hidden and self.sugg is None:
            self.next_snap = now + 1.0
            if self.tab == self.SETTINGS:
                self.remember(self.subholder, "sub:" + self.sub)
                self.remember(self.holder, self.SETTINGS)
            else:
                self.remember(self.holder, self.page_key())

        # раз в ~секунду: если настройки поменялись — сохраняем в файл
        if now >= self.next_save:
            self.next_save = now + 1.0
            now = snapshot()
            if now != self.saved:
                save_settings(now)
                self.saved = now

    # ---------- трей ----------
    def start_tray(self):
        if pystray is None:
            return
        put = self.tray_cmds.put
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: tr("tray_open"), lambda: put("show"), default=True),
            pystray.MenuItem(lambda item: tr("tray_toggle"), lambda: put("toggle"),
                             checked=lambda item: state["enabled"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: tr("tray_exit"), lambda: put("exit")),
        )
        self.tray = pystray.Icon("skywalk", tray_image(), "Skywalk", menu)
        self.tray.run_detached()              # иконка живёт в своём потоке, окно — в своём

    def handle_tray(self):
        """Выполняем команды из трея уже в потоке окна (так Tk работает без сбоев)."""
        while True:
            try:
                cmd = self.tray_cmds.get_nowait()
            except queue.Empty:
                return
            if cmd == "show":
                self.show_window()
            elif cmd == "toggle":
                toggle()
            elif cmd == "exit":
                self.quit_app()
                return

    def on_close_button(self):
        """Крестик: прячем в трей (если включено), иначе закрываем совсем."""
        if prefs["tray"] and self.tray is not None:
            save_settings(snapshot())
            self.withdraw()
            if not self.told_tray:
                self.told_tray = True
                try:
                    self.tray.notify(tr("tray_hint"), "Skywalk")
                except Exception:
                    pass
        else:
            self.quit_app()

    # ---------- плавное появление окна ----------
    def first_show(self):
        """Первый показ: окно собрано (спрятанным). Делаем его невидимым, показываем, даём дорисоваться,
        скругляем углы — и только потом плавно проявляем."""
        self.update_idletasks()
        set_window_alpha(self, 0.0)
        self.deiconify()
        self.update_idletasks()
        round_corners(self)                       # окно уже нужного размера — углы ставим сразу
        no_window_animation(self)
        self.fade_show()

    def fade_show(self, start=None):
        """Окно нарисовано целиком, но невидимое: проявляем его за 0,12 сек,
        потом выключаем «слоистый» режим (иначе чёрные блоки при перетаскивании)."""
        if start is None:
            self.update_idletasks()               # дорисовать всё, что ещё не успело (окно пока невидимое)
            start = time.perf_counter()           # отсчёт — после дорисовки, иначе проявление «проскочит»
        k = min(1.0, (time.perf_counter() - start) / 0.12)
        try:
            if k < 1.0:
                set_window_alpha(self, smooth(k))
                self.after(FRAME_MS, self.fade_show, start)
            else:
                set_window_alpha(self, 1.0)
                set_layered(self, False)
        except Exception as e:                    # окно уже закрыли — или что-то пошло не так (в журнал)
            log(f"появление окна: {e!r}")

    def minimize(self):
        """«Свернуть»: сворачиваем уже невидимым — при разворачивании окно не «собирается» на глазах."""
        set_window_alpha(self, 0.0)
        self.fade_on_map = True
        self.iconify()

    def on_map(self, e):
        if e.widget is self and self.fade_on_map and self.state() == "normal":
            self.fade_on_map = False
            self.fade_show()

    def show_window(self):
        if self.state() in ("withdrawn", "iconic"):   # из трея / свёрнутое: показываем невидимым → проявляем
            self.fade_on_map = False
            set_window_alpha(self, 0.0)
            self.deiconify()
            self.fade_show()                      # дорисовка — внутри (окно пока невидимое), сразу проявляем
        self.lift()
        self.focus_force()

    def quit_app(self):
        save_settings(snapshot())
        release_all()
        listener.stop()
        if mouse_listener is not None:
            mouse_listener.stop()
        if self.tray is not None:
            self.tray.stop()
        try:
            ctypes.windll.winmm.timeEndPeriod(1)   # возвращаем обычный таймер Windows
        except Exception:
            pass
        self.destroy()

    def on_close(self):
        self.quit_app()


# ================== Защита от двойного запуска ==================
# Две копии Skywalk сразу — обе перехватывают клавиши, и нуллы срабатывают дважды.
# Поэтому первая копия «занимает» имя в Windows (мьютекс). Вторая видит, что имя занято,
# просит первую показать окно (через событие Windows) и сразу закрывается.
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.restype = wintypes.HANDLE
SINGLE_MUTEX = "Local\\Skywalk_single_instance"
SHOW_EVENT = "Local\\Skywalk_show_window"


def already_running():
    """True — Skywalk уже запущена (и мы попросили её показать окно)."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, SINGLE_MUTEX)   # держим, пока программа работает
    if ctypes.get_last_error() != 183:                            # 183 = ERROR_ALREADY_EXISTS
        return False
    ev = kernel32.OpenEventW(0x0002, False, SHOW_EVENT)          # 0x0002 = право «поднять» событие
    if ev:
        kernel32.SetEvent(ev)
        kernel32.CloseHandle(ev)
    return True


def listen_show_requests(commands):
    """Поток первой копии: ждём просьбу второй копии и показываем окно (как «открыть» из трея)."""
    ev = kernel32.CreateEventW(None, False, False, SHOW_EVENT)    # сбрасывается сам после срабатывания
    while ev:
        if kernel32.WaitForSingleObject(ev, 0xFFFFFFFF) == 0:   # ждём бесконечно, 0 — дождались
            commands.put("show")


if __name__ == "__main__":
    if already_running():                      # вторая копия: первая покажет окно, а мы выходим
        sys.exit(0)
    setup_timing()                             # герцовка монитора + точный таймер
    load_settings()
    refresh_cs2_binds()                        # какие клавиши в CS2 прыжок / присед / cp / tp
    rebuild_sounds()                           # звук мог поменяться после загрузки настроек
    ctk.set_appearance_mode("dark")
    listener = keyboard.Listener(win32_event_filter=win_filter)
    listener.start()
    update_mouse_hook()                        # мышь перехватываем, только если -w на мыши
    import threading
    import traceback
    threading.excepthook = lambda a: write_crash(          # ошибки в потоках (клавиатура, мышь, трей)
        "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    try:
        app = App()
        threading.Thread(target=listen_show_requests, args=(app.tray_cmds,), daemon=True).start()
        app.mainloop()
    except Exception:                          # ошибка при запуске — тоже в crash.txt
        write_crash(traceback.format_exc())
        raise
