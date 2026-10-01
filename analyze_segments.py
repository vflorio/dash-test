#!/usr/bin/env python3
"""analyze_segments.py - Confronta a basso livello due (o piu') segmenti fMP4
DASH per capire cosa differenzia uno stream reale "rotto sui Samsung" dal nostro
sintetico "che funziona".

Estrae, senza dipendenze esterne, i tratti che piu' probabilmente fanno
inciampare i decoder Tizen su DAI multi-periodo:
  - ftyp (brand), timescale moov/mdhd
  - edit list (elst): media_time / priming  <-- stitching DAI
  - mvex/trex + tfhd default sample flags
  - moof/traf/tfdt baseMediaDecodeTime      <-- allineamento timeline
  - SPS H.264: profile/level, frame_mbs_only_flag, mb_adaptive_frame_field_flag,
    VUI (timing, fixed_frame_rate_flag), nal_hrd, pic_struct_present_flag
  - SEI nel primo sample: pic_timing (1), buffering_period (0), ...
  - audio: esds object type / sampling freq / SBR (HE-AAC)

Uso:
  # Un singolo gruppo nostro:
  python3 analyze_segments.py nostro=build/content_ildct_heaac/init-stream2.m4s,\\
      build/content_ildct_heaac/chunk-stream2-00001.m4s

  # Confronto reale vs nostro (scarica prima i due file reali, vedi --help-fetch):
  python3 analyze_segments.py \\
      reale=/tmp/real_init.m4s,/tmp/real_chunk.m4s \\
      nostro=build/content_ildct_heaac/init-stream2.m4s,build/content_ildct_heaac/chunk-stream2-00001.m4s

Suggerimento per ottenere i file reali da un MPD con BaseURL assoluta:
  BASE=http://.../a8fd.../r350086.mov/
  curl -o /tmp/real_init.m4s  "${BASE}init-stream2.m4s"
  curl -o /tmp/real_chunk.m4s "${BASE}chunk-stream2-00001.m4s"
  (stream2 = rendition 1080p; usa stream3 per l'audio)
"""

from __future__ import annotations

import struct
import sys


# --------------------------------------------------------------------------
# Walker dei box ISO-BMFF
# --------------------------------------------------------------------------
CONTAINERS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd", b"edts",
    b"mvex", b"moof", b"traf", b"mfra", b"dinf", b"udta",
    b"avc1", b"avc3", b"encv", b"mp4a", b"enca",
}


def iter_boxes(data: bytes, start: int = 0, end: int | None = None):
    """Itera i box di primo livello in data[start:end] -> (type, box_start,
    payload_start, box_end)."""
    if end is None:
        end = len(data)
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        btype = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        yield btype, pos, pos + header, pos + size
        pos += size


def find_all(data, path, start=0, end=None):
    """Trova tutti i box lungo un path tipo [b'moov', b'trak', ...]."""
    if end is None:
        end = len(data)
    results = []

    def rec(s, e, depth):
        for btype, bs, ps, be in iter_boxes(data, s, e):
            if btype == path[depth]:
                if depth == len(path) - 1:
                    results.append((bs, ps, be))
                elif btype in CONTAINERS:
                    # stsd ha un header di 8 byte prima dei figli
                    cs = ps + 8 if btype == b"stsd" else ps
                    rec(cs, be, depth + 1)
    rec(start, end, 0)
    return results


# --------------------------------------------------------------------------
# BitReader per SPS / SEI (Exp-Golomb)
# --------------------------------------------------------------------------
class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0  # in bit

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            v = (v << 1) | bit
            self.pos += 1
        return v

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                return 0
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def rbsp(nal: bytes) -> bytes:
    """Rimuove gli emulation-prevention bytes 0x000003."""
    out = bytearray()
    i = 0
    n = len(nal)
    while i < n:
        if i + 2 < n and nal[i] == 0 and nal[i + 1] == 0 and nal[i + 2] == 3:
            out.append(0)
            out.append(0)
            i += 3
        else:
            out.append(nal[i])
            i += 1
    return bytes(out)


