# -*- coding: utf-8 -*-
"""Reader/writer for Godot 4 OptimizedTranslation (.translation) resources.

The game loads locale/strings.<lang>.translation, NOT locale/strings.csv --
the CSV is only an editor-side import source. This module lets the russifier
write a real compiled resource.

Format verified empirically against the shipped strings.en.translation
(Godot 4.6): hash_table len == Math::larger_prime(1317) == 1543, and
bucket_table len == 2*non_empty_buckets + 4*key_count.
"""
import struct

# --- Godot core/math/math_funcs.h : Math::larger_prime table -----------------
_PRIMES = (5, 13, 23, 47, 97, 193, 389, 769, 1543, 3079, 6151, 12289, 24593,
           49157, 98317, 196613, 393241, 786433, 1572869, 3145739, 6291469,
           12582917, 25165843, 50331653, 100663319, 201326611, 402653189,
           805306457, 1610612741)


def larger_prime(n):
    for p in _PRIMES:
        if p > n:
            return p
    raise ValueError("no prime large enough for %d" % n)


def gd_hash(d, data):
    """OptimizedTranslation::hash -- FNV-1a-ish over the UTF-8 bytes.

    Godot does `d = (d * 0x1000193) ^ uint32_t(*p_str)` where *p_str is a
    signed char, so bytes >= 0x80 are sign-extended before the xor.
    """
    if d == 0:
        d = 0x1000193
    for b in data:
        if b >= 0x80:
            b = (b - 0x100) & 0xFFFFFFFF   # sign extension to uint32
        d = ((d * 0x1000193) & 0xFFFFFFFF) ^ b
        d &= 0xFFFFFFFF
    return d


# --- smaz -------------------------------------------------------------------
SMAZ_RCB = [
    " ", "the", "e", "t", "a", "of", "o", "and", "i", "n", "s", "e ", "r", " th",
    " t", "in", "he", "th", "h", "he ", "to", "\r\n", "l", "s ", "d", " a", "an",
    "er", "c", " o", "d ", "on", " of", "re", "of ", "t ", ", ", "is", "u", "at",
    "   ", "n ", "or", "which", "f", "m", "as", "it", "that", "\n", "was", "en",
    "  ", " w", "es", " an", " i", "\r", "f ", "g", "p", "nd", " s", "nd ", "ed ",
    "w", "ed", "http://", "for", "te", "ing", "y ", "The", " c", "ti", "r ", "his",
    "st", " in", "ar", "nt", ",", " to", "y", "ng", " h", "with", "le", "al", "to ",
    "b", "ou", "be", "were", " b", "se", "o ", "ent", "ha", "ng ", "their", '"',
    "hi", "from", " f", "in ", "de", "ion", "me", "v", ".", "ve", "all", "re ",
    "ri", "ro", "is ", "co", "f t", "are", "ea", ". ", "her", " m", "er ", " p",
    "es ", "by", "they", "di", "ra", "ic", "not", "s, ", "d t", "at ", "ce", "la",
    "h ", "ne", "as ", "tio", "on ", "n t", "io", "we", " a ", "om", ", a", "s o",
    "ur", "li", "ll", "ch", "had", "this", "e t", "g ", "e\r\n", " wh", "ere",
    " co", "e o", "a ", "us", " d", "ss", "\n\r\n", "\r\n\r", '="', " be", " e",
    "s a", "ma", "one", "t t", "or ", "but", "el", "so", "l ", "e s", "s,", "no",
    "ter", " wa", "iv", "ho", "e a", " r", "hat", "s t", "ns", "ch ", "wh", "tr",
    "ut", "/", "have", "ly ", "ta", " ha", " on", "tha", "-", " l", "ati", "en ",
    "pe", " re", "there", "ass", "si", " fo", "wa", "ec", "our", "who", "its", "z",
    "fo", "rs", ">", "ot", "un", "<", "im", "th ", "nc", "ate", "><", "ver", "ad",
    " we", "ly", "ee", " n", "id", " cl", "ac", "il", "</", "rt", " wi", "div",
    "e, ", " it", "whi", " ma", "ge", "x", "e c", "men", ".com",
]
SMAZ_RCB_B = [s.encode("latin-1") for s in SMAZ_RCB]


def smaz_decompress(data):
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c == 254:                       # single verbatim byte
            out.append(data[i + 1]); i += 2
        elif c == 255:                     # verbatim run
            ln = data[i + 1] + 1
            out += data[i + 2:i + 2 + ln]; i += 2 + ln
        else:
            out += SMAZ_RCB_B[c]; i += 1
    return bytes(out)


