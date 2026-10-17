# Omron BLE Home Assistant Integration (hass-omron)

<!-- hass-standards:badges:start -->
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5?style=flat-square)](#installation)
[![Release](https://img.shields.io/github/v/release/eigger/hass-omron?style=flat-square)](https://github.com/eigger/hass-omron/releases)
[![Home Assistant minimum](https://img.shields.io/badge/HA-%E2%89%A5%202025.12.0-41BDF5?style=flat-square)](hacs.json)
[![Tests](https://img.shields.io/github/actions/workflow/status/eigger/hass-omron/tests.yml?branch=master&style=flat-square&label=Tests)](https://github.com/eigger/hass-omron/actions/workflows/tests.yml)
[![Validate](https://img.shields.io/github/actions/workflow/status/eigger/hass-omron/validate.yml?branch=master&style=flat-square&label=Validate)](https://github.com/eigger/hass-omron/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/eigger/hass-omron?style=flat-square)](LICENSE)
[![Analytics reporting installs](https://img.shields.io/badge/dynamic/json?style=flat-square&color=41BDF5&label=Usage&query=%24.omron.total&url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json)](https://analytics.home-assistant.io/)
<!-- hass-standards:badges:end -->

**English** | [한국어](README.ko.md)

| Device | Model |
| :---: | :---: |
| <img src="https://raw.githubusercontent.com/eigger/hass-omron/master/docs/images/bpm.jpg" width="280" alt="Omron HEM-7142T2"> | **HEM-7142T2** |

A HACS custom integration (not part of Home Assistant Core) to connect and poll data directly from Omron Bluetooth Low Energy (BLE) blood pressure monitors and weight scales.

## Supported Models

| Model | Global Model | Type | Verified |
| :--- | :--- | :--- | :---: |
| **HEM-6161T** | RS2 Intelli IT | Wrist | ✅ |
| **HEM-6232T** | BP6350/RS7 Intelli IT | Wrist | ✅ |
| **HEM-7142T2** | BP7150/M2 Intelli IT | Upper Arm | ✅ |
| **HEM-7146T2** | X2 Smart+ | Upper Arm | ✅ |
| **HEM-7151T** | BP5250/M3 Comfort | Upper Arm | ✅ |
| **HEM-7155T** | BP7350/M4 Intelli IT | Upper Arm | ✅ |
| **HEM-7156T** | BP7250 | Upper Arm | ✅ |
| **HEM-716BT2** | BP5255/M4 Smart | Upper Arm | ✅ |
| **HEM-7188T1** | M2+ / X2+ Connect | Upper Arm | ✅ |
| **HEM-7320T** | BP7450/M7 Intelli IT | Upper Arm | ✅ |
| **HEM-7322T** | BP786/M6 Intelli IT | Upper Arm | ✅ |
| **HEM-7343T** | BP5450/Platinum Series | Upper Arm | ✅ |
| **HEM-7374T1** | U738T (China) | Upper Arm | ✅ |
| **HEM-7380T1** | M7 Intelli IT AFib / X7 Smart AFib | Upper Arm | ✅ |
| **HEM-7382T1** | BP5465 | Upper Arm | ✅ |
| **HEM-7530T** | BP7900/Complete | Upper Arm | ✅ |
| **HEM-7600T** |	BP7000/EVOLV	| Upper Arm	| ✅ |
| **HN-300T2** | HN-300T2 | Scale | ✅ |
| **HBF-222T** | VIVA / BCM-500 | Scale | ✅ |

- **HN-300T2**: weight and BMI calculated from your configured height.
- **HBF-222T**: weight, body composition, scale-reported BMI, and stored measurement history.

> [!NOTE]
> Other Omron BLE devices might work by selecting a similar model during setup, but have not been formally tested. If your device does not work, please share Home Assistant debug logs and, if possible, a [Bluetooth stack log from your phone](docs/capturing-bluetooth-logs.md).

## ⚠️ Warning: Conflict with Official App
**Omron blood pressure monitors only support one paired device at a time.**
If you have already paired your monitor with the official Omron Connect smartphone app, you **must unpair/forget** the device from your phone's Bluetooth settings before connecting it to Home Assistant. Using both simultaneously is not supported by the hardware.

## Requirements

Home Assistant 2025.12.0 or newer (see `hacs.json`) and a Bluetooth adapter or ESPHome Bluetooth proxy.

## Installation

1. **HACS**: Add this repository (`eigger/hass-omron`) to HACS as a custom repository, or 
   **Manual**: Copy the `custom_components/omron` directory into your Home Assistant `custom_components` folder.
2. Restart Home Assistant.

## ⚠️ Important Notice

- It is **strongly recommended to use a Bluetooth proxy instead of a built-in Bluetooth adapter**.  
  Bluetooth proxies generally offer more stable connections and better range, especially in environments with multiple BLE devices.

> [!TIP]
> For hardware recommendations, refer to [Great ESP32 Board for an ESPHome Bluetooth Proxy](https://community.home-assistant.io/t/great-esp32-board-for-an-esphome-bluetooth-proxy/916767/31).  
- When using a Bluetooth proxy, it is strongly recommended to **keep the scan interval at its default value**.  
  Changing these values may cause issues with Bluetooth data transmission.
- **bluetooth_proxy:** must always have **active: true**.
- Shared proxy setup, recommended devices and the connection-problem checklist (Korean): [Bluetooth proxy guide](https://github.com/eigger/hass-repo-standards/blob/ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870/docs/bluetooth.md). Model-specific wake/pairing conditions and diagnostics are in [Troubleshooting](docs/troubleshooting.md#bluetooth-proxy-and-model-specific-conditions).

  Example (recommended configuration with default values):

  ```yaml
  esp32_ble_tracker:
    scan_parameters:
      active: true

  bluetooth_proxy:
    active: true
  ```
  
## Pairing & Configuration

Device setup and pairing are done entirely through the Home Assistant UI.

<p align="center">
  <img src="https://raw.githubusercontent.com/eigger/hass-omron/master/docs/images/pairing.jpg" width="400" alt="Pairing Mode">
</p>

1. **Enter Pairing Mode**:
   - On your Omron blood pressure monitor, press and **hold the Bluetooth button** (or the respective connection button depending on your model) for 3-5 seconds.
   - The display should show a blinking **`-P-`** symbol. This means the device is ready to pair.
2. **Add Integration**:
   - In Home Assistant, go to **Settings** > **Devices & Services**.
   - Home Assistant should automatically discover the "Omron" device via Bluetooth. Click **Configure**.
   - If it wasn't auto-discovered, click **Add Integration** and search for "Omron".
3. **Select Your Model**:
   - Select your exact Omron model from the dropdown list.
4. **Finalize Pairing**:
   - Make sure `-P-` is still blinking on the monitor.
   - Click **Submit** in Home Assistant. 
   - HA will securely perform the pairing sequence (programming its pairing key or performing OS-level bonding).
   - The monitor screen will display an `[OK]` symbol when successful.

## Documentation

- [Entities, categories and polling behavior](docs/entities.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Capturing Bluetooth logs](docs/capturing-bluetooth-logs.md)
- [HEM-7374T1 notes](docs/hem-7374t1.md)
- [Roadmap](docs/roadmap.md)
- [Body composition scale notes](docs/body-composition-scales-notes.md)
- [HN-300T2 memory map](docs/hn-300t2-memory-map.md)
- [Development and tests](CONTRIBUTING.md)

## Troubleshooting

**[Troubleshooting](docs/troubleshooting.md)** — how to read the **Last Failure** and
**Duration** sensors' attributes (`failed_stage`, `likely_cause`, the radio, the
per-stage timings), what each failed stage means, and what to attach to an issue.

- **`Pairing failed: Could not enter key programming mode`**: Make sure the device is actively showing the blinking `-P-` before clicking Submit. If it timed out and turned off, hold the button on the device to trigger `-P-` again.
- **`Connection terminated by peer`**: BLE interference or weak signal. Try moving the device closer to your Home Assistant Bluetooth adapter or use a USB extension cable for your BLE dongle.
- **Sensor values not updating**: The device turns off its Bluetooth radio to save battery. It turns on briefly after a measurement. Ensure you have taken a *new* measurement after pairing to test the sync.
- **Data doesn't show up after changing batteries**: Changing batteries may reset the internal clock on the device. Re-pairing or syncing time (via the app, then re-pairing to HA) might be necessary on older models.


## References

- [omblepy (userx14)](https://github.com/userx14/omblepy)

## Support

- Bug reports: [Issues](https://github.com/eigger/hass-omron/issues)
- Questions and suggestions: [Discussions](https://github.com/eigger/hass-omron/discussions)
- Development and tests: [CONTRIBUTING.md](CONTRIBUTING.md)

## License

See [LICENSE](LICENSE).
