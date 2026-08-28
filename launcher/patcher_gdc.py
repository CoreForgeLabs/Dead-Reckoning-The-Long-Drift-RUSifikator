# -*- coding: utf-8 -*-
"""Patch English text baked directly into compiled GDScript (.gdc) bytecode --
literals that never reach TranslationServer at all. See scripts/gdc_literals.py
in the Russifier project for the full account of why (BBCode templates with
words baked in, and the colonist name pool used as dictionary keys) and how
the format was verified (byte-exact match against 5 real entries, a live
negative-control test on the shipped executable).
"""
import json
import struct

import zstandard as zstd


def encode_variant_string(s):
    b = s.encode("utf-8")
    pad = (4 - len(b) % 4) % 4
    return struct.pack("<II", 4, len(b)) + b + b"\x00" * pad


def decompress_gdc(data):
    if data[:4] != b"GDSC":
        return None
    size = struct.unpack_from("<I", data, 8)[0]
    return zstd.ZstdDecompressor().decompress(data[12:], max_output_size=size)


def recompress_gdc(original_header8, decompressed):
    comp = zstd.ZstdCompressor(level=3).compress(decompressed)
    return original_header8 + struct.pack("<I", len(decompressed)) + comp


def patch_file(data, pairs):
    dec = decompress_gdc(data)
    for old_text, new_text in pairs:
        old_bytes = encode_variant_string(old_text)
        count = dec.count(old_bytes)
        if count != 1:
            raise RuntimeError("ожидалось ровно 1 совпадение для %r, найдено %d -- "
                              "версия игры отличается от той, под которую "
                              "собран этот патч" % (old_text[:60], count))
        new_bytes = encode_variant_string(new_text)
        dec = dec.replace(old_bytes, new_bytes, 1)
    return recompress_gdc(data[:8], dec)


def check_no_collisions(fixes):
    by_path = {}
    for f in fixes:
        by_path.setdefault(f["file"], {}).setdefault(f["ru"], []).append(f["en"])
    problems = []
    for path, targets in by_path.items():
        for ru, sources in targets.items():
            if len(set(sources)) > 1:
                problems.append((path, ru, sources))
    if problems:
        raise RuntimeError("%d коллизий в данных патча -- применение "
                           "отменено: %r" % (len(problems), problems[:3]))


def apply_all(files, fixes_path):
    fixes = json.load(open(fixes_path, encoding="utf-8"))
    check_no_collisions(fixes)
    by_path = {}
    for f in fixes:
        by_path.setdefault(f["file"], []).append((f["en"], f["ru"]))

    patched = 0
    for f in files:
        if f["path"] not in by_path:
            continue
        f["data"] = patch_file(f["data"], by_path[f["path"]])
        patched += len(by_path[f["path"]])
    return patched
