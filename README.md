<!-- hass-standards:header:start -->
# Omron BLE Home Assistant Integration (hass-omron)

A HACS custom integration (not part of Home Assistant Core) to connect and poll data directly from Omron Bluetooth Low Energy (BLE) blood pressure monitors and weight scales.

**English** | [한국어](README.ko.md)

<!-- hass-standards:badges:start -->
[![Release](https://img.shields.io/github/v/release/eigger/hass-omron?style=flat)](https://github.com/eigger/hass-omron/releases)
[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5?style=flat)](#installation)
[![Home Assistant minimum](https://img.shields.io/badge/HA-%E2%89%A5%202025.12.0-41BDF5?style=flat)](hacs.json)
[![Tests](https://img.shields.io/github/actions/workflow/status/eigger/hass-omron/tests.yml?branch=master&style=flat&label=Tests)](https://github.com/eigger/hass-omron/actions/workflows/tests.yml)
[![Validate](https://img.shields.io/github/actions/workflow/status/eigger/hass-omron/validate.yml?branch=master&style=flat&label=Validate)](https://github.com/eigger/hass-omron/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/eigger/hass-omron?style=flat)](LICENSE)
[![Downloads](https://img.shields.io/github/downloads/eigger/hass-omron/total?style=flat&color=41BDF5&label=Downloads)](https://github.com/eigger/hass-omron/releases)
<!-- hass-standards:badges:end -->
<!-- hass-standards:header:end -->

The Downloads badge counts GitHub release asset downloads across all releases, not active installations.

| Device | Model |
| :---: | :---: |
| <img src="https://raw.githubusercontent.com/eigger/hass-omron/master/docs/images/bpm.jpg" width="280" alt="Omron HEM-7142T2"> | **HEM-7142T2** |

## Supported Models

| Model | Global Model | Type | Verified |
| :--- | :--- | :--- | :---: |
| **HEM-6161T** | RS2 Intelli IT | Wrist | ✅ |
| **HEM-6232T** | BP6350/RS7 Intelli IT | Wrist | ✅ |
| **HEM-7142T2** | BP7150/M2 Intelli IT / U705T (China) | Upper Arm | ✅ |
| **HEM-7146T2** | X2 Smart+ | Upper Arm | ✅ |
| **HEM-7151T** | BP5250/M3 Comfort | Upper Arm | ✅ |
| **HEM-7155T** | BP7350/M4 Intelli IT | Upper Arm | ✅ |
| **HEM-7156T** | BP7250 | Upper Arm | ✅ |
| **HEM-716BT2** | BP5255/M4 Smart | Upper Arm | ✅ |
| **HEM-7188T1** | M2+ / X2+ Connect | Upper Arm | ✅ |
| **HEM-7196T1** | M4 Connect AFib / X4 Connect AFib | Upper Arm | ✅ |
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

## Requirements

Home Assistant 2025.12.0 or newer (see `hacs.json`) and a Bluetooth adapter or ESPHome Bluetooth proxy (a Bluetooth proxy is strongly recommended for connection stability and range).

## Installation

<!-- hass-standards:installation:start -->
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=eigger&repository=hass-omron)
<!-- hass-standards:installation:end -->

1. **HACS**: With HACS installed, open HACS, search for **Omron**, open the integration, and download it. This integration is included in the default HACS repository list, so no custom repository is needed.
   **Manual**: Alternatively, copy the `custom_components/omron` directory into your Home Assistant `custom_components` folder.
2. Restart Home Assistant.

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

## Quick Start

Recommended ESPHome Bluetooth proxy configuration (keep the default scan interval):

```yaml
esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true
```

## Important Limitations

- **One paired host at a time**: Omron blood pressure monitors only support one paired device at a time. If you have already paired your monitor with the official Omron Connect smartphone app, you **must unpair/forget** the device from your phone's Bluetooth settings before connecting it to Home Assistant. Using both simultaneously is not supported by the hardware.
- **Bluetooth proxy requirements**: `bluetooth_proxy` must always have `active: true`, and `scan_parameters` should remain at default intervals. For hardware recommendations, see [Great ESP32 Board for an ESPHome Bluetooth Proxy](https://community.home-assistant.io/t/great-esp32-board-for-an-esphome-bluetooth-proxy/916767/31). Proxy setup, recommended devices and the connection-problem checklist: [Bluetooth proxy setup](https://github.com/eigger/blesession/blob/main/docs/bluetooth.md). Model-specific wake/pairing conditions, error-by-error fixes, and diagnostics (`Last Failure`, `Duration`) are in [Troubleshooting](docs/troubleshooting.md).
- **Unverified models**: Models not marked in the Supported Models table are unverified.

## Documentation

- [Entities, categories and polling behavior](docs/entities.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Capturing Bluetooth logs](docs/capturing-bluetooth-logs.md)
- [HEM-7374T1 notes](docs/hem-7374t1.md)
- [Body composition scale notes](docs/body-composition-scales-notes.md)
- [HN-300T2 memory map](docs/hn-300t2-memory-map.md)
- [Roadmap](docs/roadmap.md)
- [Development and tests](CONTRIBUTING.md)
- Reference: [omblepy (userx14)](https://github.com/userx14/omblepy)

<!-- hass-standards:support:start -->
## Support

- [Issues](https://github.com/eigger/hass-omron/issues): Report bugs or request features.
- [Discussions](https://github.com/eigger/hass-omron/discussions): Ask questions and share your experience.
<!-- hass-standards:support:end -->

<!-- hass-standards:related-projects:start -->
## Related Projects

- [blesession](https://github.com/eigger/blesession): Shared BLE sessions and Bluetooth proxy guidance used by this integration.
- [omblepy](https://github.com/userx14/omblepy): Reference implementation for Omron BLE communication.
<!-- hass-standards:related-projects:end -->

<!-- hass-standards:license:start -->
## License

Released under the [MIT License](LICENSE).
<!-- hass-standards:license:end -->
