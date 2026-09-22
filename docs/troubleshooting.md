# Troubleshooting

When a cuff does not sync, the integration has usually already recorded why. This page is the order to look in: first the two diagnostic sensors that hold the last BLE session's breakdown, then the cases those sensors cannot see, then what to attach to an issue.

## Where to look

Open the cuff's device page (**Settings → Devices & services → Omron → the cuff**). Under *Diagnostic* there are five entities to read, in this order:

| Entity | What it tells you |
|---|---|
| **Last Failure** | When a BLE session last **failed**. Its *attributes* are the breakdown of that session — `failed_stage`, `likely_cause`, `error`, the radio (`via`, `rssi`, `paths`) and the per-stage timings. They stay until the next failure, so a failure from last night is still readable after this morning's poll succeeded. |
| **Duration** | Seconds of the **most recent** BLE session, success or not. Its *attributes* are the same breakdown for that session, attached when the session ends (while one runs — Connection on — it has none). Use it when the session you are debugging is the last one. |
| **Failure Count** | How many sessions have failed since the integration was (re)loaded. It counts `connect` failures too, and a cuff that has just gone back to sleep fails there — so a count that creeps up a little every day is normal; one that rises on every poll, or while measurements are not arriving, is what to look into. Read Last Failure's `failed_stage` before reading anything into the number. |
| **Last Readout** | When a poll last **decoded a record**. A poll that connected but came back with no record does not move it — so "Duration updates, Last Readout does not" means the cuff is reachable but the readout is coming back empty, or failing before it. |
| **Connection** (binary) | On **while a session is in progress**. It is not "the cuff is nearby": a cuff keeps its radio off between readings. |

