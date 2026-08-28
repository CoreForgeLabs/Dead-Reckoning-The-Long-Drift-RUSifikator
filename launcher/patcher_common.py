# -*- coding: utf-8 -*-
"""Shared plumbing for the standalone patcher executables: finding the
player's game install, backing up before touching it, and reading/writing the
Godot PCK format. Self-contained -- no dependency on the rest of this repo,
because this file (and its two patcher_*.py siblings) is exactly what gets
frozen into a distributable Windows executable via PyInstaller.
"""
import ctypes
import hashlib
import io
import os
import re
import shutil
import struct
import sys
import winreg

APP_ID = "4557340"
EXE_NAME_PATTERNS = (
    re.compile(r"^dead_reckoning.*windows\.exe$", re.I),
)


def setup_console():
    """Windows' console defaults to a legacy codepage (CP866/CP1251), not
    UTF-8 -- printing Cyrillic without this turns into mojibake, which is
    exactly what happened on first release of these tools."""
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            setattr(sys, stream_name, io.TextIOWrapper(
                stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))


setup_console()


def resource_path(*parts):
    """A file sitting next to the running executable -- NOT baked into the
    exe via PyInstaller's --add-data. Translation data lives on disk in a
    plain 'data' folder beside RussifierPatcher.exe, so it can be updated
    (new translation, fixed strings) by replacing those files, with no
    rebuild of the tool itself needed."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def pause_on_exit(msg="\nНажмите Enter для выхода..."):
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


def die(msg):
    print("\nОШИБКА: %s" % msg)
    pause_on_exit()
    sys.exit(1)


# --- Steam install discovery --------------------------------------------------

def _steam_install_path():
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")):
        try:
            with winreg.OpenKey(hive, key) as k:
                val, _ = winreg.QueryValueEx(k, "SteamPath" if hive == winreg.HKEY_CURRENT_USER else "InstallPath")
                if val and os.path.isdir(val):
                    return val
        except OSError:
            continue
    return None


def _library_folders(steam_path):
    """Every Steam library root, parsed out of libraryfolders.vdf (a simple
    quoted key/value format -- a tiny hand-rolled parser is enough, no need
    for a real VDF library for one field)."""
    libs = [steam_path]
    vdf = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    if not os.path.exists(vdf):
        return libs
    try:
        text = open(vdf, encoding="utf-8", errors="replace").read()
    except OSError:
        return libs
    for m in re.finditer(r'"path"\s*"([^"]+)"', text):
        p = m.group(1).replace("\\\\", "\\")
        if os.path.isdir(p):
            libs.append(p)
    return libs


def _manifest_installdir(library, app_id):
    manifest = os.path.join(library, "steamapps", "appmanifest_%s.acf" % app_id)
    if not os.path.exists(manifest):
        return None
    text = open(manifest, encoding="utf-8", errors="replace").read()
    m = re.search(r'"installdir"\s*"([^"]+)"', text)
    return m.group(1) if m else None


def find_game_exe_via_steam():
    """Best-effort auto-detect. Returns a path or None -- callers must have a
    manual fallback, since every install is different and this cannot be
    guaranteed to work."""
    steam = _steam_install_path()
    if not steam:
        return None
    for lib in _library_folders(steam):
        installdir = _manifest_installdir(lib, APP_ID)
        if not installdir:
            continue
        game_dir = os.path.join(lib, "steamapps", "common", installdir)
        if not os.path.isdir(game_dir):
            continue
        for fn in os.listdir(game_dir):
            if any(p.match(fn) for p in EXE_NAME_PATTERNS):
                return os.path.join(game_dir, fn)
    return None


def _find_exe_in_dir(d):
    """Search a folder (top level, then one level deeper) for the game's exe
    -- lets the player point at the game's install folder instead of having
    to find the exact .exe themselves."""
    try:
        top = os.listdir(d)
    except OSError:
        return None
    for fn in top:
        if any(p.match(fn) for p in EXE_NAME_PATTERNS):
            return os.path.join(d, fn)
    for fn in top:
        sub = os.path.join(d, fn)
        if not os.path.isdir(sub):
            continue
        try:
            for fn2 in os.listdir(sub):
                if any(p.match(fn2) for p in EXE_NAME_PATTERNS):
                    return os.path.join(sub, fn2)
        except OSError:
            continue
    return None


def prompt_for_exe():
    print("Не удалось найти игру автоматически.")
    print("Перетащите .exe игры (или папку с игрой) в это окно, либо вставьте")
    print("путь к нему/к ней, затем нажмите Enter:")
    while True:
        raw = input("> ").strip().strip('"')
        if not raw:
            continue
        if os.path.isdir(raw):
            found = _find_exe_in_dir(raw)
            if found:
                print("Найден: %s" % found)
                return found
            print("В папке %r не нашлось подходящего .exe -- попробуйте ещё раз." % raw)
            continue
        if os.path.isfile(raw) and raw.lower().endswith(".exe"):
            return raw
        print("Это не файл, не .exe и не папка: %r -- попробуйте ещё раз." % raw)


def locate_game_exe():
    found = find_game_exe_via_steam()
    if found:
        print("Найдено через Steam: %s" % found)
        ans = input("Использовать этот файл? [Y/n] ").strip().lower()
        if ans in ("", "y", "yes", "д", "да"):
            return found
    return prompt_for_exe()


# --- Godot project.binary (ECFG) ---------------------------------------------

def parse_project_binary(data):
    assert data[:4] == b"ECFG", "not a project.binary"
    count = struct.unpack_from("<I", data, 4)[0]
    pos, entries = 8, []
    for _ in range(count):
        klen = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        raw_key = data[pos:pos + klen]
        pos += klen
        vlen = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        value = data[pos:pos + vlen]
        pos += vlen
        entries.append([raw_key.split(b"\x00")[0].decode("utf-8"), value, raw_key])
    return entries


def serialize_project_binary(entries):
    out = bytearray(b"ECFG")
    out += struct.pack("<I", len(entries))
    for _key, value, raw_key in entries:
        out += struct.pack("<I", len(raw_key)) + raw_key
        out += struct.pack("<I", len(value)) + value
    return bytes(out)


def pb_get(entries, key):
    for e in entries:
        if e[0] == key:
            return e[1]
    return None


def pb_set(entries, key, value):
    for e in entries:
        if e[0] == key:
            e[1] = value
            return True
    return False


PACKED_STRING_ARRAY = 34


def pb_decode_string_array(value):
    vtype, count = struct.unpack_from("<II", value, 0)
    assert vtype == PACKED_STRING_ARRAY, vtype
    pos, out = 8, []
    for _ in range(count):
        n = struct.unpack_from("<I", value, pos)[0]
        pos += 4
        out.append(value[pos:pos + n].split(b"\x00")[0].decode("utf-8"))
        pos += n + ((4 - n % 4) % 4)
    return out


def pb_encode_string_array(items):
    out = bytearray(struct.pack("<II", PACKED_STRING_ARRAY, len(items)))
    for s in items:
        b = s.encode("utf-8") + b"\x00"
        out += struct.pack("<I", len(b)) + b + b"\x00" * ((4 - len(b) % 4) % 4)
    return bytes(out)


def backup(path):
    """Keep exactly one pristine backup per file -- never overwrite an
    existing backup, since the second run's 'pristine' copy is already
    patched and would poison the backup forever."""
    bak = path + ".backup"
    if not os.path.exists(bak):
        print("Резервная копия оригинала: %s" % bak)
        shutil.copy2(path, bak)
    else:
        print("Резервная копия уже существует: %s (не перезаписана)" % bak)
    return bak


# --- Godot embedded PCK (GDPC) -------------------------------------------------

HEADER_LEN = 112


def read_pck(path):
    with open(path, "rb") as f:
        data = bytearray(f.read())
    if data[-4:] != b"GDPC":
        raise ValueError("%s не заканчивается сигнатурой GDPC -- это не "
                         "самодостаточный exe Godot, либо файл уже повреждён" % path)
    pck_len = struct.unpack("<Q", data[-12:-4])[0]
    pck_start = len(data) - pck_len - 12
    header = bytearray(data[pck_start:pck_start + HEADER_LEN])
    fbase = struct.unpack("<Q", header[24:32])[0]
    dir_offset = struct.unpack("<Q", header[32:40])[0]

    pos = pck_start + dir_offset
    count = struct.unpack("<I", data[pos:pos + 4])[0]
    pos += 4
    files = []
    for _ in range(count):
        plen = struct.unpack("<I", data[pos:pos + 4])[0]
        raw_path = bytes(data[pos + 4:pos + 4 + plen])
        pos += 4 + plen
        offset, size = struct.unpack("<QQ", data[pos:pos + 16])
        pos += 16 + 16 + 4                              # offset+size, md5, flags (skip stored values)
        flags = struct.unpack("<I", data[pos - 4:pos])[0]
        abs_off = pck_start + fbase + offset
        files.append({
            "path": raw_path.decode("utf-8", "replace").rstrip("\x00"),
            "raw_pbytes": raw_path,
            "flags": flags,
            "data": bytes(data[abs_off:abs_off + size]),
        })
    return bytes(data[:pck_start]), header, files


def write_pck(out_path, exe_prefix, header, files):
    blob = bytearray()
    entries = []
    for f in files:
        blob.extend(b"\x00" * ((64 - (len(blob) % 64)) % 64))
        entries.append({
            "raw_pbytes": f["raw_pbytes"],
            "offset": len(blob),
            "size": len(f["data"]),
            "md5": hashlib.md5(f["data"]).digest(),
            "flags": f["flags"],
        })
        blob.extend(f["data"])
    blob.extend(b"\x00" * ((64 - (len(blob) % 64)) % 64))

    dir_bytes = bytearray(struct.pack("<I", len(entries)))
    for e in entries:
        dir_bytes += struct.pack("<I", len(e["raw_pbytes"])) + e["raw_pbytes"]
        dir_bytes += struct.pack("<QQ", e["offset"], e["size"])
        dir_bytes += e["md5"]
        dir_bytes += struct.pack("<I", e["flags"])

    header = bytearray(header)
    struct.pack_into("<Q", header, 24, HEADER_LEN)
    struct.pack_into("<Q", header, 32, HEADER_LEN + len(blob))
    pck = bytes(header) + bytes(blob) + bytes(dir_bytes)

    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(exe_prefix)
        f.write(pck)
        f.write(struct.pack("<Q", len(pck)) + b"GDPC")
    os.replace(tmp, out_path)                            # atomic on the same volume
