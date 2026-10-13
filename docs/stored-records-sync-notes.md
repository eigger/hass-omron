# Stored-record sync: design review for #234

Review of how to read the cuff's stored history (HEM-7361T, 2 users x 100
slots) instead of only the latest record per user. Nothing here is implemented.
Confidence: **observed** (the index block and records in the #234 log),
**code** (read from this repository), **unverified**.

## Findings

- Incremental reads are possible; reading every slot on each poll is not needed.
- Each record carries a sequence number (`_record_id`, bytes 10-11,
  `record_parsers.py`). The index block also holds the latest one per user.
- The slot index cannot be computed from the sequence number (user 1: sequence
  1643, slot 99). Walk back from the write cursor; use the sequence only to count
  new records and to check continuity.
- Reads do not change the device: the 7361T profile has no
  `measurement_completion`. The unread counters (0x04 / 0x06) stay untouched,
  they belong to the OMRON connect app (see the ack-write notes).

## Index block (observed, #234 log)

`00400d00 00800080 6b060000 0d000000` at 0x0010:

| Offset | Value | Meaning |
| :--- | :--- | :--- |
| 0x00 / 0x02 | 0x4000 / 0x000D | per-user write cursor; low byte - 1 = newest slot (99 / 12) |
| 0x04 / 0x06 | 0x8000 / 0x8000 | `unread_counter_offset`, not read by the driver |
| 0x08 / 0x0C | 0x066B / 0x000D | per-user newest sequence; equals `_record_id` of the newest record (1643 / 13) |

Only the 7361T log and the #67 capture show 0x08 / 0x0C. Other index layouts
are **unverified**; enable per profile after comparing a reporter's
`Index block raw=` with the record bytes.

## Cost

`transmission_block_size` is 0x10, so one record is one transaction (about
200 ms in the log).

| Mode | Transactions | Time |
| :--- | :--- | :--- |
| Full dump, 2 x 100 | 200 | 40 s or more (**unverified** that the transfer link holds) |
| Incremental, N new | 1 index + N | about 0.2 s x N |

## Proposed sync

Per user, keep `last_seq` in a `helpers.storage.Store` (not `entry.data`).

1. `new = (index_seq - last_seq) mod 65536`.
2. `new == 0`: no extra reads. `new <= 100`: read `new` slots back from the
   cursor, check `_record_id` falls by 1 each step, return oldest first.
3. `new > 100`: the ring wrapped. Return the whole ring and report
   `new - 100` lost.
4. `index_seq < last_seq` (memory cleared, device swapped): rebase, dedupe by
   record signature.
5. First setup: `last_seq = index_seq`. No backfill.
6. Fire `omron_measurement` per record, oldest first, then persist `last_seq`
   (at-least-once).
7. Profiles without a verified sequence field fall back to a datetime
   watermark, deduped on (datetime, sys, dia, bpm).

`_probe_index_user` (`driver.py`) already walks back from the cursor for
TruRead; generalize its collect limit rather than adding a second walker.

Optional service `omron.read_stored_records` (`SupportsResponse.ONLY`): full
dump under `session_lock`, pattern of `button.py`. The user turns the response
into a CSV with an automation.

## Home Assistant history

- States cannot be written at a past time.
- `async_add_external_statistics` takes hourly mean/min/max, so individual
  readings (three in a row a minute apart) merge. Use a separate
  `omron:<addr>_...` statistic id, not the entity's, and re-aggregate the hour
  from the stored raw records on each increment (the hour row is overwritten).
- The maintainer's position in #234 is that history import is out of scope.

## Risks

- Clock zone: the cuff clock is written in the OS zone, record labels use the
  HA zone (roadmap, "Cuff clock vs record time zone"). A backfill shifts the
  whole history by the difference. Fix before any statistics import.
- Watermark must be the sequence, not the datetime (a clock set back hides new
  records).
- Selective deletion of bad readings needs device writes and has no HA path;
  out of scope.

## Needed from the reporter

1. Debug log after two or more readings between polls (0x08 up by N,
   consecutive ids).
2. Whether 0x08 resets after clearing the device memory.
3. Whether the `forced_transfer` advertisement appears after a reading.
4. HA time zone and host / container TZ.

## Test plan

Extend `tests/test_truread_index_sequence.py` fixtures with the #234 index and
record bytes: new = 0 / 3 / 100 / 150, wrap at cursor 0, `index_seq < last_seq`,
broken id continuity, 16-bit wrap, empty user 2, oldest-first order, and
`1 + N` reads. HA side: Store persistence, no duplicate events after a restart,
no events on first setup.

## Order

1. Incremental read plus event, HEM-7361T only.
2. Optional full-dump service.
3. Statistics sync, after the time-zone fix and the maintainer's decision.
