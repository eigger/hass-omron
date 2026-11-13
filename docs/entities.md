# Entities, categories and measurement behavior

Moved from the main README (detailed reference).

## How It Works

- The integration actively polls the blood pressure monitor in the background.
- Whenever you take a measurement, the device stores it in its internal EEPROM memory.
- As long as the device is in range, Home Assistant will periodically connect via Bluetooth (every 5 minutes by default) and download the latest unread records.
- To turn scheduled polling off, set the **Update Interval** to `0` (in the integration's options, or on the model-selection step when adding the device). Nothing then connects on a timer; the **Refresh Data** button and the cuff's own transfer signal still fetch readings. Some cuffs never signal a transfer, so with `0` those update only when you press **Refresh Data**.
- HA creates automatically updated sensor entities for:
  - **Systolic Blood Pressure (mmHg)**
  - **Diastolic Blood Pressure (mmHg)**
  - **Heart Rate / Pulse (bpm)**
  - **Pulse Pressure (mmHg)**
  - **Estimated Mean Arterial Pressure (mmHg)**
  - **Shock Index (ratio)**
  - **Rate Pressure Product (mmHg*bpm)**
  - **Blood Pressure Category** (ACC/AHA)
  - **Measurement Timestamp**
  - **RSSI / Signal Strength (diagnostic)**
  - **Duration (diagnostic)**: seconds of the last BLE session; its attributes are that session's breakdown (stage timings, the radio used, and on a failure the stage and a likely cause)
  - **Last Failure (diagnostic)**: when a BLE session last failed, with that session's breakdown as attributes — kept until the next failure
  - **Failure Count (diagnostic)**: failed BLE sessions since the integration was (re)loaded
  - **Last Readout (diagnostic)**: when a poll last decoded a record
- **Binary & Diagnostic Sensors** (Status from the last measurement):
  - **Cuff Fit**: `On` (problem) indicates the cuff was **too loose** (improper fit); `Off` means it fitted properly.
  - **Body Movement**: `On` indicates that movement was detected during the measurement.
  - **Irregular Pulse**: `On` indicates that an irregular heart rhythm was detected.
  - **Improper Position**: `On` indicates the device was not at heart level (wrist models).
  - **Connection**: `On` while Home Assistant is actively communicating with the monitor.
- **Scales** get the same diagnostic sensors, plus:
  - **Weight (kg)** and **Measurement Timestamp**
  - **HBF-222T body composition**: body fat, skeletal muscle, visceral fat level, basal metabolic rate and the BMI reported by the scale. Composition sensors remain unknown when the scale has no composition result for a reading; the on-scale user profile must already be configured.
  - **Height** (number entity, under *Configuration*): your height in cm, used for BMI. Enter a value from `100` to `220` cm (0.1 cm steps); `0` clears it. It is stored in Home Assistant only and restored after a restart; nothing is written to the scale.
  - **BMI (kg/m²)** and **BMI Category (WHO)**, on weight-only scales such as the HN-300T2. Both are computed from the latest weight and the Height entity, and stay `unknown` until both are set. Their attributes show the `weight_kg` and `height_cm` used. Changing the Weight sensor's display unit does not affect them.

## BMI Categories (WHO)

The **BMI Category** sensor uses the **WHO adult** bands on the BMI rounded to one decimal (the value the BMI sensor shows).

| Category | BMI (kg/m²) |
| :--- | :--- |
| **Underweight** | < 18.5 |
| **Normal** | 18.5 – 24.9 |
| **Overweight** | 25.0 – 29.9 |
| **Obesity class 1** | 30.0 – 34.9 |
| **Obesity class 2** | 35.0 – 39.9 |
| **Obesity class 3** | ≥ 40.0 |

> BMI and these bands are adult screening criteria, not a diagnosis. They do not apply to children and teenagers or during pregnancy, and they misjudge very muscular people such as athletes.

## Blood Pressure Categories (ACC/AHA)

The **Blood Pressure Category** sensor classifies readings according to the **ACC/AHA 2017 Guidelines**. If a measurement's systolic and diastolic values fall into different categories, the higher (more severe) category is assigned.

| Category | Systolic (mmHg) | | Diastolic (mmHg) |
| :--- | :--- | :---: | :--- |
| **Normal** | < 120 | and | < 80 |
| **Elevated** | 120 – 129 | and | < 80 |
| **Hypertension Stage 1** | 130 – 139 | or | 80 – 89 |
| **Hypertension Stage 2** | ≥ 140 | or | ≥ 90 |
| **Hypertensive Crisis** | > 180 | or | > 120 |

## Device note (text entity)

- Each integration entry exposes a **Text** entity (**Device nickname**) under *Configuration* for a free-form note. It does **not** change the Device Registry name, entity IDs, or integration options; Home Assistant restores the last value like other restore-state entities.

## User names (multi-user models) & last values

- Some Omron models store **two (or more) user profiles** on the cuff. After you choose such a model, the setup flow asks for a **display name per profile** (defaults: `user1`, `user2`, …). Those names are used in sensor labels and in the internal sensor key (slug), so they replace the old `_user1` / `_user2`-style suffix in `entity_id`s when you customize them.
- **Single-user models** do not show this step.
- **Changing a name** later under **Configure** keeps the sensors of that profile slot: their `entity_id`, history and long-term statistics stay, and only the display name changes. If an entity left over from an earlier name change already uses a new name, that profile is left as it was and its sensors are not moved. Names can also be swapped between profiles. An entity already left over from an older name change (before this was supported) is not recovered.
- **Last value after restart**: non-diagnostic measurement sensors restore their last recorded state from Home Assistant so they can show the previous reading when the cuff is off or not yet polled. The **Connection** binary sensor still reflects whether a poll connection is active. Stale readings are possible if the device has not synced—use the connection sensor and timestamps as context.

## Measurements taken between polls

A poll normally surfaces only the newest reading. On the models listed below, the integration also reads the measurements stored since the previous poll, so taking three readings between two polls no longer loses two of them.

- Each older reading is published, oldest first, as its own state change of the measurement sensors (the **Measurement Timestamp** sensor carries the cuff's time for each). The state history shows them at poll time, not at the time of the reading.
- Every reading, including the newest, is also fired as an `omron_measurement` event, which keeps the exact time. Use it in automations or to record readings yourself:

  | Field | Meaning |
  |---|---|
  | `device_id`, `address` | the device |
  | `user`, `user_alias` | profile slot (1-based) and its display name |
  | `sequence` | the cuff's running record number; use it to ignore a repeated event |
  | `measured_at` | time of the reading with your time zone applied |
  | `device_time` | the cuff's own clock reading, unmodified |
  | `sys`, `dia`, `bpm` | the reading |
  | `irregular_pulse`, `body_movement`, `cuff_loose`, `improper_position` | status flags (`cuff_loose` is true when the cuff was too loose) |

- Setting this up (or turning it on) never replays what is already in the cuff: it starts from the next reading. Up to 30 readings per user are read per poll; a longer backlog is read over the next polls. Readings the cuff overwrote before a poll can no longer be recovered, and a warning says how many.
- It is **off by default**. Turn it on under **Configure** with **Publish every measurement stored since the last poll** (the option only appears for the models below). While it is off, polls do not read anything extra; turning it on starts from the cuff's current reading, not from what piled up meanwhile.
- Models: HEM-7361T, HEM-7342T, HEM-7155T, HEM-7155T-K4, HEM-1026T2, HEM-7153JT, HEM-7150T, HEM-7157T, HEM-7151T, HEM-7142T2, HEM-6161T, HEM-6231T, HEM-7530T, HEM-7136T and HEM-6232T. Only the HEM-7361T was checked against a real index dump; for the others the counter position comes from the manufacturer's memory map, so report a problem with a debug log (`Index block` and `record counter` lines). On the HEM-7342T a TruRead set arrives as its three individual readings in the events only; the sensors show the set's average. Models whose cuff keeps no record counter, and scales, are not covered yet. With [users](#users-on-single-slot-devices) registered, the synced readings are also assigned to them.

## Users on single-slot devices

The HN-300T2 scale and blood pressure monitors that keep a single user profile (for example the HEM-7600T) cannot tell people apart: every measurement looks the same whoever took it. If several people share such a device, add them as **users**. A user is a sub-entry of the device (**Add user** on the integration page) with a sub-device of its own, and each new measurement is given to one user, whose sensors then show it. The sensors of the device itself are unchanged and still show every measurement, so they duplicate the user's sensors under a different device name.

With no user added, nothing here applies and the integration behaves as before. Multi-profile cuffs (the ones with the per-slot names above) and body-composition scales (HBF) do not offer users.

**Sensors of each user**

| Device | Sensors | Attributes |
|---|---|---|
| Scale | Weight, BMI (from the **Height** you enter for that user) | `assignment`, `measured_at`, `weight_kg`, `height_cm` (BMI) |
| Blood pressure monitor | Systolic, Diastolic, Pulse, Measured At, and the derived Pulse Pressure, Estimated MAP, BP Category (ACC/AHA), Shock Index, Rate Pressure Product | `assignment` (`rule`), `measured_at`, `irregular_pulse`, `body_movement` |

A monitor's user also gets the status flags of their last measurement as binary sensors: **Irregular Pulse**, **Body Movement**, **Cuff Fit** and **Improper Position**, with the same device class, icons and meaning as the device's own (**Cuff Fit** is `On` when the cuff was too loose, as there). Only the flags the model's records carry are created: HEM-6401T and HEM-6410T record just the first two, the other single-slot monitors all four. The derived sensors use the same formulas, units, rounding and guards as the device's own (a pulse-based metric is unknown for a measurement without a pulse; the pressure-based ones are unknown unless systolic is above diastolic). Every one of these is unknown until the user has a measurement.

The sensors keep their last values across restarts. A measurement that cannot be given to one user is given to nobody, leaves every user's sensors alone and is announced as an `omron_unassigned_measurement` event:

| Field | Meaning |
|---|---|
| `device_id`, `address` | the device |
| `measured_at`, `device_time`, `sequence` | when it was taken (your time zone / the device's own clock) and the cuff's record number if it has one |
| `weight` or `sys`, `dia`, `bpm`, `irregular_pulse`, `body_movement` | the measurement |
| `reason` | `ambiguous` (fits several users, or none clearly), `no_match` (fits no user's rules), `no_time` (a user's time window needs a time the record does not have), `out_of_range` (weight-based: even the closest user is too far away) |
| `candidates` | per user: for weight, the reference weight, the difference and the tolerance in kg; for rules, which rules held, failed or could not be checked |

**How a measurement is assigned.** The device uses one strategy for all its users; a scale lets you choose it under **Configure** (**Assign measurements by**), a blood pressure monitor always uses rules. Changing the strategy forgets the weights learned so far. Users keep what was entered for the old strategy, so edit each one afterwards: by weight a user needs a usual weight (without it they are never assigned a measurement, and a warning is logged at startup), by rules they need their rules (a user with none fits every measurement).

- **Weight** (scale only). Enter each user's usual weight. A measurement goes to the user whose reference weight it is closest to, provided it is within a tolerance of 3 % of that weight (kept between 1.0 and 4.0 kg; the percentage can be set to 1 to 5 % under **Configure**) and at least 1.0 kg closer than the second-closest user. Slow drifts are followed: the reference becomes a weighted average of the user's last five assigned weights, the newest counting most. A weight just outside the tolerance still goes to the closest user while it is within twice the tolerance and 5 kg (`nearest`); anything further away, or too close to call, is not assigned. **People whose usual weights are less than about 5.5 kg apart (at 75 kg) will often be left unassigned**; a guest within reach of a user is given to that user. The constants are design assumptions, not yet checked against real scale data. The measured time is never used.
- **Rules** (blood pressure monitor, or scale). These are not health thresholds; they only decide whose measurement it is. The form has a section **When do they measure?** where every time window is added with the **Add** button (no limit, in the order you add them). For each window pick its **Start** from the list (every half hour; a time that is not in the list, such as 07:15, can be typed) and its **Duration** (30 minutes, 1, 1.5, 2, 3 or 4 hours); the end is worked out, so 22:00 for 3 hours ends at 01:00, a window past midnight. A window includes its start and excludes its end. If the duration is left empty it is 1 hour. With no window the time is not used), and a collapsed section **Usual range (optional)** with value ranges (weight for a scale, systolic and diastolic for a monitor); all are optional. For a family, one person might measure from 06:00 to 08:00 and another from 21:00 to 23:00. A measurement goes to the one user whose rules all hold, to nobody if none or several do. A user with no rules at all fits everything (you are warned when adding one, and warned more strongly when another user has none either, because then no measurement can be assigned to anyone). Internally the windows are stored as text (`06:00-09:00, 18:00-21:00`) in the order of the list; editing a user shows every stored window in the list as a start and a duration, so users saved with 4.6.0-beta.1 or beta.2 keep all their windows; a stored length that is not in the duration list (for example 2 h 15 min) is added to the list for that form and is kept as it is. Saving two users with overlapping time windows is allowed after a warning; where they overlap, a measurement that fits both is not assigned. Value ranges may overlap, which also leaves such measurements unassigned.

**Limits to know about**

- A device is read by polling, so what is assigned is what the device reports at the time of the poll. Without **Publish every measurement stored since the last poll** (all scales, and monitors such as the HEM-7600T, which have no record counter) **only the newest measurement of each poll is assigned**: if several people measure between two polls, the earlier measurements are not assigned and not announced. With that option on (models listed above), every stored measurement is assigned, oldest first by the cuff's record number. Registering a user does not change that option.
- **What the device held before you added the first user is not assigned.** Adding the first user notes the time; on the first poll, a record the device says was taken before then (more than 5 minutes before, to allow for a device clock that is slightly off) is treated as left over and only marks where the device stands. A measurement taken after adding the user is assigned on that same poll, by the users' rules. A record without a time is never assigned on the first poll.
- A measurement is told from the previous one by its time and values. If the device sends no time, two identical measurements in a row look like one, and the second is missed. Rules with time windows cannot decide a measurement without a time (`no_time`).
- Time windows use the **device's clock**, which can be wrong; if it is, every time rule is wrong. The HEM-7600T supports clock sync.
- Value ranges are only a hint (blood pressure varies a lot within one person and ranges of different people overlap) and can assign a measurement to the wrong person. Time windows only work when everyone's routine is regular.
- For a monitor, the device's own sensors may show a reading that was just taken live over Bluetooth while the user's sensors show the record the monitor stored; the two can differ for a moment or by a reading. Only the stored record is assigned.
- Assignment is not undone: a measurement given to the wrong user (or left unassigned) is not moved later, and there is no way to pick the current user. The home-presence of people is not used.
- Removing a user removes their sensors and what was learned about them; their earlier states stay in the Home Assistant history. Removing the last user forgets where the device stood, so adding a user again starts as described above.
