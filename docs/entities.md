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
  - **Cuff Fit**: `On` indicates the cuff was **not wrapped correctly** (improper fit).
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
- **Changing a name** later under **Configure** re-creates sensors for that profile slot (new `entity_id`, new long-term statistics for those entities).
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
  | `irregular_pulse`, `body_movement`, `cuff_loose`, `improper_position` | status flags |

- Setting this up (or turning it on) never replays what is already in the cuff: it starts from the next reading. Up to 30 readings per user are read per poll; a longer backlog is read over the next polls. Readings the cuff overwrote before a poll can no longer be recovered, and a warning says how many.
- It is **off by default**. Turn it on under **Configure** with **Publish every measurement stored since the last poll** (the option only appears for the models below). While it is off, polls do not read anything extra; turning it on starts from the cuff's current reading, not from what piled up meanwhile.
- Models: HEM-7361T, HEM-7342T, HEM-7155T, HEM-7155T-K4, HEM-1026T2, HEM-7153JT, HEM-7150T, HEM-7157T, HEM-7151T, HEM-7142T2, HEM-6161T, HEM-6231T, HEM-7530T, HEM-7136T and HEM-6232T. Only the HEM-7361T was checked against a real index dump; for the others the counter position comes from the manufacturer's memory map, so report a problem with a debug log (`Index block` and `record counter` lines). On the HEM-7342T a TruRead set arrives as its three individual readings in the events only; the sensors show the set's average. Models whose cuff keeps no record counter, and scales, are not covered yet.
