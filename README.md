# AstralPool Halo Cloud

<p align="center">
  <img src="docs/branding/logo-final.png" alt="AstralPool Halo Cloud logo" width="180">
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Home Assistant Custom Integration](https://img.shields.io/badge/Home%20Assistant-Custom%20Integration-41BDF5.svg)](https://www.home-assistant.io/)

A Home Assistant custom integration, plus its supporting Python client library, for **AstralPool Halo chlorinators** using the **vendor cloud connection** for normal operation instead of BLE.

> [!WARNING]
> **DEEP DEVELOPMENT WARNING**
>
> This project is **unofficial**, **reverse-engineered**, and still under **deep active development**.
> It is **not production-ready** and should **not be relied on for production pool operation**.
>
> It is not affiliated with AstralPool, Astral, Fluidra, or Astral Labs.
> Use it at your own risk.
>
> In particular, BLE onboarding, broader equipment control, timer workflows, and some write paths still need more real-world validation.

## What this project is

This repository contains two closely related pieces:

- a **Home Assistant custom integration** for day-to-day use
- a **Python client library** (`pychlorinator_cloud`) that speaks the Halo cloud protocol

The goal is simple: expose Halo cloud data in Home Assistant without relying on BLE for ongoing operation.

## Why this exists

Existing Halo integrations are mostly BLE-centric. That can work well, but it is not always a good fit if:

- your chlorinator is a poor match for BLE range or signal reliability
- you do not want to depend on an ESPHome/BLE proxy
- you want the integration to use the same cloud path the vendor app uses

If BLE already works well in your setup and you want a more established option today, also look at:

- [`astralpool_halo_chlorinator`](https://github.com/DanielNagy/astralpool_halo_chlorinator)
- [`astralpool_chlorinator`](https://github.com/pbutterworth/astralpool_chlorinator)

## Current status

The integration already provides useful read-only coverage and a limited set of cloud controls, but it should still be treated as **beta / preview** rather than a finished stable release.

### Solid today

- cloud connection and live updates
- core telemetry and operating state
- Home Assistant config flow
- Home Assistant entities for status, diagnostics, and a conservative control surface
- main mode control: **Off**, **Auto**, **On**
- pH and ORP setpoint number entities in Home Assistant
- manual pump speed control, with working writes and conservative readback handling
- app-confirmed control paths for heater, light, Blade, Jets, acid-dosing holds, and controller time sync are now mapped in the local working copy

### Still not fully locked down

- release-quality BLE onboarding across more real-world setups
- broader write support for heater, lighting, valves, and other equipment functions
- wider real-world validation of setpoint and equipment writes
- timer and equipment-control workflows

## Installation

### HACS

1. Open **HACS**.
2. Add a **Custom repository**.
3. Repository URL: `https://github.com/robmarkoski/pychlorinator-cloud`
4. Category: **Integration**
5. Install the repository.
6. Restart Home Assistant.
7. Go to **Settings -> Devices & Services**.
8. Add **AstralPool Halo Cloud**.

### Requirements

For normal onboarding, Home Assistant needs a working Bluetooth path to discover and pair with the chlorinator.

After setup, the integration uses the **cloud connection for normal operation**. BLE is primarily needed to obtain cloud credentials during pairing.

## Setup and onboarding

### Normal setup path

The intended user flow is:

1. Home Assistant discovers the Halo chlorinator over BLE
2. you confirm the device and start pairing
3. Home Assistant retrieves the generated cloud credentials
4. you choose the Home Assistant device name and optional area
5. the integration connects over the cloud and creates entities

### Manual credential path (firmware 2.7+ workaround)

On Halo Chlor firmware 2.7+, BLE pairing for third-party clients is blocked by a server-side gate that requires Google Play Integrity / Apple App Attest from the genuine vendor app. See [#1](https://github.com/robmarkoski/pychlorinator-cloud/issues/1) for the reverse-engineering detail. The local crypto path that earlier firmware accepted no longer produces a MAC the chlorinator will honour.

This fork (branch `fw27-cloud-only-control`) exposes a manual-credential setup path so owners with affected firmware can still use the cloud integration, by reading the cloud credentials directly from the official app.

#### What you need

- A Halo chlorinator already paired via the official Halo Chlor Go Android app at least once.
- The Android phone with that paired install.
- A computer with `adb` (Android Platform Tools) installed, USB debugging enabled on the phone.

#### Extracting the credentials

1. Connect the phone to the computer via USB. Authorise the debugging connection on the phone if prompted.
2. Confirm the device is visible:

   ```bash
   adb devices
   ```

3. Start a filtered logcat tail on the official app's process:

   ```bash
   adb logcat --pid=$(adb shell pidof au.com.fabtronics.halochlorgo)
   ```

4. Force-stop and re-launch the Halo Chlor Go app on the phone. The app writes the cloud credentials to stdout on launch.
5. In the logcat output, find the line containing the chlorinator serial number (`sn`), a cloud `username`, and a 64-character `password`. Copy the three values.

> [!WARNING]
> The credentials are bearer secrets. Anyone holding them can control the chlorinator via the cloud. Do not share them, do not commit them to a repository, and do not paste them into chat, issues, or screenshots.

#### Adding the integration

1. In Home Assistant: **Settings → Devices & Services → Add Integration → AstralPool Halo Cloud**.
2. Choose **Manual credential entry (advanced)** as the setup method.
3. Paste the serial, username, and password.
4. Confirm. The integration connects via the cloud and creates entities exactly as the BLE flow would.

#### Operational wrinkle

The Astralpool cloud appears to permit only one concurrent connection per chlorinator across all accounts. If the official Halo Chlor Go app is logged in on a phone and the Home Assistant integration connects, one of the two sessions gets disconnected.

Workaround: disable the Home Assistant config entry (do not delete it) when you need the phone, re-enable when done.

## Capability matrix

This table is intentionally conservative.

| Area | Status | Notes |
|---|---|---|
| Cloud connection and live updates | Working | Main operating path |
| Core status and measurement sensors | Working | pH, ORP, temperatures, mode, pump state, messages, diagnostics |
| Binary sensors for operating and fault state | Working | Includes connectivity, no-flow, salt, sanitising, standby, etc. |
| System mode select | Working | `Off`, `Auto`, `On` |
| Pump speed select | Working, still conservative | Writes are working, but readback remains inference-based in some transitions |
| pH / ORP setpoint controls | Working, still conservative | Exposed as Home Assistant number entities with bounded validation |
| BLE pairing-based onboarding | Functional but still being hardened | Not yet something I would overclaim as fully proven across all setups |
| Pool/spa selection control | Not currently exposed | Read state exists, control is not presented as a Home Assistant entity today |
| Heater control | Working locally, still under live validation | On/off and stepwise setpoint changes are app-confirmed |
| Light, Blade, Jets, auxiliary equipment controls | Working locally, still conservative | App-confirmed write paths mapped, optional entities should stay user-enabled rather than assumed present |
| Acid dosing hold controls | Working locally, still conservative | Resume now, indefinite hold, and app preset timed holds are mapped |
| Controller time sync | Working locally, still conservative | Uses app-confirmed date/time writes |
| Timer and schedule control | Not release-ready | Not part of the normal HA surface |

## Entity overview

Entity IDs use the configured Home Assistant device name.

- default device name: `Halo <serial>`
- device slug: `slugify(device_name)`
- entity ID pattern: `<platform>.<device_slug>_<entity_key>`

Example, if the configured device name is **Pool Chlorinator**:

- `sensor.pool_chlorinator_mode`
- `binary_sensor.pool_chlorinator_low_salt`
- `select.pool_chlorinator_mode_select`

### Select entities

| Entity | Default entity ID | Purpose |
|---|---|---|
| System Mode | `select.<device_slug>_mode_select` | Main operating mode |
| Pump Speed Control | `select.<device_slug>_pump_speed_select` | Manual pump speed selection |
| Heater Mode Control | `select.<device_slug>_heater_mode_select` | Heater on/off |
| Acid Dosing Hold | `select.<device_slug>_acid_dosing_select` | Resume now, indefinite hold, and preset timed holds, default-disabled until you choose to expose it |

Optional-by-default selects, enabled manually if your hardware actually has them:

| Entity | Default entity ID | Purpose |
|---|---|---|
| Light Mode | `select.<device_slug>_light_mode_select` | Light off/on/auto |
| Blade Mode | `select.<device_slug>_blade_mode_select` | Auxiliary equipment target id 6 |
| Jets Mode | `select.<device_slug>_jets_mode_select` | Auxiliary equipment target id 7 |

### Number entities

| Entity | Default entity ID | Purpose |
|---|---|---|
| pH Setpoint | `number.<device_slug>_ph_setpoint_control` | Adjust pH target |
| ORP Setpoint | `number.<device_slug>_orp_setpoint_control` | Adjust ORP target |

### Button entities

| Entity | Default entity ID | Purpose |
|---|---|---|
| Sync Controller Time | `button.<device_slug>_sync_controller_time` | Sync date/time from Home Assistant host |
| Heater Setpoint Up | `button.<device_slug>_heater_setpoint_up` | Raise heater setpoint by 1°C |
| Heater Setpoint Down | `button.<device_slug>_heater_setpoint_down` | Lower heater setpoint by 1°C |

### Core sensors

| Entity | Default entity ID |
|---|---|
| System Mode | `sensor.<device_slug>_mode` |
| Pump Speed | `sensor.<device_slug>_pump_speed` |
| pH | `sensor.<device_slug>_ph_measurement` |
| ORP Measurement | `sensor.<device_slug>_orp_measurement` |
| Chlorine Status | `sensor.<device_slug>_chlorine_status` |
| pH Status | `sensor.<device_slug>_ph_status` |
| Info Message | `sensor.<device_slug>_info_message` |
| Error Message | `sensor.<device_slug>_error_message` |
| Water Temperature | `sensor.<device_slug>_water_temperature` |
| Heater Mode | `sensor.<device_slug>_heater_mode` |
| Heater Setpoint | `sensor.<device_slug>_heater_setpoint` |
| Heater Water Temperature | `sensor.<device_slug>_heater_water_temperature` |

### Binary sensors

| Entity | Default entity ID |
|---|---|
| Cloud Connected | `binary_sensor.<device_slug>_connected` |
| Pump Operating | `binary_sensor.<device_slug>_pump_operating` |
| Cell Operating | `binary_sensor.<device_slug>_cell_operating` |
| Heater On | `binary_sensor.<device_slug>_heater_on` |
| No Flow | `binary_sensor.<device_slug>_no_flow` |
| Low Salt | `binary_sensor.<device_slug>_low_salt` |
| High Salt | `binary_sensor.<device_slug>_high_salt` |
| Sanitising Active | `binary_sensor.<device_slug>_sanitising_active` |
| Filtering Only | `binary_sensor.<device_slug>_filtering_only` |
| Sampling Active | `binary_sensor.<device_slug>_sampling_active` |
| Standby | `binary_sensor.<device_slug>_standby` |
| Manual Acid Dose Active | `binary_sensor.<device_slug>_manual_acid_dose_active` |
| Backwashing | `binary_sensor.<device_slug>_backwashing` |

### Additional diagnostics

The integration also exposes a larger set of diagnostic entities, including:

- precise water temperature
- cell current and cell level
- pH / ORP control type
- pH / ORP / chlorine / acid setpoint readbacks
- access level and protocol version
- board temperature and pool volume
- raw salt/error code and heater error

Some of these are **disabled by default** in Home Assistant because they are primarily diagnostic.

## Notes and limitations

- Halo cloud access can contend with BLE or app access. In practice, the device behaves much better when only one control path is active at a time.
- This is still a reverse-engineered integration, so vendor-side changes could break parts of it.
- BLE pairing is a key release-quality gate. Until that path is proven more broadly, this should be treated as preview/beta.
- pH and ORP setpoints are exposed in Home Assistant, and newer app-confirmed controls are being surfaced conservatively. Optional accessories like lights or extra equipment should not be assumed present on every install.
- Timer and equipment-control work is not yet part of the stable feature set and is mostly kept out of the default HA surface.
- Confirmed cloud protocol version currently surfaced by the integration is `2.0`.

## Tested device

| Item | Value |
|---|---|
| Tested model | AstralPool Halo Chlorinator |
| Confirmed cloud protocol version | `2.0` |
| Example Home Assistant device name used in testing | `Pool Chlorinator` |

## Troubleshooting

### The chlorinator is not discovered during setup

- Make sure Bluetooth is available to Home Assistant.
- Make sure the chlorinator is advertising and can be put into pairing mode.
- Try the initial pairing flow with the chlorinator physically closer to the Bluetooth adapter if possible.

### The integration sets up, but cloud connectivity is unreliable

- Close the vendor app and stop any BLE polling or BLE-based integrations temporarily.
- The chlorinator appears to dislike multiple active control paths at once.
- If BLE is still connected, the cloud session may fail or behave inconsistently.

### Some entities are missing

- Check whether the entity is **disabled by default** in the entity registry.
- Some sensors are intentionally diagnostic-only.
- Some controls are not exposed yet because they are not sufficiently validated.

### A control you expected is not available

That is usually intentional at this stage. This repository prefers conservative exposure over offering controls that are not yet well validated on real hardware.

## Support

If you run into a bug or install problem, please open an issue on GitHub with:

- your Home Assistant version
- integration version / branch
- what setup path you used
- relevant log snippets
- whether BLE or the vendor app was also connected at the time