# --- binary resource (RSRC) plumbing ----------------------------------------
V_NIL, V_STRING, V_PACKED_BYTE_ARRAY, V_PACKED_INT32_ARRAY = 1, 5, 31, 32
_PROPS = ["resource_local_to_scene", "resource_name", "messages", "locale",
          "plural_rules_override", "hash_table", "bucket_table", "strings", "script"]


class _R:
    def __init__(self, d):
        self.d = d
        self.p = 0

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def u64(self):
        v = struct.unpack_from("<Q", self.d, self.p)[0]
        self.p += 8
        return v

    def s(self):
        n = self.u32()
        v = self.d[self.p:self.p + n]
        self.p += n
        return v.split(b"\x00")[0].decode("utf-8")


def read_optimized_translation(blob):
    """Return the property dict of a .translation resource."""
    r = _R(blob)
    assert blob[:4] == b"RSRC", "not a binary Godot resource"
    r.p = 4
    r.u32(); r.u32(); r.u32(); r.u32(); r.u32()   # endian, real64, ver major/minor/patch
    rtype = r.s()
    assert rtype == "OptimizedTranslation", rtype
    r.u64()
    flags = r.u32()
    r.u64()                                        # uid
    if flags & 8:
        r.s()                                      # script class
    r.p += 4 * 11                                  # reserved
    nstr = r.u32()
    strtab = [r.s() for _ in range(nstr)]
    for _ in range(r.u32()):                       # external resources
        r.s(); r.s(); r.u64()
    internals = []
    for _ in range(r.u32()):
        internals.append((r.s(), r.u64()))
    r.p = internals[0][1]
    r.s()                                          # resource type again
    props = {}
    for _ in range(r.u32()):
        name = strtab[r.u32()]
        vt = r.u32()
        if vt == V_PACKED_INT32_ARRAY:
            n = r.u32()
            props[name] = struct.unpack_from("<%dI" % n, blob, r.p)
            r.p += n * 4
        elif vt == V_PACKED_BYTE_ARRAY:
            n = r.u32()
            props[name] = blob[r.p:r.p + n]
            r.p += n + ((4 - n % 4) % 4)
        elif vt == V_STRING:
            props[name] = r.s()
        elif vt == V_NIL:
            props[name] = None
        else:
            raise ValueError("unhandled variant %d for %s" % (vt, name))
    return props


def lookup(props, key):
    """Reimplementation of OptimizedTranslation::get_message."""
    ht = props.get("hash_table") or ()
    bt = props.get("bucket_table") or ()
    sb = props.get("strings") or b""
    if not ht:
        return None
    cs = key.encode("utf-8")
    p = ht[gd_hash(0, cs) % len(ht)]
    if p == 0xFFFFFFFF:
        return None
    size, func = bt[p], bt[p + 1]
    h = gd_hash(func, cs)
    for i in range(size):
        base = p + 2 + i * 4
        if bt[base] == h:
            off, comp, uncomp = bt[base + 1], bt[base + 2], bt[base + 3]
            raw = sb[off:off + comp] if comp == uncomp else smaz_decompress(sb[off:off + comp])
            return raw.split(b"\x00")[0].decode("utf-8", "replace")
    return None


def all_keys_present(props, keys):
    """Which of `keys` the resource can actually resolve."""
    return [k for k in keys if lookup(props, k) is not None]


def iter_entries(props):
    """Yield (bucket_index, slot_index, value) for every entry in the table."""
    ht, bt, sb = props["hash_table"], props["bucket_table"], props["strings"]
    for i, p in enumerate(ht):
        if p == 0xFFFFFFFF:
            continue
        n = bt[p]
        for j in range(n):
            b = p + 2 + j * 4
            off, comp, unc = bt[b + 1], bt[b + 2], bt[b + 3]
            raw = sb[off:off + comp] if comp == unc else smaz_decompress(sb[off:off + comp])
            yield i, j, raw.split(b"\x00")[0].decode("utf-8", "replace")