To see the attributes: click the entity → ⋮ → **Attributes**, or in **Developer tools → States** search for the entity. In a template: `{{ state_attr('sensor.hem_7155t_a1b2_last_failure', 'likely_cause') }}` (the id is the model plus the last four hex digits of the cuff's MAC).

Home Assistant also records the Duration attributes with each state change, so the history of that sensor has the breakdown of every session, not only the last failed one. Both sensors start empty on a restart or reload; the history is where a failure from before it lives.

## Reading the attributes

The attributes are, in order. `failed_stage` is the shared name used across BLE integrations; `failed_detail` is this cuff's own name for that stage, present only when the two differ.

| Attribute | Meaning |
|---|---|
| `operation` | `poll` (a scheduled or triggered readout), `pairing` (the *Retry Pairing* button or an auto-pairing advertisement) or `time_sync`. |
| `success` | Whether the session completed. |
| `error`, `failed_stage`, `failed_detail`, `likely_cause` | Only on a failure: the exact message, the shared stage it escaped from, the cuff's own name for that stage, and one sentence on what that usually means. |
| `likely_cause_key` | Only when that sentence is the shared one every BLE integration uses (`connect.no_slot`, `link_lost`, `unreachable`, …): a stable name for it, so an automation can match the reading instead of the English text. A sentence written for the cuff carries no key. |
| `via`, `via_type`, `rssi`, `paths` | The radio the link went over (a proxy or a local adapter), the cuff's signal as that radio last saw it, and how many connectable radios currently see the cuff. `paths: 1` means there is no other radio to fall back to. On a `connect` failure `via` is the radio that was tried. |
| `advertised_via` | Only when it differs from `via`: the radio whose advertisement was strongest, which is the one Home Assistant tries first. The link ending up elsewhere is a failover — or, on a multi-proxy setup, the proxy that holds the bond. |
| `connect_s`, `services_s`, `pair_s`, `unlock_s`, `memory_open_s`, `time_sync_s`, `readout_s`, `device_info_s`, `registration_s`, `memory_close_s`, `disconnect_s` | Seconds spent in each stage, in the order they ran. A stage that did not run is absent. These keep the cuff's own names. |
| `connect_attempts`, `bonded_at_connect` | How many connects were tried (up to 3; a link that drops during the post-connect settle is retried), and whether the connect made the bond (a pairing session over a local adapter). |
| `adopted_link` | The poll ran over the link a pairing session had just opened, instead of connecting itself. |
| `memory_session_attempts` | How many tries the unlock + readout-session open took (up to 3). |
| `records` | How many latest records the readout came back with — one per user with data. `0` is a cuff with nothing stored: not a failure, and why the measurement entities did not move. |
| `time_sync_error`, `registration_error` | A clock write, or a pairing registration, that failed but did not stop the session. |

## Reading a failure

Start with `failed_stage` on Last Failure: it says how far the session got, in the shared vocabulary. `failed_detail` is the cuff's name for the same stage. `likely_cause` is a reading of the stage, the failure type and the radio situation; `error` is the exact message. `likely_cause_key`, when present, is the stable name of that reading.

### `connect`

The link never came up.
- *This is the cuff's normal state between readings.* It turns its radio off to save battery and wakes for a short window after a measurement (and while it shows the Bluetooth symbol). A poll that runs while the cuff has just gone back to sleep fails here; the next one after a measurement succeeds. Look further only when a sync **never** succeeds.
- *Check:* `rssi` and `paths`. `failed_detail: settle` (or `error` containing *settle*) = the cuff accepted the link and dropped it before encryption settled. `error` with *slot* = the proxy's connection slots are all in use.
- *Do:* Weak `rssi` (below about −85 dBm): move the cuff or add a proxy near it. *settle* on a multi-proxy setup: only the radio that paired holds the bond — check `via` (and `advertised_via`, when present) against the proxy the cuff was paired through, and pair again through the one it now uses. *slot*: fewer BLE devices per proxy, or another proxy.

### `session` (`failed_detail: services`)

Connected, but the cuff does not expose the GATT service this model profile expects.
- *Check:* The model chosen in the entry against the label on the cuff.
- *Do:* Wrong model: remove the entry and add the cuff again with the right one. Right model: a stale GATT cache on the proxy — the integration already clears it and retries; if it persists, restart the proxy.

### `auth` (`failed_detail: pair`)

Bonding with the cuff failed (`operation: pairing`).
- *Check:* *Could not enter key programming mode* = the cuff was not in pairing mode.
- *Do:* Hold the cuff's Bluetooth button until it shows the blinking **-P-**, then press *Retry Pairing* while it is still blinking.

### `auth` (`failed_detail: unlock`)

The cuff refused, or did not answer, the application-level unlock.
- *Check:* *pairing key mismatch* = the cuff no longer accepts the stored key. *No stored transport credential* = the entry has no credential for a model that needs one. *PIN or Key Missing* / *auth* = the cuff no longer accepts the stored **bond**.
- *Do:* All three usually mean the cuff was paired to another host since (the phone app takes the pairing over), or the bond was lost on our side (a re-flashed proxy, a changed adapter). Pair again: put the cuff in **-P-** and press *Retry Pairing*. A cuff that has to be re-paired every few days is being re-paired by the phone in between.

### `auth` (`failed_detail: memory_open`)

Unlocked, but the cuff refused to start a readout session.
- *Check:* Does it repeat? `memory_session_attempts: 3` means every try failed.
- *Do:* Once: ignore, the next poll usually succeeds. Every time: treat it like `unlock` — pair again.

### `transfer` (`failed_detail: readout`)

Failed while reading the record memory. This is the one stage that points at link quality — or at the memory map.
- *Check:* `rssi`, `via`, and whether it fails at the same point every time.
- *Do:* Once: move the cuff or the proxy it used (`via`), or add one. Every time, on a model listed as untested: the memory map for that model may be wrong — please [open an issue](https://github.com/eigger/hass-omron/issues) with a Bluetooth log ([capturing-bluetooth-logs.md](capturing-bluetooth-logs.md)).

### `finish` (`failed_detail: memory_close`) / `disconnect`

The records were read; only the session close failed. Harmless on its own. If the **next** connection is refused (`unlock` or `memory_open` failing right after), the cuff did not commit the session — pair again.

### Quick checks

- **`error: Session deadline reached`** — the session hung and was cut at the 3-minute bound. `failed_stage` says where. This is not the cuff: bleak's BlueZ backend has no timeout of its own, so a wedged `bluetoothd` or a proxy that died mid-session looks exactly like this. Restart the adapter (or the proxy) if it repeats.
- **`rssi` is low but `paths` is 2 or more** — another radio might do better; Home Assistant connects through the strongest advertisement, so the alternative is only used after a failure. Check `via` to see which one was used.
- **Everything fails at `connect` right after adding a proxy** — the proxy must be `active: true` in both `esp32_ble_tracker` and `bluetooth_proxy`; a passive proxy sees the cuff but cannot connect.
- **`connect_attempts` above 1 on successful polls** — the link dropped during the post-connect settle and the retry got through. Harmless once in a while; on most polls it is where a marginal link, or a proxy without the bond, shows first.
- **`time_sync_error` on every poll** — the records still come through; the cuff's clock is not being set. On models that sync the clock through the EEPROM this means the clock layout for the model is wrong — worth an issue with the model name.

## What the attributes cannot show

Three kinds of problem never reach the failure sensors, because the session did not fail — or never happened.

**The poll succeeded but no measurement appeared.** `success: true` and `records` is 1 or more: the latest record the cuff holds is the one already shown, so the new measurement went to a user slot this entry does not read — check which user the cuff was set to (the user switch on the cuff). `records: 0`: the cuff has nothing stored at all for the users read. Neither is a failure, which is why Last Failure did not move.

**No session was attempted.** The cuff was not seen by any radio when the poll was due, so nothing connected and Duration did not move. The poll returns the cached values and logs nothing at default level. Take a measurement — the cuff advertises for a while afterwards — and press *Refresh Data* while it shows the Bluetooth symbol.

**The action itself errored before any BLE traffic.** *Retry Pairing* with the cuff out of range fails immediately with *BLE device not available*, and *BLE session already in progress* means a poll is running: wait for Connection to turn off and try again.

## Intermittent failures

A poll that fails only sometimes is the reason Last Failure keeps its attributes: look there, not at Duration, which already shows the later success. Its `failed_stage` and `rssi` at the time of failure are what matter. If failures cluster at one `via`, that proxy is the problem; if `paths` was 1 each time, a second radio would have given a fallback.

## What to attach to an issue

1. The **Last Failure** attributes (Developer tools → States → the entity → copy the attributes block) and, if the failure is not the latest session, the **Duration** attributes from the history around that time.
2. The model chosen in the entry and the model printed on the cuff.
3. Which radio the cuff uses (`via` — proxy model and ESPHome version, or the adapter) if the failure is `connect` or `transfer`.
4. For a model that pairs but never reads, or reads the wrong values: a Bluetooth log from the phone app — [capturing-bluetooth-logs.md](capturing-bluetooth-logs.md) explains how and what it contains.

Debug logging is rarely needed; if asked, add `custom_components.omron: debug` under `logger:` and reproduce once.