def parse_sps(sps: bytes) -> dict:
    """Parsa i campi SPS H.264 rilevanti per l'interlacciamento/timing."""
    r = BitReader(rbsp(sps))
    out = {}
    profile = r.u(8)
    r.u(8)  # constraint flags + reserved
    level = r.u(8)
    out["profile_idc"] = profile
    out["level_idc"] = level
    r.ue()  # seq_parameter_set_id
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = r.ue()
        if chroma == 3:
            r.u(1)
        r.ue()  # bit_depth_luma
        r.ue()  # bit_depth_chroma
        r.u(1)  # qpprime_y_zero_transform_bypass
        if r.u(1):  # seq_scaling_matrix_present
            for i in range(8 if chroma != 3 else 12):
                if r.u(1):
                    size = 16 if i < 6 else 64
                    last = next_ = 8
                    for _ in range(size):
                        if next_ != 0:
                            delta = r.se()
                            next_ = (last + delta + 256) % 256
                        last = next_ if next_ != 0 else last
    r.ue()  # log2_max_frame_num
    poc_type = r.ue()
    if poc_type == 0:
        r.ue()
    elif poc_type == 1:
        r.u(1)
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()  # max_num_ref_frames
    r.u(1)  # gaps_in_frame_num_allowed
    r.ue()  # pic_width_in_mbs_minus1
    r.ue()  # pic_height_in_map_units_minus1
    frame_mbs_only = r.u(1)
    out["frame_mbs_only_flag"] = frame_mbs_only
    out["mb_adaptive_frame_field_flag"] = 0
    if not frame_mbs_only:
        out["mb_adaptive_frame_field_flag"] = r.u(1)
    r.u(1)  # direct_8x8_inference
    if r.u(1):  # frame_cropping
        r.ue(); r.ue(); r.ue(); r.ue()
    out["vui_present"] = 0
    out["fixed_frame_rate_flag"] = None
    out["nal_hrd"] = 0
    out["vcl_hrd"] = 0
    out["pic_struct_present_flag"] = 0
    out["timing_info"] = None
    if r.u(1):  # vui_parameters_present
        out["vui_present"] = 1
        if r.u(1):  # aspect_ratio_info_present
            if r.u(8) == 255:
                r.u(16); r.u(16)
        if r.u(1):  # overscan_info_present
            r.u(1)
        if r.u(1):  # video_signal_type_present
            r.u(3); r.u(1)
            if r.u(1):  # colour_description_present
                r.u(8); r.u(8); r.u(8)
        if r.u(1):  # chroma_loc_info_present
            r.ue(); r.ue()
        if r.u(1):  # timing_info_present
            num_units = r.u(32)
            time_scale = r.u(32)
            fixed = r.u(1)
            out["timing_info"] = (num_units, time_scale)
            out["fixed_frame_rate_flag"] = fixed
        nal_hrd = r.u(1)
        out["nal_hrd"] = nal_hrd
        if nal_hrd:
            _skip_hrd(r)
        vcl_hrd = r.u(1)
        out["vcl_hrd"] = vcl_hrd
        if vcl_hrd:
            _skip_hrd(r)
        if nal_hrd or vcl_hrd:
            r.u(1)  # low_delay_hrd
        r.u(1)  # pic_struct_present_flag
        out["pic_struct_present_flag"] = (r.data[(r.pos - 1) >> 3] >> (7 - ((r.pos - 1) & 7))) & 1
    return out


def _skip_hrd(r: BitReader) -> None:
    cpb_cnt = r.ue() + 1
    r.u(4)  # bit_rate_scale
    r.u(4)  # cpb_size_scale
    for _ in range(cpb_cnt):
        r.ue(); r.ue(); r.u(1)
    r.u(5); r.u(5); r.u(5); r.u(5)


# --------------------------------------------------------------------------
# Estrazione feature da un buffer (init + eventuale fragment)
# --------------------------------------------------------------------------
SEI_TYPES = {0: "buffering_period", 1: "pic_timing", 2: "pan_scan",
             3: "filler", 4: "user_data_registered", 5: "user_data_unregistered",
             6: "recovery_point", 45: "frame_packing"}


