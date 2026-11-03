"""Reader for erwin Data Modeler binary .erwin files (r9/r10 'GDM' format).

The file is a stream of tagged objects:
  f6 <class:u32> f6 <id:u32> f6 <owner:u32> f6 <nprops:u32>
followed by nprops properties:
  f6 <prop:u32> fb 00 00 <type:u8> 00 00 <value>
Type 0x40 set = list (u32 count + items). Base types are sized below.
A value written as fb 01 00 carries no data.
"""
import re
import struct
from collections import defaultdict

FIXED = {0x01: 4, 0x03: 20, 0x05: 16, 0x06: 1, 0x07: 8, 0x0B: 4, 0x0C: 8, 0x0D: 8}
# model objects are classes 0x4020xxxx; 0x40000004 holds user-defined property (UDP) definitions
HDR = re.compile(rb'\xf6(..\x20\x40|\x04\x00\x00\x40)\xf6(....)\xf6(....)\xf6(....)', re.S)


class Obj:
    __slots__ = ("cls", "id", "owner", "props", "children")

    def __init__(self, cls, oid, owner):
        self.cls, self.id, self.owner = cls, oid, owner
        self.props, self.children = {}, []

    def __repr__(self):
        return f"<{self.cls:#x} id={self.id} owner={self.owner}>"


def _u32(d, p):
    return struct.unpack_from('<I', d, p)[0]


def _scalar(d, t, p):
    if t == 0x02:
        n = _u32(d, p)
        if n == 0:
            return '', p + 4
        # NUL-terminated; older models use cp1252 rather than UTF-8
        raw = d[p + 4:p + 4 + n]
        try:
            return raw.decode('utf-8'), p + 5 + n
        except UnicodeDecodeError:
            return raw.decode('cp1252', 'replace'), p + 5 + n
    if t == 0x00:
        return None, p
    if t == 0x04:  # blob, e.g. an embedded PNG
        n = _u32(d, p)
        return d[p + 4:p + 4 + n], p + 4 + n
    size = FIXED.get(t)
    if size is None:
        raise ValueError(f"unknown type {t:#x}")
    raw = d[p:p + size]
    if t in (0x01, 0x0B):
        v = struct.unpack('<i', raw)[0]
    elif t == 0x03:
        v = raw[:16].hex()
    elif t == 0x06:
        v = bool(raw[0])
    elif t == 0x0D:
        v = struct.unpack('<d', raw)[0]
    elif t == 0x07:
        v = struct.unpack('<ii', raw)
    else:
        v = raw.hex()
    return v, p + size


def _value(d, p):
    if d[p] != 0xFB:
        raise ValueError("expected value marker")
    if d[p + 1] == 0x01:  # short form: property present but holds no value
        return None, p + 3
    t = d[p + 3]
    p += 6
    if t & 0x40:
        n = _u32(d, p)
        p += 4
        out = []
        for _ in range(n):
            v, p = _scalar(d, t & 0x3F, p)
            out.append(v)
        return out, p
    return _scalar(d, t, p)


def read(path):
    d = open(path, 'rb').read()
    objs = {}
    failed = 0
    for m in HDR.finditer(d):
        cls = _u32(d, m.start(1))
        oid, owner, n = (struct.unpack('<I', m.group(i))[0] for i in (2, 3, 4))
        if n > 2000:
            continue
        o = Obj(cls, oid, owner)
        p = m.end()
        try:
            for _ in range(n):
                if d[p] != 0xF6:
                    raise ValueError("expected property marker")
                pid = _u32(d, p + 1)
                o.props[pid], p = _value(d, p + 5)
        except (ValueError, struct.error, IndexError):
            failed += 1
            continue
        objs[oid] = o
    for o in objs.values():
        if o.owner in objs and o.owner != o.id:
            objs[o.owner].children.append(o)
    return objs, failed
