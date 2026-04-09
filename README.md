# AstralPool Halo Cloud

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Home Assistant Custom Integration](https://img.shields.io/badge/Home%20Assistant-Custom%20Integration-41BDF5.svg)](https://www.home-assistant.io/)

A Home Assistant custom integration for AstralPool Halo chlorinators that uses the cloud connection rather than BLE for normal operation.

> [!WARNING]
> This project is unofficial, reverse-engineered software.
> It is under **heavy development**, carries **no warranty**, and things may break until the release is properly locked in.
> It is **not for production use yet**.
> It is not affiliated with AstralPool, Astral, Fluidra, or Astral Labs.
> Use at your own risk.

> [!IMPORTANT]
> Most of the core integration works, but some controls are still incomplete or not fully locked down yet.
> Check the control-status section below before assuming a feature is release-ready.

## Why this exists

I built this because the BLE-only options were not a great fit for my setup.
I wanted a cloud-first integration without the normal BLE range and proxy limitations, and my BLE proxy location was far enough away that I could not rely on it staying connected consistently.
That is why this project exists.

If you want something more established today and BLE already works well in your environment, look at:

- [`astralpool_halo_chlorinator`](https://github.com/DanielNagy/astralpool_halo_chlorinator)
- [`astralpool_chlorinator`](https://github.com/pbutterworth/astralpool_chlorinator)

If one of those already does what you need and just works, use it and forget you ever saw this one.

## What it does

This integration exposes Halo cloud data in Home Assistant, including:

- operating mode and pump speed
- pH and ORP readings
- chlorine, pH, timer, and error/status messages
- water temperature and heater state
- setpoints and device diagnostics
- cloud connectivity and operating-state binary sensors

## Installation

### HACS

1. Open HACS.
2. Add a **Custom repository**.
3. Use repository URL: `https://github.com/robmarkoski/pychlorinator-cloud`
4. Category: **Integration**
5. Install the repository.
6. Restart Home Assistant.
7. Go to **Settings -> Devices & Services**.
8. Add **AstralPool Halo Cloud**.

## Configuration

The integration currently supports two setup paths:

| Setup path | Use case | Notes |
|---|---|---|
| Manual cloud credentials | Fastest and most predictable setup path | Best for current install/testing |
| BLE pairing / discovery | Preferred long-term onboarding path | Important before a full 1.0 release claim |

During setup you can also choose a device name and optional Home Assistant area.

## Entity naming convention

Entity IDs depend on the configured device name chosen during setup.

| Item | Rule |
|---|---|
| Device name default | `Halo <serial>` |
| Device slug | `slugify(device_name)` |
| Object ID pattern | `<device_slug>_<entity_key>` |
| Entity ID pattern | `<platform>.<device_slug>_<entity_key>` |
| Area | Used for placement only, not entity IDs |

Example, if the configured device name is **Pool Chlorinator**, the device slug becomes `pool_chlorinator` and entity IDs will look like:

- `sensor.pool_chlorinator_mode`
- `binary_sensor.pool_chlorinator_low_salt`
- `select.pool_chlorinator_mode_select`

## Current control status

The checklist below is intentionally conservative. It reflects what is currently known to work, what is still incomplete, and what is planned for the timer-focused 2.0 milestone.

### Manual controls working now

- [x] Connect to the chlorinator over the cloud
- [x] Read live state, measurements, setpoints, and status messages
- [x] Set main operating mode to **Off**
- [x] Set main operating mode to **Auto**
- [x] Force sanitising/manual run **On**
- [x] Select pump speed **High**
- [x] Expose manual control/select entities in Home Assistant for supported mode changes

### Manual controls partially implemented or not fully locked down yet

- [ ] Confirm manual pump speed **Low** end-to-end on hardware
- [ ] Confirm manual pump speed **Medium** end-to-end on hardware
- [ ] Re-validate Pool/Spa selection end-to-end on hardware
- [ ] Expose and validate safe pH setpoint writes in the main Home Assistant UX
- [ ] Expose and validate safe ORP setpoint writes in the main Home Assistant UX

### Manual controls not working yet / not release-ready yet

- [ ] Heater **On/Off** control locked down
- [ ] Light **On/Off** control locked down
- [ ] Valve / auxiliary manual controls (for example Blade / Jets) locked down
- [ ] Solar / equipment manual controls locked down
- [ ] Release-quality BLE pairing/onboarding locked down

### Planned for 2.0 release

The planned **2.0** milestone is the timer/equipment-control release.

- [ ] Equipment timer system working end-to-end
- [ ] Summer / Winter timer profile handling working end-to-end
- [ ] Lighting timer system working end-to-end
- [ ] Heat Demand timer/system working end-to-end
- [ ] Safe timer writes exposed in Home Assistant
- [ ] Timer-related equipment control polished and release-ready

## Entities

The tables below show the main entities exposed by the integration. Default entity IDs use `<device_slug>` to indicate the configured system/device name slug.

### Selects

| Name | Default entity ID | Unit | Description |
|---|---|---:|---|
| Mode | `select.<device_slug>_mode_select` | — | Main operating mode selector |
| Pool/Spa | `select.<device_slug>_pool_spa_select` | — | Pool or spa selection |

### Core sensors

| Name | Default entity ID | Unit | Description |
|---|---|---:|---|
| Mode | `sensor.<device_slug>_mode` | — | Current chlorinator operating mode |
| Pump Speed | `sensor.<device_slug>_pump_speed` | — | Current pump speed |
| pH | `sensor.<device_slug>_ph_measurement` | pH | Live pH reading |
| ORP Measurement | `sensor.<device_slug>_orp_measurement` | mV | Live ORP reading |
| Chlorine Status | `sensor.<device_slug>_chlorine_status` | — | Human-readable chlorine status |
| pH Status | `sensor.<device_slug>_ph_status` | — | Human-readable pH status |
| Info Message | `sensor.<device_slug>_info_message` | — | Main operating/status text |
| Error Message | `sensor.<device_slug>_error_message` | — | Main error/status code as text |
| Timer Info | `sensor.<device_slug>_timer_info` | — | Timer-related status text |
| Water Temperature | `sensor.<device_slug>_water_temperature` | °C | Primary water temperature |
| pH Setpoint | `sensor.<device_slug>_ph_setpoint` | pH | Target pH setpoint |
| ORP Setpoint | `sensor.<device_slug>_orp_setpoint` | mV | Target ORP setpoint |
| Pool Chlorine Setpoint | `sensor.<device_slug>_pool_chlorine_setpoint` | — | Pool chlorine target |
| Acid Setpoint | `sensor.<device_slug>_acid_setpoint` | — | Acid dosing target |
| Spa Chlorine Setpoint | `sensor.<device_slug>_spa_chlorine_setpoint` | — | Spa chlorine target |
| Heater Mode | `sensor.<device_slug>_heater_mode` | — | Heater on/off mode |
| Heater Pump Mode | `sensor.<device_slug>_heater_pump_mode` | — | Heater pump mode |
| Heater Setpoint | `sensor.<device_slug>_heater_setpoint` | °C | Heater target temperature |
| Heat Pump Mode | `sensor.<device_slug>_heat_pump_mode` | — | Heat pump operating mode |
| Heater Water Temperature | `sensor.<device_slug>_heater_water_temperature` | °C | Heater-reported water temperature |

### Diagnostic sensors

| Name | Default entity ID | Unit | Description | Notes |
|---|---|---:|---|---|
| Water Temperature Precise | `sensor.<device_slug>_water_temperature_precise` | °C | Higher-precision water temperature | Diagnostic |
| Cell Level | `sensor.<device_slug>_cell_level` | — | Cell output level | Diagnostic |
| Cell Current | `sensor.<device_slug>_cell_current` | mA | Cell current draw | Diagnostic |
| pH Control Type | `sensor.<device_slug>_ph_control_type` | — | pH control mode | Diagnostic |
| ORP Control Type | `sensor.<device_slug>_orp_control_type` | — | ORP control mode | Diagnostic |
| Access Level | `sensor.<device_slug>_access_level` | — | Cloud access level | Diagnostic |
| Protocol Version | `sensor.<device_slug>_protocol_version` | — | Reported cloud protocol version | Diagnostic |
| Last Update | `sensor.<device_slug>_last_update` | — | Timestamp of last update | Disabled by default |
| Board Temperature | `sensor.<device_slug>_board_temperature` | °C | Controller board temperature | Diagnostic |
| Pool Volume | `sensor.<device_slug>_pool_volume` | L | Configured pool volume | Diagnostic |
| Litres Left to Filter | `sensor.<device_slug>_litres_left_to_filter` | L | Remaining filtration volume estimate | Diagnostic |
| Heater Error | `sensor.<device_slug>_heater_error` | — | Raw heater error value | Diagnostic |
| Salt/Error Code | `sensor.<device_slug>_salt_error_raw` | — | Raw salt/error code field | Disabled by default |

### Binary sensors

| Name | Default entity ID | Type | Description | Notes |
|---|---|---|---|---|
| Cloud Connected | `binary_sensor.<device_slug>_connected` | Diagnostic | Cloud connection state | Connectivity |
| Pump Operating | `binary_sensor.<device_slug>_pump_operating` | Status | Pump currently operating | Running state |
| Cell Operating | `binary_sensor.<device_slug>_cell_operating` | Status | Cell currently operating | Running state |
| Cell Reversed | `binary_sensor.<device_slug>_cell_reversed` | Diagnostic | Cell polarity reversed | Diagnostic |
| Cell Reversing | `binary_sensor.<device_slug>_cell_reversing` | Diagnostic | Cell currently reversing | Diagnostic |
| Cooling Fan | `binary_sensor.<device_slug>_cooling_fan_on` | Diagnostic | Cooling fan active | Diagnostic |
| Dosing Pump | `binary_sensor.<device_slug>_dosing_pump_on` | Status | Dosing pump active | Running state |
| AI Mode Active | `binary_sensor.<device_slug>_ai_mode_active` | Diagnostic | AI mode flag active | Diagnostic |
| Spa Selected | `binary_sensor.<device_slug>_spa_selection` | Diagnostic | Spa mode selected | Diagnostic |
| Heater On | `binary_sensor.<device_slug>_heater_on` | Status | Heater currently on | Heat state |
| No Flow | `binary_sensor.<device_slug>_no_flow` | Diagnostic | No-flow condition detected | Diagnostic |
| Low Salt | `binary_sensor.<device_slug>_low_salt` | Diagnostic | Low-salt condition detected | Diagnostic |
| High Salt | `binary_sensor.<device_slug>_high_salt` | Diagnostic | High-salt condition detected | Diagnostic |
| Sampling Only | `binary_sensor.<device_slug>_sampling_only` | Diagnostic | Sampling-only condition active | Diagnostic |
| Dosing Disabled | `binary_sensor.<device_slug>_dosing_disabled` | Diagnostic | Dosing disabled state | Diagnostic |
| Daily Acid Dose Limit Reached | `binary_sensor.<device_slug>_daily_acid_dose_limit_reached` | Diagnostic | Daily acid-dose limit reached | Diagnostic |
| Cell Disabled | `binary_sensor.<device_slug>_cell_disabled` | Diagnostic | Cell disabled state | Diagnostic |
| Sanitising Active | `binary_sensor.<device_slug>_sanitising_active` | Status | Sanitising mode active | Operating state |
| Filtering Only | `binary_sensor.<device_slug>_filtering_only` | Status | Filtering without sanitising | Operating state |
| Sampling Active | `binary_sensor.<device_slug>_sampling_active` | Status | Sampling mode active | Operating state |
| Standby | `binary_sensor.<device_slug>_standby` | Status | Standby state active | Operating state |
| Low Speed No Chlorinating | `binary_sensor.<device_slug>_low_speed_no_chlorinating` | Status | Low-speed no-chlorination state | Operating state |
| Reduced Output Low Temperature | `binary_sensor.<device_slug>_reduced_output_low_temperature` | Status | Reduced output due to low temperature | Operating state |
| Heater Cooldown Active | `binary_sensor.<device_slug>_heater_cooldown_active` | Status | Heater cooldown in progress | Operating state |
| Manual Acid Dose Active | `binary_sensor.<device_slug>_manual_acid_dose_active` | Status | Manual acid dosing active | Operating state |
| Backwashing | `binary_sensor.<device_slug>_backwashing` | Status | Backwash state active | Operating state |

## Tested device

| Item | Value |
|---|---|
| Tested model | AstralPool Halo Chlorinator |
| Home Assistant device name used in testing | Pool Chlorinator |
| Firmware currently running | Not currently surfaced/recorded cleanly by the integration yet |
| Confirmed cloud protocol/build info | `2.0` |

## Notes and limitations

- The chlorinator only supports one active connection at a time, so cloud access can contend with BLE or app access.
- This project is still under heavy development.
- BLE pairing needs to be fully proven in real-world onboarding before a full 1.0 release claim.
- Timer and equipment work is underway, but not yet part of the stable public feature set.
- Exact chlorinator firmware version is not currently recorded in a clean user-facing way, so it is not claimed here.
- Confirmed cloud protocol/build information currently indicates protocol `2.0`.

## Work in progress

Current development focus includes:

- hardening BLE pairing for release-quality onboarding
- timer decoding and safe timer support
- continued Home Assistant polish and packaging improvements

## Support

If you hit a problem with the public HACS/install path, please use the GitHub issue tracker.
