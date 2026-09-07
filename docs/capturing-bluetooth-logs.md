# Capturing Bluetooth logs

A Home Assistant debug log shows what this integration did. A Bluetooth stack log shows
what actually went over the air — including the security handshake, which is where most
of the hard bugs in this project have turned out to live.

The most useful capture is the **official OMRON connect app doing a successful sync**,
because it gives us a working reference to compare against. A cuff pairs to one device at
a time, so remove it from Home Assistant first.

---

## Read this before you capture anything

> [!CAUTION]
> A Bluetooth stack log is a recording of the radio. It contains your blood-pressure
> readings in cleartext and the encryption keys from pairing. Do not post one without
> reading this section.

It contains:

- **Your measurements.** Systolic, diastolic, pulse and timestamps travel in cleartext
  vendor frames. Anyone who reads the file can read your readings.
- **BLE bond keys.** The pairing exchange carries the LTK and IRK. Publishing them lets
  someone who is in radio range impersonate your phone to the cuff.
- **Hardware addresses** of the cuff, your phone, and any other Bluetooth device that was
  active during the capture.

On iOS the problem is bigger, because the only way to get the log is a full
`sysdiagnose`, which packages a great deal of unrelated data from your phone.

**You do not have to post a raw capture.** Three options, all fine:

1. Post the raw file, accepting the above.
2. Post only a sanitized summary (see [Sanitizing a capture](#sanitizing-a-capture)).
3. Say you would rather not share it. Ask in the issue and we will work out something
   else — several problems in this project were solved from sanitized summaries alone.

If you do post a raw capture, consider taking a throwaway measurement first so the file
holds nothing you mind sharing.

---

## Android

### 1. Turn on the stack log

**Settings** → **About phone** → tap **Build number** seven times to unlock
**Developer options**, then:

- **Developer options** → enable **USB debugging**
- **Developer options** → **Bluetooth stack log** (also called **Bluetooth HCI snoop
  log**) → choose **Detailed** / **Enabled**. Do **not** pick the filtered mode.

Turn Bluetooth **off and on again** so the setting takes effect.

> [!TIP]
> **On recent Pixel and Samsung builds the menu toggle is not always enough** — it can
> silently record nothing, or only a filtered subset. If your capture comes out empty or
> the menu does not match the names above, set it over adb instead and reboot:
>
> ```bash
> adb shell settings put global bluetooth_btsnoop_default_mode full
> adb reboot
> ```
>
> Afterwards, turn it back off the same way:
>
> ```bash
> adb shell settings put global bluetooth_btsnoop_default_mode disabled
> adb reboot
> ```

### 2. Reproduce the problem

In the **OMRON connect** app:

1. put the cuff into pairing mode (`-P-` blinking) and pair it
2. take a measurement and let the app sync it

If you can, also do **one ordinary sync afterwards** — take a measurement, then open the
app without touching the pairing button. That reconnect is what fails for most of the
open issues here, so it is the most valuable part of the capture.

### 3. Pull the log

Turn the stack log back off, toggle Bluetooth off and on, then on a PC with
[platform-tools](https://developer.android.com/tools/releases/platform-tools):

```bash
adb devices
```

The first time, the phone shows as `unauthorized` — unlock it, tap **Allow USB
debugging**, and run it again until it shows `device`. Then:

```bash
adb bugreport
```

This writes a zip into the current directory, for example
`dumpstate-2026-06-17-19-37-02.zip`. With more than one device attached, add
`-s <serial>`.

Unzip it and look for `btsnoop_hci.log`. The path moves between Android versions; the two
common ones are:

```
FS/data/log/bt/btsnoop_hci.log
FS/data/misc/bluetooth/logs/btsnoop_hci.log
```

If you find `btsnooz_hci.log` instead (note the **z**), that is the compressed ring buffer
from the bug report, not a full capture — it has the advertisements but the connection
payloads are stripped, so it cannot be used. Use the adb method above to get a real one.

---

## iPhone

iOS can capture the same layer, but it needs a profile from Apple first.

### 1. Install Apple's Bluetooth logging profile

On the iPhone, open Safari and go to Apple's **Profiles and Logs** page:

<https://developer.apple.com/bug-reporting/profiles-and-logs/>

Downloading requires signing in with an Apple ID. The list is long and covers every
Apple platform - the one you want is **Bluetooth for iOS/iPadOS**, which downloads as
`iOSBluetoothLogging.mobileconfig`.

Install it under **Settings** → **General** → **VPN & Device Management** → tap the
downloaded profile → **Install**.

### 2. Reboot the iPhone

The profile only takes effect after a restart.

### 3. Reproduce the problem

Same as Android above: pair in the OMRON connect app, take a measurement, let it sync,
and if possible do one ordinary sync afterwards without touching the pairing button.

### 4. Trigger a sysdiagnose

Right afterwards, hold **Volume Up + Volume Down + Side button** together for about 1.5
seconds and release. You will feel a short vibration. The phone then takes five to ten
minutes to write the log in the background.

Find it under **Settings** → **Privacy & Security** → **Analytics & Improvements** →
**Analytics Data**, as an entry named `sysdiagnose_<date>_….tar.gz`.

### 5. Extract only the Bluetooth trace

> [!WARNING]
> **Do not upload the sysdiagnose itself.** It is several hundred MB of unrelated
> personal data from your phone.

Extract the `.tar.gz` (7-Zip on Windows, double-click on macOS) and look for a
`bluetooth` folder holding one or more `.pklg` files. Those are the packet traces, and
they are the only files worth attaching.

To read one yourself: **PacketLogger** on macOS (part of Additional Tools for Xcode), or
Wireshark on any platform.

---

## Sanitizing a capture

If you would rather not share the raw file, this prints the security handshake with no key
material — names, status codes and reasons only. It works on Android `btsnoop_hci.log` as
well as iOS `.pklg`:

```bash
btmon -r capture.log | grep -E "SMP: |IO capability|OOB data|Authentication requirement|Max encryption key size|key distribution|Encryption Change|Encryption: |Status: |Reason: "
```

`btmon` ships with BlueZ on Linux. That output alone has been enough to settle several
questions in this repo.

Note that modern Android emits **Encryption Change v2** (HCI event `0x59`), not the older
`0x08`. Tools that only decode `0x08` will report "no encryption result captured" even
though the capture is fine.

---

## What to include when you post

- your cuff's model code (the `HEM-…` number on the label, and the retail name)
- phone model and OS version
- which of the steps above you performed, and what the cuff displayed
- the Home Assistant debug log covering the same period, if you have one

Open a [GitHub issue](https://github.com/eigger/hass-omron/issues) with those.
