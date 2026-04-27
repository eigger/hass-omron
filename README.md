# Omron BLE Home Assistant Integration (hass-omron)

[![GitHub Release](https://img.shields.io/github/v/release/eigger/hass-omron?style=flat-square)](https://github.com/eigger/hass-omron/releases)
[![License](https://img.shields.io/github/license/eigger/hass-omron?style=flat-square)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![integration usage](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=usage&suffix=%20installs&cacheSeconds=15600&query=%24.omron.total&url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json)

<p align="center">
  <img src="https://raw.githubusercontent.com/eigger/hass-omron/master/docs/images/bpm.jpg" width="400" alt="Omron BPM Integration">
</p>

A custom integration for Home Assistant to connect and poll data directly from Omron Bluetooth Low Energy (BLE) blood pressure monitors.

## 💬 Feedback & Support

🐞 Found a bug? Let us know via an [Issue](https://github.com/eigger/hass-omron/issues).  
💡 Have a question or suggestion? Join the [Discussion](https://github.com/eigger/hass-omron/discussions)!

## Supported Models

- HEM-7142T2
- HEM-7320T
- HEM-7322T

- *Other Omron BLE devices might work by selecting a similar model, but have not been formally tested. If your device does not work, please share the debug logs for further investigation.*

## ⚠️ Warning: Conflict with Official App
**Omron blood pressure monitors only support one paired device at a time.**
If you have already paired your monitor with the official Omron Connect smartphone app, you **must unpair/forget** the device from your phone's Bluetooth settings before connecting it to Home Assistant. Using both simultaneously is not supported by the hardware.

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

## How It Works

- The integration actively polls the blood pressure monitor in the background.
- Whenever you take a measurement, the device stores it in its internal EEPROM memory.
- As long as the device is in range, Home Assistant will periodically connect via Bluetooth (every 5 minutes by default) and download the latest unread records.
- HA creates automatically updated sensor entities for:
  - **Systolic Blood Pressure (mmHg)**
  - **Diastolic Blood Pressure (mmHg)**
  - **Heart Rate / Pulse (bpm)**
  - **Pulse Pressure (mmHg)**
  - **Estimated Mean Arterial Pressure (mmHg)**
  - **Shock Index (ratio)**
  - **Rate Pressure Product (mmHg*bpm)**
  - **Blood Pressure Category** (ACC/AHA text label)
  - **Measurement Timestamp**
  - **RSSI / Signal Strength (diagnostic)**
  - **Last Poll Duration (diagnostic)**

## User names (multi-user models) & last values

- Some Omron models store **two (or more) user profiles** on the cuff. After you choose such a model, the setup flow asks for a **display name per profile** (defaults: `user1`, `user2`, …). Those names are used in sensor labels and in the internal sensor key (slug), so they replace the old `_user1` / `_user2`-style suffix in `entity_id`s when you customize them.
- **Single-user models** do not show this step.
- **Changing a name** later under **Configure** re-creates sensors for that profile slot (new `entity_id`, new long-term statistics for those entities).
- **Last value after restart**: non-diagnostic measurement sensors restore their last recorded state from Home Assistant so they can show the previous reading when the cuff is off or not yet polled. The **Connection** binary sensor still reflects whether a poll connection is active. Stale readings are possible if the device has not synced—use the connection sensor and timestamps as context.

## Troubleshooting

- **`Pairing failed: Could not enter key programming mode`**: Make sure the device is actively showing the blinking `-P-` before clicking Submit. If it timed out and turned off, hold the button on the device to trigger `-P-` again.
- **`Connection terminated by peer`**: BLE interference or weak signal. Try moving the device closer to your Home Assistant Bluetooth adapter or use a USB extension cable for your BLE dongle.
- **Sensor values not updating**: The device turns off its Bluetooth radio to save battery. It turns on briefly after a measurement. Ensure you have taken a *new* measurement after pairing to test the sync.
- **Data doesn't show up after changing batteries**: Changing batteries may reset the internal clock on the device. Re-pairing or syncing time (via the app, then re-pairing to HA) might be necessary on older models.

## References

- [omblepy (userx14)](https://github.com/userx14/omblepy)

