"""Verify the Omron device catalog against vendor device configuration specifications.

Usage: python3 scripts/verify_catalog_against_vendor.py <repo_root> <vendor_config_path> [<vendor_config_path> ...]
"""
from __future__ import annotations

import glob
import io
import json
import os
import sys
import zipfile

REPO, *VENDOR_ROOTS = sys.argv[1:]
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))
import conftest  # noqa: E402, F401  (stubs homeassistant/bleak so the catalog imports)

from custom_components.omron.omron_ble.device_catalog import CANONICAL_DEVICE_PROFILES  # noqa: E402


# ---------------------------------------------------------------- vendor load
def _parse_sys(text: str):
    """DeviceConfig.sys content -> dict; duplicate keys keep every value (connect_type)."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith((";", "[")) or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out.setdefault(k.strip(), []).append(v.strip())
    return {k: v[0] for k, v in out.items()} | {"_multi": out}


def _parse_json(content: str | bytes):
    if isinstance(content, str):
        content = content.encode("utf-8")
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            return json.loads(content.decode(enc))
        except Exception:
            continue
    return None


def _load_from_zip(zf: zipfile.ZipFile) -> dict:
    models = {}
    names = zf.namelist()
    for name in names:
        if "__MACOSX/" in name or os.path.basename(name).startswith("._"):
            continue
        if name.endswith("DeviceConfig.sys"):
            d = os.path.dirname(name)
            mname = os.path.basename(d)
            if mname in models:
                continue
            sys_txt = zf.read(name).decode("utf-8", errors="replace")
            other = {}
            vital = {}
            other_name = os.path.join(d, "OtherInfo.json")
            if other_name in names:
                other = _parse_json(zf.read(other_name)) or {}
            vital_name = os.path.join(d, "VitalDataIndexes.json")
            if vital_name in names:
                vital = _parse_json(zf.read(vital_name)) or {}
            models[mname] = {
                "dir": d,
                "sys": _parse_sys(sys_txt),
                "other": other,
                "vital": vital,
            }
        elif name.endswith(".zip"):
            try:
                sub_bytes = zf.read(name)
                with zipfile.ZipFile(io.BytesIO(sub_bytes)) as sub_zf:
                    sub_models = _load_from_zip(sub_zf)
                    for k, v in sub_models.items():
                        if k not in models:
                            models[k] = v
            except Exception:
                pass
    return models


def load_vendor():
    models = {}
    for root in VENDOR_ROOTS:
        if os.path.isfile(root):
            if zipfile.is_zipfile(root):
                with zipfile.ZipFile(root, "r") as zf:
                    sub_models = _load_from_zip(zf)
                    for k, v in sub_models.items():
                        if k not in models:
                            models[k] = v
            continue

        if os.path.isdir(root):
            for cfg in glob.glob(os.path.join(root, "**", "DeviceConfig.sys"), recursive=True):
                d = os.path.dirname(cfg)
                name = os.path.basename(d)
                if name in models:
                    continue
                sys_txt = open(cfg, encoding="utf-8", errors="replace").read()
                other_path = os.path.join(d, "OtherInfo.json")
                vital_path = os.path.join(d, "VitalDataIndexes.json")
                other = _parse_json(open(other_path, "rb").read()) if os.path.exists(other_path) else {}
                vital = _parse_json(open(vital_path, "rb").read()) if os.path.exists(vital_path) else {}
                models[name] = {
                    "dir": d,
                    "sys": _parse_sys(sys_txt),
                    "other": other or {},
                    "vital": vital or {},
                }
    return models


def hx(v: str) -> int:
    return int(v, 16)


def vendor_regions(s):
    """{address: (record_size, slot_count, data_index)} for every data_N block."""
    out = {}
    for i in range(1, 12):
        a = s.get(f"data_{i}_address")
        if not a:
            continue
        n = s.get(f"index_{i}_data_num_of_memories")
        if n is None and f"index_{i}_data_pointer_max" in s:
            n = int(s[f"index_{i}_data_pointer_max"]) + 1
        out[hx(a)] = (int(s.get(f"data_{i}_size", 0)), int(n) if n is not None else None, i)
    return out


def vendor_user(s, i):
    """Index-pointer fields for data block i, normalised across format versions."""
    def g(*names):
        return next((s[n] for n in names if n in s), None)

    cur = g(f"index_{i}_data_pointer_byte_offset", f"index_{i}_data_pointer_byte_offset_in_read")
    unr = g(f"index_{i}_num_of_unsend_byte_offset", f"index_{i}_num_of_unsend_byte_offset_in_read")
    layout = g(f"index_{i}_data_pointer_bit_layout")
    mask = None
    if layout:
        mask = sum(1 << (len(layout) - 1 - p) for p, c in enumerate(layout) if c == "1")
    return {
        "cursor": int(cur) if cur is not None else None,
        "unread": int(unr) if unr is not None else None,
        "mask": mask,
        "bias": int(g(f"index_{i}_data_pointer_latest_pos_correction") or 0),
        "min": int(g(f"index_{i}_data_pointer_min") or 0),
        "max": (lambda v: int(v) if v is not None else None)(g(f"index_{i}_data_pointer_max")),
    }


def vendor_setting_blocks(s):
    """[(start, end)] of the setting-info blocks, as offsets from settings_read_address."""
    read = s.get("index_setting_read_address")
    if not read:
        return []
    base = hx(read)
    info = s.get("index_setting_info_read_address")
    start = hx(info) - base if info else int(s.get("index_setting_pointer_unsend_size") or 0)
    blocks, off = [], start
    for i in range(1, 9):
        size = s.get(f"index_setting_info_block_{i}_size")
        if size is None:
            break
        explicit = s.get(f"index_setting_info_block_{i}_address_offset")
        b0 = start + int(explicit) if explicit is not None else off
        blocks.append((b0, b0 + int(size)))
        off = b0 + int(size)
    return blocks


def vital_signature(v):
    """(sys, dia, bpm, year) byte offsets from VitalDataIndexes.json, if present."""
    idx = v.get("vitalDataIndex1")
    if not isinstance(idx, dict):
        return None
    md = idx.get("measurementData") or {}
    sd = idx.get("startDate") or {}

    def off(node):
        if isinstance(node, dict):
            return node.get("addressOffset")
        return None

    def m(code):
        for key in (code, code.lower(), code.upper()):
            if key in md:
                return off((md[key] or {}).get("value") or (md[key] or {}).get("measurement", {}).get("value"))
        return None

    return (m("0001"), m("0002"), m("0003"), off(sd.get("year")))


def bp_block_indices(ven):
    """Data blocks containing blood pressure measurements."""
    s, vital = ven["sys"], ven["vital"]
    explicit = {
        i
        for i in range(1, 12)
        if s.get(f"data_{i}_num_of_unsend_key") == "blood_pressure_unsend_num"
    }
    if explicit:
        return explicit
    out = set()
    for i in range(1, 12):
        idx = vital.get(f"vitalDataIndex{i}")
        if isinstance(idx, dict):
            codes = {c.lower() for c in (idx.get("measurementData") or {})}
            if {"0001", "0002", "0003"} <= codes:
                out.add(i)
    return out


# ---------------------------------------------------------------- comparison
def check(model_id, cfg, ven):
    s = ven["sys"]
    issues = []
    regions = vendor_regions(s)

    # 1. record regions: match by address so error/waveform blocks never shift users
    for u, addr in enumerate(cfg.user_start_addresses or []):
        if addr not in regions:
            issues.append(
                f"user{u+1} start 0x{addr:04X} is not a vendor data block "
                f"(vendor blocks: {['0x%04X' % a for a in sorted(regions)]})"
            )
            continue
        size, count, di = regions[addr]
        bp = bp_block_indices(ven)
        if bp and di not in bp:
            issues.append(
                f"user{u+1} start 0x{addr:04X} is vendor data_{di}, not a blood-pressure block "
                f"(vendor marks data_{'/'.join(str(b) for b in sorted(bp))} as BP)"
            )
        if size and size != cfg.record_byte_size:
            issues.append(f"user{u+1} record_byte_size {cfg.record_byte_size} != vendor {size}")
        if count is not None and count != cfg.per_user_records_count[u]:
            issues.append(f"user{u+1} records {cfg.per_user_records_count[u]} != vendor {count}")

        # 2. index-pointer fields for that same data block
        layout = (cfg.index_pointer_layout or {}).get("users") or []
        if u < len(layout):
            vu, ru = vendor_user(s, di), layout[u]
            if vu["cursor"] is not None and vu["cursor"] != ru.get("write_cursor_offset"):
                issues.append(f"user{u+1} write_cursor_offset 0x{ru.get('write_cursor_offset'):02X} != vendor {vu['cursor']}")
            if vu["unread"] is not None and vu["unread"] != ru.get("unread_counter_offset"):
                issues.append(f"user{u+1} unread_counter_offset 0x{ru.get('unread_counter_offset'):02X} != vendor {vu['unread']}")
            if vu["mask"] is not None and vu["mask"] != ru.get("write_cursor_mask"):
                issues.append(f"user{u+1} write_cursor_mask 0x{ru.get('write_cursor_mask'):02X} != vendor 0x{vu['mask']:02X}")
            if vu["bias"] != ru.get("slot_index_bias"):
                issues.append(f"user{u+1} slot_index_bias {ru.get('slot_index_bias')} != vendor {vu['bias']}")
            vmax = vu["max"] if vu["max"] is not None else (count - 1 if count else None)
            if vmax is not None and vmax != ru.get("slot_index_max"):
                issues.append(f"user{u+1} slot_index_max {ru.get('slot_index_max')} != vendor {vmax}")

    # 3. settings block addresses
    for key, attr in (("index_setting_read_address", "settings_read_address"),
                      ("index_setting_write_address", "settings_write_address")):
        v, r = s.get(key), getattr(cfg, attr)
        if v and r is not None and hx(v) != r:
            issues.append(f"{attr} 0x{r:04X} != vendor 0x{hx(v):04X}")

    # 4. index region size
    us = s.get("index_setting_pointer_unsend_size")
    rr = (cfg.index_pointer_layout or {}).get("index_region_byte_size")
    if us and rr is not None and int(us) != rr:
        issues.append(f"index_region_byte_size {rr} (0x{rr:02X}) != vendor {us}")

    # 5. time-sync window must line up with one of the setting-info blocks
    if cfg.settings_time_sync_bytes:
        blocks = vendor_setting_blocks(s)
        want = tuple(cfg.settings_time_sync_bytes)
        if blocks and want[0] not in [b[0] for b in blocks]:
            issues.append(
                f"settings_time_sync_bytes starts at 0x{want[0]:02X}, which is not a vendor "
                f"setting-block boundary {['0x%02X' % a for a, _ in blocks]}"
            )

    # 6. connect type (the [model] section can list several: usb_block + a WL* one)
    vct = [c for c in ven["sys"]["_multi"].get("connect_type", []) if c != "usb_block"]
    if vct and str(cfg.connect_type) and str(cfg.connect_type) not in vct:
        issues.append(f"connect_type {cfg.connect_type!s} != vendor {'/'.join(vct)}")

    return issues


def main():
    vendor = load_vendor()
    rows, covered, clean = [], 0, 0
    uncovered, sig_by_profile = [], {}

    for canon, cfg in CANONICAL_DEVICE_PROFILES.items():
        for mid in [canon] + list(cfg.equivalent_model_ids or ()):
            ven = vendor.get(mid)
            if ven is None:
                uncovered.append((canon, mid))
                continue
            covered += 1
            sig = vital_signature(ven["vital"])
            if sig:
                sig_by_profile.setdefault(canon, {}).setdefault(sig, []).append(mid)
            issues = check(mid, cfg, ven)
            if issues:
                rows.append((canon, mid, issues))
            else:
                clean += 1

    known = {m for c in CANONICAL_DEVICE_PROFILES for m in [c] + list(CANONICAL_DEVICE_PROFILES[c].equivalent_model_ids or ())}
    def is_bp(v):
        idx = (v["vital"] or {}).get("vitalDataIndex1") or {}
        md = {k.lower() for k in (idx.get("measurementData") or {})}
        return {"0001", "0002", "0003"} <= md and v["sys"].get("data_1_size") in {"14", "16"}
    missing = sorted(m for m, v in vendor.items() if m not in known and vendor_regions(v["sys"]))
    missing_bp = [m for m in missing if is_bp(vendor[m])]

    print("=" * 78)
    print(f"vendor model configs : {len(vendor)}")
    print(f"catalog model ids    : {len(known)}")
    print(f"  with vendor config : {covered}   ({clean} fully match, {len(rows)} with findings)")
    print(f"  without            : {len(known) - covered}")
    print(f"vendor models absent from catalog: {len(missing)}")
    print("=" * 78)

    if rows:
        print("\n## FINDINGS\n")
        for canon, mid, issues in rows:
            print(f"[{canon}] {mid}")
            for i in issues:
                print(f"    - {i}")

    mixed = {c: g for c, g in sig_by_profile.items() if len(g) > 1}
    if mixed:
        print("\n## profiles whose variants disagree on record field layout\n")
        for c, g in mixed.items():
            print(f"[{c}]")
            for sig, ms in g.items():
                print(f"    sys/dia/bpm/year offsets {sig}: {', '.join(ms)}")

    if missing:
        print(f"\n## blood-pressure models in the vendor set but not in the catalog ({len(missing_bp)})\n")
        for m in missing_bp:
            s2 = vendor[m]["sys"]
            regs = vendor_regions(s2)
            print(f"    {m:22s} {[c for c in vendor[m]['sys']['_multi'].get('connect_type',[]) if c!='usb_block']} "
                  f"data={[('0x%04X' % a, r[0], r[1]) for a, r in sorted(regs.items())][:2]} "
                  f"set={s2.get('index_setting_read_address')}/{s2.get('index_setting_write_address')}")
        other = [m for m in missing if m not in missing_bp]
        print(f"\n## other (non-BP) vendor models not in the catalog ({len(other)}) — out of scope\n    "
              + ", ".join(other))

    print(f"\n## catalog ids with no vendor config ({len(uncovered)})\n")
    for canon, mid in uncovered:
        print(f"    [{canon}] {mid}")


if __name__ == "__main__":
    main()
