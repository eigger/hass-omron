# Body-composition scales: collected protocol notes (HBF family)

Working notes for adding the HBF body-composition scales after the HN-300T2
(#233, [hn-300t2-memory-map.md](hn-300t2-memory-map.md)). Nothing here is
implemented. Confidence is marked per item: **observed** (seen on a real
device), **map** (read from per-model device configuration data, not tested on a
device), **unverified** (a single report, or the reports disagree).

## Same stack as the cuffs

Every scale with a memory map sits on the memory protocol the cuffs already use
(session open/close, block reads and writes, index block, settings blocks with an
additive checksum). Only the record layout and a few per-model parameters differ,
so a scale is a catalog profile with a `MeasurementKind`, not a second driver.
The standard Weight Scale (0x181D) and Body Composition (0x181B) services are
listed in the GATT table of the scales that were inspected, and no capture shows
the vendor app using them.

## Per-model parameters (map)

| Model | Connect type | Users | Records | Index read / write | Cursor layout |
| :--- | :--- | :--- | :--- | :--- | :--- |
| HN-300T2 (all variants) | WLD1.0 | 1 | 30 x 16 B @0x2C0 | 0x1A0 / 0x230 | `OF111111` |
| HBF-222T, 227T, 228T, 229T, 230T | WLC3.0 | 4 | 30 x 32 B @0x2C0 | 0x1A0 / 0x230 | `EF111111` |
| HBF-702T | WLC3.0 | 4 | 30 x 48 B @0x2C0 | 0x1A0 / 0x230 | `EF111111` |
| HBF-257T2 (KRD-60x) | WLS3.0 | 4 | 30 x 32 B @0x2C0 | 0x1A0 / 0x230 | `EF111111` |
| HBF-260T1 / 270T1 | WLD3.2 | 2 / 4 | 32 B @0x240 | 0x120 / 0x1B0 | `OF111111` |
| HBF-255T / 256T | USB block | 4 | 32 B @0x260 | 0x1A0 / 0x200 | none |

US variants (SC-150 for the HN-300T2, BCM-500 for the HBF-222T) have no map of
their own; they are expected to follow the model they are built on.

Notes:

- `connect_type` is already an enum in the catalog (`ConnectType`); WLC3.0 is not
  in it yet.
- Cursor layout: the first letter is the parity of bit 7 (`O` odd, `E` even), `F`
  is bit 6, a flag that reads as "ring buffer has wrapped", and the low six bits
  are the pointer. The newest record is slot `pointer - 1`. The cleared value is
  `0x80` (parity bit alone) on the models checked.
- Index write block (12 bytes on the HN-300T2): +0 cursor, +4 unread count (same
  layout, cleared to `0x80`), +8 u16 sequence number.
- Settings blocks carry a checksum: the sum of the block without its last two
  bytes, stored at `size - 2`; the last byte is left as it was.
- Time: scales keep local time and have no zone field. The map's time-update rule
  is `0` on every scale (always write, no tolerance check).

## Record layout, HBF-222T (observed on one device, 32 bytes)

Bit-packed, bit 0 is the most significant bit of byte 0.

| Field | Bits | Encoding |
| :--- | :--- | :--- |
| weight | 0-11 | / 10, kg |
| body fat | 17-26 | / 10, % |
| visceral fat | 28-32 | integer |
| basal metabolic rate | 33-44 | kcal |
| skeletal muscle | 49-58 | / 10, % |
| year | 58-64 | + 2000 |
| BMI | 64-74 | / 10 |
| minute | 74-80 | |
| month | 92-96 | |
| day | 96-101 | |
| hour | 101-106 | |
| counter | bytes 24-25 | u16 big-endian, per profile |
| weight, second copy | 208-219 | same encoding as weight |

- 59 records were cross-checked against the cloud history of the same weigh-ins
  and matched to 0.1 kg and the minute.
- No seconds in the record; timestamps have one-minute resolution.
- The second copy of the weight is the only per-record integrity check; drop the
  record when the two differ.
- All composition fields read zero when the scale has no complete profile for the
  person (guest slot, or a child too young for bioimpedance). That is "not
  measured", not a measured zero; keep weight and BMI.
- The record has no person field. The height the scale used, `sqrt(weight / BMI)`,
  is the only hint of which profile a weigh-in landed on.
- The log is a ring of 30 records per profile; how far back it reaches depends on
  how often the person weighs in.

**Unverified:** one report reads the weight of the HBF scales as a
12-bit big-endian field at byte offset 26, low 4 bits dropped, in 0.05 kg steps.
That disagrees with the table above (0.1 kg steps from bit 0), and with its
48-byte vs 32-byte record sizes it may describe a different model. Settle it
against a real dump before writing a parser.

## User profile (map, HBF-222T_E user 1)

The scale cannot compute composition without the person's height, age and sex;
the vendor app writes them to the user's settings blocks (blocks 4 to 7, 24
bytes each, one per user slot).

