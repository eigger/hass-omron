# HN-300T2 memory map (beta, #233)

Reconstructed from a Bluetooth capture of the vendor app reading the scale
(102.8 kg at 2026-10-06 18:58:39). Same FE4A stack, token unlock and WLD1.0
memory protocol as the HEM-716BT2 cuff; only the record and the checks differ.
The scale also advertises the standard Weight Scale service (0x181D), which the
app never uses.

## Session

`80` open, reads, writes, `8F` close. A read returns at most 16 bytes, a write
takes at most 12 (frames of 24 and 20 bytes with a 6-byte header and a 2-byte
check).

## Regions

| Address | Size | Contents |
| :--- | :--- | :--- |
| `0x01A0` (read) / `0x0230` (write) | 12 | index block |
| `0x01B8` (read) / `0x0248` (write) | 8 | clock block |
| `0x02C0` | 30 x 16 | records, one user, ring buffer |

## Index block

| Offset | Contents |
| :--- | :--- |
| +0 | cursor |
| +4 | unread counter, `0x80` when cleared |
| +8 | u16 sequence number of the newest record |

Cursor byte: bit 7 keeps the odd parity of the byte, bit 6 is set once the ring
has wrapped, bits 0-5 are the pointer. The newest record is slot `pointer - 1`
(wrapping at 30). A cursor of `0x80` is the cleared value: nothing recorded.
The unread counter has the same layout with a 6-bit value.

The app clears the unread counter with a 12-byte write to `0x0230` after the
readout. The integration does not: nothing requires it for reading the latest
record. Revisit if the scale keeps announcing pending data.

## Record (16 bytes, big-endian)

| Offset | Contents |
| :--- | :--- |
| 0-1 | weight, 0.05 kg steps; `0xFFFF` is an empty slot |
| 2-7 | year-2000, month, day, hour, minute, second |
| 8 | unit: 0 kg, 1 lb, 2 st/lb |
| 9-10 | sequence number, equals the index sequence for the newest record |
| 11-12 | the same weight in 0.2 lb steps |
| 13-15 | not defined |

The lb field is the byte-order check: `0x046D` is 226.6 lb for 102.8 kg only
when read big-endian.

## Clock block (8 bytes)

`[year-2000, month, day, hour, minute, second, sum, pad]`. `sum` is the additive
checksum of the six time bytes. The app keeps the pad byte as it read it
(`0xFF` on the captured scale). The scale keeps local time; there is no zone
field.

## Not known

- What the scale does when the unread counter is never cleared.
- Whether the kg field stays valid when the scale is set to lb or st.
- The meaning of setting IDs the app reads next to the clock block.