def rebuild_by_position(blob, values, locale, uid):
    """Rewrite a .translation's text by POSITION rather than by old value.

    Addressing an entry by the Russian it currently holds is the wrong anchor:
    the developer rewrites Russian in almost every update -- 585 of 1336 values changed in one of them -- and every
    entry whose old text moved is silently left untranslated. The English side
    changed 0 in the same update, and the two tables share a bucket layout
    entry for entry, so a position carries a stable identity.

    `values` is indexed by `iter_entries` order and may hold None for entries
    that keep their current text.
    """
    props = read_optimized_translation(blob)
    ht, bt = list(props["hash_table"]), list(props["bucket_table"])
    sb = props["strings"]

    strings_blob = bytearray()
    replaced = kept = 0
    idx = 0
    for i, p in enumerate(ht):
        if p == 0xFFFFFFFF:
            continue
        for j in range(bt[p]):
            b = p + 2 + j * 4
            off, comp, unc = bt[b + 1], bt[b + 2], bt[b + 3]
            raw = sb[off:off + comp] if comp == unc else smaz_decompress(sb[off:off + comp])
            old = raw.split(b"\x00")[0].decode("utf-8", "replace")
            new = values[idx] if idx < len(values) else None
            if new is None:
                new = old
                kept += 1
            else:
                replaced += 1
            idx += 1
            enc = new.encode("utf-8") + b"\x00"
            bt[b + 1] = len(strings_blob)
            bt[b + 2] = len(enc)
            bt[b + 3] = len(enc)
            strings_blob += enc
    return _serialize(ht, bt, bytes(strings_blob), locale, uid), replaced, kept


def read_uid(blob):
    """The resource UID from a .translation header, so a rebuild can keep it.

    Reusing the original UID means the engine's UID cache sees the same
    resource it always had, instead of a new one claiming id 0.
    """
    r = _R(blob)
    r.p = 4
    for _ in range(5):
        r.u32()
    r.s()
    r.u64()
    r.u32()
    return r.u64()


def build_optimized_translation(messages, locale, uid=0xFFFFFFFFFFFFFFFF):
    """Build a .translation blob.

    Strings are stored uncompressed -- the engine treats comp_size ==
    uncomp_size as 'stored raw', so we stay off a hand-rolled smaz_compress.
    """
    keys = list(messages.keys())
    size = larger_prime(len(keys))
    buckets = [[] for _ in range(size)]

    strings_blob = bytearray()
    entries = []
    for idx, k in enumerate(keys):
        cs = k.encode("utf-8")
        buckets[gd_hash(0, cs) % size].append((idx, cs))
        src = messages[k].encode("utf-8") + b"\x00"   # CharString includes the NUL
        entries.append((len(strings_blob), len(src), len(src)))
        strings_blob += src

    hash_table = [0xFFFFFFFF] * size
    bucket_table = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        d = 1
        while True:                                   # find a collision-free seed
            slots = {}
            ok = True
            for idx, cs in b:
                s = gd_hash(d, cs)
                if s in slots:
                    ok = False
                    break
                slots[s] = idx
            if ok:
                break
            d += 1
        hash_table[i] = len(bucket_table)
        bucket_table.append(len(b))
        bucket_table.append(d)
        for slot, idx in slots.items():
            off, comp, uncomp = entries[idx]
            bucket_table += [slot, off, comp, uncomp]

    return _serialize(hash_table, bucket_table, bytes(strings_blob), locale, uid)


def _pstr(s):
    b = s.encode("utf-8") + b"\x00"
    return struct.pack("<I", len(b)) + b


def _serialize(hash_table, bucket_table, strings_blob, locale, uid):
    body = bytearray()
    body += _pstr("OptimizedTranslation")
    props = [("locale", V_STRING, locale),
             ("hash_table", V_PACKED_INT32_ARRAY, hash_table),
             ("bucket_table", V_PACKED_INT32_ARRAY, bucket_table),
             ("strings", V_PACKED_BYTE_ARRAY, strings_blob)]
    body += struct.pack("<I", len(props))
    for name, vt, val in props:
        body += struct.pack("<II", _PROPS.index(name), vt)
        if vt == V_STRING:
            body += _pstr(val)
        elif vt == V_PACKED_INT32_ARRAY:
            body += struct.pack("<I", len(val)) + struct.pack("<%dI" % len(val), *val)
        else:
            body += struct.pack("<I", len(val)) + val + b"\x00" * ((4 - len(val) % 4) % 4)

    head = bytearray(b"RSRC")
    head += struct.pack("<IIIII", 0, 0, 4, 6, 6)      # endian, real64, Godot 4.6.6
    head += _pstr("OptimizedTranslation")
    head += struct.pack("<q", 0)                      # metadata offset
    head += struct.pack("<I", 3)                      # flags: named_scene_ids | uids
    head += struct.pack("<Q", 0)                      # uid (unused at runtime)
    head += b"\x00" * (4 * 11)                        # reserved
    head += struct.pack("<I", len(_PROPS))
    for s in _PROPS:
        head += _pstr(s)
    head += struct.pack("<I", 0)                      # external resources
    head += struct.pack("<I", 1)                      # internal resources
    name = _pstr("local://OptimizedTranslation_ru00")
    offset = len(head) + len(name) + 8
    head += name + struct.pack("<q", offset)
    return bytes(head) + bytes(body) + b"RSRC"