def read_fullbox_version_flags(data, ps):
    vf = struct.unpack(">I", data[ps:ps + 4])[0]
    return (vf >> 24) & 0xFF, vf & 0xFFFFFF


def extract(data: bytes) -> dict:
    f = {"brands": None, "handlers": [], "mdhd_timescale": None,
         "elst": None, "tfdt": None, "trex": None, "tfhd": None,
         "sps": None, "avc_codec": None, "audio": None, "sei": [], "nal_types": []}

    # ftyp
    for btype, bs, ps, be in iter_boxes(data):
        if btype == b"ftyp":
            major = data[ps:ps + 4]
            compat = [data[i:i + 4] for i in range(ps + 8, be, 4)]
            f["brands"] = (major.decode("latin1"),
                           [c.decode("latin1") for c in compat])
            break

    # handler + mdhd timescale (primo trak video se possibile)
    for bs, ps, be in find_all(data, [b"moov", b"trak", b"mdia", b"hdlr"]):
        handler = data[ps + 8:ps + 12].decode("latin1")
        f["handlers"].append(handler)
    for bs, ps, be in find_all(data, [b"moov", b"trak", b"mdia", b"mdhd"]):
        ver, _ = read_fullbox_version_flags(data, ps)
        off = ps + 4
        if ver == 1:
            ts = struct.unpack(">I", data[off + 16:off + 20])[0]
        else:
            ts = struct.unpack(">I", data[off + 8:off + 12])[0]
        f["mdhd_timescale"] = ts
        break

    # edit list
    for bs, ps, be in find_all(data, [b"moov", b"trak", b"edts", b"elst"]):
        ver, _ = read_fullbox_version_flags(data, ps)
        off = ps + 4
        count = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        entries = []
        for _ in range(count):
            if ver == 1:
                seg_dur = struct.unpack(">Q", data[off:off + 8])[0]
                media_time = struct.unpack(">q", data[off + 8:off + 16])[0]
                off += 16
            else:
                seg_dur = struct.unpack(">I", data[off:off + 4])[0]
                media_time = struct.unpack(">i", data[off + 4:off + 8])[0]
                off += 8
            rate = struct.unpack(">i", data[off:off + 4])[0]
            off += 4
            entries.append((seg_dur, media_time, rate))
        f["elst"] = entries
        break

    # trex
    for bs, ps, be in find_all(data, [b"moov", b"mvex", b"trex"]):
        off = ps + 4
        vals = struct.unpack(">IIIII", data[off:off + 20])
        f["trex"] = {"track_id": vals[0], "default_sample_duration": vals[2],
                     "default_sample_size": vals[3], "default_sample_flags": vals[4]}
        break

    # stsd -> avcC (video) oppure esds (audio)
    for bs, ps, be in find_all(data, [b"moov", b"trak", b"mdia", b"minf",
                                      b"stbl", b"stsd"]):
        for sbtype, sbs, sps_, sbe in iter_boxes(data, ps + 8, be):
            if sbtype in (b"avc1", b"avc3", b"encv"):
                for cbtype, cbs, cps, cbe in iter_boxes(data, sps_ + 78, sbe):
                    if cbtype == b"avcC":
                        f["avc_codec"], f["sps"] = _parse_avcc(data, cps, cbe)
            elif sbtype in (b"mp4a", b"enca"):
                f["audio"] = _parse_audio(data, sbs, sbe)

    # moof/traf: tfhd, tfdt, trun ; poi SEI nel primo sample
    moof_boxes = find_all(data, [b"moof"])
    if moof_boxes:
        _parse_fragment(data, moof_boxes[0], f)
    return f


def _parse_avcc(data, cps, cbe):
    # avcC: configurationVersion, profile, compat, level, ..., SPS
    off = cps + 5
    num_sps = data[off] & 0x1F
    off += 1
    sps = None
    for _ in range(num_sps):
        ln = struct.unpack(">H", data[off:off + 2])[0]
        off += 2
        sps = data[off:off + ln]
        off += ln
    profile = data[cps + 1]
    level = data[cps + 3]
    codec = "avc1.%02x%02x%02x" % (profile, data[cps + 2], level)
    parsed = parse_sps(sps) if sps else None
    return codec, parsed