| Field | Offset | Encoding |
| :--- | :--- | :--- |
| birth date | +48 | year - 1900, then month, day |
| sex | +51 | bit 0 |
| height | +52 | 0.1 cm steps, 100.0 to 199.5 |

Measurement codes in the map run `0101` to `0121`, up to 32 kinds. Which ones the
HBF scales fill is not known.

## Connection (observed on the HBF-222T, classic stack)

- Service `ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b`, unlock characteristic
  `b305b680-...`, command `db5b55e0-...`, notify RX0 to RX3 (`49123040-...`,
  `4d0bf320-...`, `5128ce60-...`, `560f1420-...`).
- The link must be bonded and encrypted; the scale sends a Security Request as
  soon as the unlock characteristic is touched.
- Unlock is a registered 16-byte key, not the stateless token the HN-300T2 uses:
  `01` + key answers `8100`; `02` + 16 zero bytes enters key programming and
  answers `8200`; `00` + key registers a key and answers `8000`. A non-zero
  second byte is a refusal. Registering a new key evicts the vendor app's key.
- **Notification order matters.** Before unlock only RX0 and the unlock
  characteristic may be subscribed, with RX1 to RX3 off. After `8100`, switch the
  unlock characteristic's notification off, then turn RX1, RX2 and RX3 on in that
  order, and only then start the transfer. Subscribing to everything on connect
  leaves the start command unanswered and the scale showing `Err`. Ties in with
  `keep_notify_subscriptions` in the catalog; the HBF scales need the opposite
  choreography for the unlock characteristic.
- Frames: start `08 00 00 00 00 10 00 18`, read `08 01 00 <addr hi> <addr lo> 30
  00 <xor>`, end `08 0f 00 00 00 00 00 07`. The last byte is the XOR of the
  bytes before it. A read of 48 bytes (`0x30`) comes back as one frame of 56
  bytes: length, `0x81`, 3-byte big-endian address, payload length, payload,
  trailer; its XOR over the whole frame is zero. The response arrives split
  across RX0 to RX3 and is reassembled in arrival order.
- Always send the end command, even when a read got no answer: an abandoned
  session leaves the scale on `Err` until it is reset.
- The scale talks to one peer at a time.

## Open questions before an HBF profile

- Weight field of the HBF record: the table above or the 12-bit variant (needs a
  real dump).
- What a scale does when the unread counter is not cleared, and whether the
  scale-side register of a user profile is needed for reading history at all.
- Which user slot a weigh-in belongs to (the record has no person field); the
  index block is per slot, so reading per-slot cursors is the likely answer.
- Whether WLC3.0 and WLS3.0 need anything beyond the classic-stack key unlock and
  the notification order above.
- Entities: weight, BMI, body fat, visceral fat, skeletal muscle, BMR, with the
  composition entities left unavailable when the scale reports them as absent.
- Where the user's birth date and sex come from: an options flow per user slot is
  the natural place. Height already has a home: the per-slot **Height** number
  entity (cm, `runtime.heights_cm[slot]`) that weight scales use for BMI, so
  `write_user_profile` can later take the height from it. A body-composition
  scale reports its own BMI, so it gets the Height entity but not the derived
  BMI sensors.

## What the HN-300T2 work already provides

`MeasurementKind` (blood pressure, weight, body composition), the per-user index
layout fields (`write_cursor_mask`, `cursor_parity`, `clear_value`), the
`WEIGHT_16` parser pattern, the 8-byte clock layout, the `write_user_profile` hook
(currently a no-op) and the weight sensor. An HBF profile adds a record parser, a
profile writer, the composition sensors and the connection choreography above.