def _parse_audio(data, sbs, sbe):
    out = {"codec": None, "channels": None, "samplerate": None,
           "object_type": None, "sbr": None}
    # AudioSampleEntry: 8 byte box header + 28 byte parte fissa, poi i figli (esds).
    esds = None
    for btype, bs, ps, be in iter_boxes(data, sbs + 8 + 28, sbe):
        if btype == b"esds":
            esds = (ps, be)
            break
    if esds is None:  # fallback: cerca il box esds ovunque nel sample entry
        idx = data.rfind(b"esds", sbs, sbe)
        if idx >= 0:
            esds = (idx + 4, sbe)
    if esds is not None:
        ps, be = esds
        blob = data[ps + 4:be]
        # cerca il DecoderSpecificInfo (tag 0x05) e AudioSpecificConfig
        i = blob.find(b"\x05")
        if i >= 0:
            j = i + 1
            length = 0
            while j < len(blob):
                b = blob[j]
                length = (length << 7) | (b & 0x7F)
                j += 1
                if not (b & 0x80):
                    break
            asc = blob[j:j + length]
            if len(asc) >= 2:
                r = BitReader(asc)
                obj = r.u(5)
                if obj == 31:
                    obj = 32 + r.u(6)
                freq_idx = r.u(4)
                chan = r.u(4)
                out["object_type"] = obj
                out["channels"] = chan
                freqs = [96000, 88200, 64000, 48000, 44100, 32000, 24000,
                         22050, 16000, 12000, 11025, 8000, 7350]
                out["samplerate"] = freqs[freq_idx] if freq_idx < len(freqs) else None
                out["sbr"] = (obj == 5)
    return out


def _parse_fragment(data, moof, f):
    moof_start, _, moof_end = moof
    trafs = find_all(data, [b"moof", b"traf"], moof_start, moof_end)
    if not trafs:
        return
    tbs, tps, tbe = trafs[0]
    default_sample_size = f["trex"]["default_sample_size"] if f["trex"] else 0
    default_sample_dur = f["trex"]["default_sample_duration"] if f["trex"] else 0
    base_is_moof = False
    trun_boxes = []
    for btype, bs, ps, be in iter_boxes(data, tps, tbe):
        if btype == b"tfhd":
            ver, flags = read_fullbox_version_flags(data, ps)
            off = ps + 4 + 4  # version/flags + track_ID
            if flags & 0x000001:
                off += 8  # base_data_offset
            if flags & 0x000002:
                off += 4  # sample_description_index
            if flags & 0x000008:
                default_sample_dur = struct.unpack(">I", data[off:off + 4])[0]
                off += 4
            if flags & 0x000010:
                default_sample_size = struct.unpack(">I", data[off:off + 4])[0]
                off += 4
            dflags = None
            if flags & 0x000020:
                dflags = struct.unpack(">I", data[off:off + 4])[0]
            base_is_moof = bool(flags & 0x020000)
            f["tfhd"] = {"default_sample_duration": default_sample_dur,
                         "default_sample_size": default_sample_size,
                         "default_sample_flags": dflags,
                         "default_base_is_moof": base_is_moof}
        elif btype == b"tfdt":
            ver, _ = read_fullbox_version_flags(data, ps)
            if ver == 1:
                f["tfdt"] = struct.unpack(">Q", data[ps + 4:ps + 12])[0]
            else:
                f["tfdt"] = struct.unpack(">I", data[ps + 4:ps + 8])[0]
        elif btype == b"trun":
            trun_boxes.append((ps, be))

    # calcola offset e dimensione del primo sample
    first_size = default_sample_size
    first_off = None
    if trun_boxes:
        ps, be = trun_boxes[0]
        ver, flags = read_fullbox_version_flags(data, ps)
        off = ps + 4
        sample_count = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        data_offset = 0
        if flags & 0x000001:
            data_offset = struct.unpack(">i", data[off:off + 4])[0]
            off += 4
        if flags & 0x000004:
            off += 4  # first_sample_flags
        # primo sample
        sdur = ssize = sflags = None
        if flags & 0x000100:
            sdur = struct.unpack(">I", data[off:off + 4])[0]; off += 4
        if flags & 0x000200:
            ssize = struct.unpack(">I", data[off:off + 4])[0]; off += 4
        if ssize is not None:
            first_size = ssize
        if base_is_moof:
            first_off = moof_start + data_offset

    # scan NAL del primo sample
    if first_off is None:
        mdat = [b for b in iter_boxes(data) if b[0] == b"mdat"]
        if mdat:
            first_off = mdat[0][2]
    if first_off is not None and first_size:
        _scan_nals(data, first_off, min(first_size, len(data) - first_off), f)


def _scan_nals(data, off, size, f):
    """Itera i NAL AVC length-prefixed (4 byte) del primo sample."""
    end = off + size
    pos = off
    seen = []
    seis = []
    guard = 0
    while pos + 4 <= end and guard < 256:
        guard += 1
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        if ln <= 0 or pos + ln > end:
            break
        nal_type = data[pos] & 0x1F
        seen.append(nal_type)
        if nal_type == 6:  # SEI
            seis.extend(_parse_sei(data[pos + 1:pos + ln]))
        pos += ln
    f["nal_types"] = seen
    f["sei"] = seis


def _parse_sei(payload: bytes):
    out = []
    data = rbsp(payload)
    i = 0
    n = len(data)
    while i < n:
        t = 0
        while i < n and data[i] == 0xFF:
            t += 255; i += 1
        if i >= n:
            break
        t += data[i]; i += 1
        size = 0
        while i < n and data[i] == 0xFF:
            size += 255; i += 1
        if i >= n:
            break
        size += data[i]; i += 1
        out.append((t, SEI_TYPES.get(t, "type%d" % t)))
        i += size
        if t == 0x80:
            break
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def load(spec: str):
    label, _, paths = spec.partition("=")
    if not paths:
        label, paths = spec, spec
    buf = b""
    for p in paths.split(","):
        with open(p.strip(), "rb") as fh:
            buf += fh.read()
    return label, buf


def summarize(f: dict) -> dict:
    sps = f.get("sps") or {}
    ti = sps.get("timing_info")
    return {
        "brand": (f["brands"][0] if f["brands"] else "?"),
        "handlers": ",".join(f["handlers"]) or "?",
        "mdhd_timescale": f["mdhd_timescale"],
        "elst": f["elst"],
        "tfdt_baseMediaDecodeTime": f["tfdt"],
        "trex_default_sample_flags": (hex(f["trex"]["default_sample_flags"])
                                      if f["trex"] else None),
        "avc_codec": f["avc_codec"],
        "frame_mbs_only_flag": sps.get("frame_mbs_only_flag"),
        "mb_adaptive_frame_field_flag": sps.get("mb_adaptive_frame_field_flag"),
        "vui_present": sps.get("vui_present"),
        "timing_info(num,scale)": ti,
        "fixed_frame_rate_flag": sps.get("fixed_frame_rate_flag"),
        "nal_hrd": sps.get("nal_hrd"),
        "pic_struct_present_flag": sps.get("pic_struct_present_flag"),
        "sei_first_sample": [name for _, name in f["sei"]] or None,
        "nal_types_first_sample": f["nal_types"] or None,
        "audio": f["audio"],
    }


def main(argv):
    specs = [a for a in argv if "=" in a or a.endswith(".m4s") or a.endswith(".mp4")]
    if not specs:
        print(__doc__)
        return 2
    reports = []
    for spec in specs:
        label, buf = load(spec)
        reports.append((label, summarize(extract(buf))))

    keys = list(reports[0][1].keys())
    labels = [r[0] for r in reports]
    w = max(28, *(len(k) for k in keys)) + 1
    colw = max(24, *(len(l) for l in labels)) + 2

    header = "campo".ljust(w) + "".join(l.ljust(colw) for l in labels)
    print(header)
    print("-" * len(header))
    for k in keys:
        row = k.ljust(w)
        vals = [str(r[1][k]) for r in reports]
        diff = len(set(vals)) > 1 and len(reports) > 1
        for v in vals:
            row += v.ljust(colw)
        if diff:
            row += " <-- DIFF"
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
