# AstralPool Halo Cloud

<!-- markdownlint-disable MD013 MD028 MD033 -->

> [!CAUTION]
> **Stop before installing this. This is work in progress, pre-release software.**
>
> This integration may break at any time. There is no stability promise, no warranty, and no vendor support path.
>
> It has been tested on **one firmware version only: `pbver 2.3`**. That value is reported by the controller in `buildinfo.pbver` during cloud connection. Other firmware versions, controller variants, regions, and accessory combinations are untested.
>
> **Strong recommendation: do NOT use this. Use a BLE-based integration instead.** The cloud path is contended, brittle, depends on internet access, and depends on Astral's servers. The vendor backend appears to allow very limited simultaneous sessions.
>
> **DO NOT run this integration and a BLE integration against the same chlorinator at the same time. Pick one.** Running both can cause cloud disconnects, BLE pairing failures, and unpredictable controller state.
>
> This project is not affiliated with AstralPool, Astral, Fluidra, or Astral Labs. Astral can change the backend or device protocol at any time and this may stop working.

<p align="center">
  <img src="https://raw.githubusercontent.com/robmarkoski/pychlorinator-cloud/main/brand/logo.png" alt="AstralPool Halo Cloud logo" width="540">
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Home Assistant Custom Integration](https://img.shields.io/badge/Home%20Assistant-Custom%20Integration-41BDF5.svg)](https://www.home-assistant.io/)

> [!IMPORTANT]
> After downloading, installing, or updating this integration through HACS, restart Home Assistant before adding or reloading the integration.

## What It Does

AstralPool Halo Cloud is an unofficial Home Assistant custom integration for AstralPool Halo chlorinators. It is cloud-first: BLE is used only during onboarding to retrieve the device-generated cloud credentials, then normal operation uses the vendor cloud WebSocket at `iot.connectmypool.com.au`.

The integration exposes around 165 Home Assistant entities (88 sensors, 49 binary sensors, 11 selects, 6 numbers, 9 buttons), five `astralpool_halo_cloud.*` service calls, and bundled Lovelace cards for timer/schedule editing.

> [!NOTE]
> In this pre-release build **all entities are enabled by default** to make the full surface easy to review while testing. A curated long-term default-disabled set (mostly per-flag diagnostics and raw read-backs) is documented in `docs/DEFAULT_DISABLED_ENTITIES.md` and will be restored before a stable release. Disable any you do not want from the device page in the meantime.

## Current Status

| Area | Status | Notes |
| --- | --- | --- |
| Cloud connection and live updates | Working on one controller | Uses the vendor WebSocket signalling path. The client mimics the vendor's post-connect "vomit" handshake (`0x006B` + `0x0005`) and uses an escalating post-disconnect cooldown; a multi-day soak on one controller held mostly 15–30 minute sessions with automatic recovery from the relay's periodic kicks. Cloud contention is still the main reliability risk. |
| Acid reservoir tracking | Implemented | HA-side estimate of remaining acid, driven by the controller's daily dosing (there is no tank-level sensor in the protocol). Set your bottle size, press **Log Acid Refill** when you replace it, and the integration tracks remaining volume, % left, used-today, average daily use, and days remaining. The seconds-to-millilitres conversion is provisional until validated against a real dose. |
| BLE onboarding | Implemented, needs broader testing | Used to register a username and retrieve the generated cloud password. |
| Read-only telemetry | Best covered area | Core state, chemistry, pump, heater, timer readback, lights, and diagnostics are parsed. |
| System mode write | Implemented, experimental | `Auto -> On -> Auto` has been live tested. Treat `Off` carefully. Known revert/reliability issues are still being investigated. |
| Pump speed write | Implemented, experimental | Manual `Low` / `Medium` / `High` writes exist. They force manual/on behavior and can still revert unexpectedly. |
| pH and ORP setpoint writes | Implemented, experimental | Bounds are checked, but broader live validation is still needed. |
| Heater writes | Implemented, experimental | Heater mode and target temperature are exposed if the controller reports heater data. |
| Acid dosing hold | Implemented, high risk | Resume / hold-for-period control plus the acid reservoir estimate (see below). Use normal water testing as ground truth. |
| Light, Blade, and Jets writes | Implemented, hardware dependent | Off/Auto/On for Blade and Jets, plus light mode. Disabled by default; only enable if your controller has the accessory. Blade/Jets live state is now read back from the controller (`0x00C9`), so the selects reflect real state, not just last-write. |
| Light colour / effect | Implemented, hardware dependent | `set_light_colour` / `synchronise_light_colour` services drive per-zone colour/effect, mapped per light model. Only applies to colour-capable light models. |
| Custom equipment names | Implemented | Built-in valve/GPO labels resolve to user-configured custom names where the controller emits them. Falls back to enum labels gracefully. |
| Maintenance programs | Implemented, experimental | Abort and Sanitise Until Tomorrow are buttons. Filter For Period and Sanitise For Period are fixed-duration selects using the same time increments as Acid Dosing Hold. |
| Last-known value persistence | Implemented | Most live values restore from the previous HA session on restart. Live-only values (heater setpoint, chemistry watermarks, timer pump speed) stay Unknown until first refresh by design. |
| Error code labels | Decoded from vendor app | 77 codes across Hardware / Sensor / Pump / Heater / Chemistry / Lighting / Solar / Cell / Flow / Acid / Firmware categories. Each carries severity (Information / Warning / Fault), category, reason, and recommended action. |
| Timer/schedule reads | Implemented, diagnostic | Schedule metadata and slot readback exist. |
| Timer/schedule writes | Implemented, experimental | Equipment timers (8 slots/season), lighting timers (2 slots/season), and the heat-demand window are all writable via services and the bundled card. |
| Bundled Lovelace cards | Implemented, experimental | `custom:halo-timer-card` edits equipment timers, lighting timers, and the heat-demand window (mode tabs). `custom:halo-schedule-view` visualises active timer slots. |
| Local/LAN mode | Not exposed in Home Assistant | Low-level library code exists, but this HA integration is cloud-first. |

## Entity Catalogue

Entity IDs use the Home Assistant device name chosen during setup. The default name is `halo_<serial>` where `<serial>` is your controller's reported serial number. Example object IDs look like (substitute your serial for `<SERIAL>`):

```text
sensor.halo_<SERIAL>_ph_measurement
select.halo_<SERIAL>_mode_select
number.halo_<SERIAL>_ph_setpoint_control
binary_sensor.halo_<SERIAL>_connected
button.halo_<SERIAL>_pause_cloud_connection
```

If you rename the device, the `halo_<SERIAL>` prefix will be different.

### System Control Writes

These are the entities users actively interact with. All cloud-backed writes require the cloud session to be connected unless noted.

| Entity keys | Type | Stability |
| --- | --- | --- |
| `mode_select` | select | Experimental. `Auto -> On -> Auto` has been tested on one controller. Pump/system mode revert behavior is still under investigation. |
| `pump_speed_select` | select | Experimental. Manual `Low`, `Medium`, and `High` writes exist, but reliability is still under investigation. |
| `heater_mode_select`, `heater_setpoint_control` | select, number | Experimental. Requires heater data from the controller. |
| `ph_setpoint_control`, `orp_setpoint_control` | number | Experimental. Uses guarded setpoint writes and controller-reported bounds when available. |
| `pool_chlorine_setpoint_control` | number | Experimental. Adjusts the pool's manual chlorine level in whole steps, using controller-reported bounds or a fallback of 0–8. This is a controller level, not a measured chlorine concentration. |
| `light_mode_select`, `blade_mode_select`, `jets_mode_select` | select | Experimental, accessory-dependent, disabled by default. |
| `acid_dosing_select` | select | Experimental, disabled by default. Dosing controls are not a substitute for water testing. |
| `connection_pause_select`, `connection_pause_minutes` | select, number | Local HA controls for temporarily releasing the cloud connection. Useful when opening the vendor app. |
| `filter_for_period`, `sanitise_for_period` | select | Fixed-duration maintenance program starts. Options match the Acid Dosing Hold time increments. |
| `pause_cloud_connection`, `resume_cloud_connection` | button | Local HA controls for the same cloud-session release workflow. |
| `sync_controller_time` | button | Experimental. Writes controller date/time from Home Assistant host time. |

### Live State Sensors

| Purpose | Entity keys |
| --- | --- |
| Core state | `mode`, `current_operating_speed`, `pump_speed`, `last_update`, `connected` |
| Messages | `info_message`, `error_message`, `timer_info` |
| Chemistry | `ph_measurement`, `orp_measurement`, `chlorine_status`, `ph_status`, `ph_setpoint`, `orp_setpoint`, `acid_setpoint`, `pool_chlorine_setpoint`, `spa_chlorine_setpoint` |
| Temperature | `water_temperature`, `water_temperature_precise`, `heater_water_temperature` |
| Heater | `heater_mode`, `heater_setpoint` |
| Timers | `controller_datetime`, `timer_profile_index`, `timer_next_profile_index`, `active_timer_slot`, `timer_pump_speed`, `timer_season`, `priming_countdown`, `filter_sanitise_remaining` |
| Cloud/session helpers | `connection_pause_minutes`, `acid_dosing_hold_remaining` |

### Light Zones

| Purpose | Entity keys |
| --- | --- |
| Per-zone on/off state | `light_zone1_on`, `light_zone2_on`, `light_zone3_on`, `light_zone4_on` |
| Per-zone manual mode | `zone1_manual_mode`, `zone2_manual_mode`, `zone3_manual_mode`, `zone4_manual_mode` |
| Per-zone active source | `zone1_active_source`, `zone2_active_source`, `zone3_active_source`, `zone4_active_source` |
| Timer readback | `lighting_timer_slots` |

### Binary State Flags

| Purpose | Entity keys |
| --- | --- |
| Connection and pump | `connected`, `pump_operating`, `pump_priming` |
| Cell and chemistry validity | `cell_operating`, `cell_disabled`, `cell_reversed`, `cell_reversing`, `chemistry_values_current`, `chemistry_values_valid` |
| Heater | `heater_on`, `heater_cooldown_active` |
| Flow and salt | `no_flow`, `low_salt`, `high_salt` |
| Operating states | `sanitising_active`, `filtering_only`, `sampling_active`, `sampling_only`, `standby`, `backwashing`, `spa_selection` |
| Dosing | `manual_acid_dose_active`, `dosing_pump_on`, `dosing_disabled`, `daily_acid_dose_limit_reached` |
| Limits and warnings | `low_speed_no_chlorinating`, `reduced_output_low_temperature`, `cooling_fan_on`, `ai_mode_active`, `sanitising_until_next_timer_tomorrow` |

### Diagnostics

These are intended for advanced troubleshooting (see the pre-release note above about default-enabled state).

| Purpose | Entity keys |
| --- | --- |
| Protocol and access | `access_level`, `protocol_version`, `firmware_version` |
| Cell and board | `cell_level`, `cell_current`, `cell_reversal_count`, `cell_running_hours`, `low_salt_cell_running_hours`, `board_temperature`, `filter_pump_minutes_today`, `power_board_runtime_hours` |
| Chemistry watermarks | `highest_ph_measured`, `lowest_ph_measured`, `highest_orp_measured`, `lowest_orp_measured` |
| Control model | `ph_control_type`, `chlorine_control_type`, `pool_volume`, `litres_left_to_filter`, `salt_error_raw` |
| Heater diagnostics | `heat_pump_mode`, `heater_pump_mode`, `heater_error`, `heater_message` (with `detail` attribute) |
| Gas-heater status flags | `heater_flame`, `heater_pressure`, `heater_gas_valve`, `heater_lockout`, `heater_service_required` (binary sensors; most relevant to gas heaters) |
| Heat-demand schedule | `heat_demand_schedule` (sensor, window summary + attributes), `heat_demand_enabled`, `heat_demand_window_enabled`, `heat_demand_activated` (binary sensors) |
| Solar (accessory-dependent, disabled by default) | `solar_roof_temperature`, `solar_water_temperature`, `solar_mode`, `solar_message` (sensors); `solar_pump`, `solar_flush_active` (binary sensors) |
| Time drift | `time_drift`, `time_drift_threshold` |
| Timers | `equipment_timer_summary`, `equipment_timer_slots`, `equipment_timer_active_slots`, `active_timer_slot` |
| Valves | `valve_0_active`, `valve_1_active` |
| Equipment names | `gpo1_name` to `gpo4_name`, `valve1_name` to `valve4_name` (each with `is_custom_name` attribute) |
| Controller notices | `controller_notice_active`, `controller_fault_active` (`error_message` carries `severity`, `category`, `reason`, `recommended_action`, `raw_code` attributes) |

### Maintenance

| Purpose | Entity keys |
| --- | --- |
| One-shot actions | `button.halo_<SERIAL>_abort_maintenance_task`, `button.halo_<SERIAL>_sanitise_until_tomorrow` |
| Fixed-duration maintenance programs | `select.halo_<SERIAL>_filter_for_period`, `select.halo_<SERIAL>_sanitise_for_period` |
| Force optional refresh | `button.halo_<SERIAL>_refresh_optional_values` (30-second rate limit) |

### Acid Reservoir

The controller reports acid pump run-time per day (`0x0259`) but has no tank-level sensor, so remaining acid is estimated HA-side. Set your bottle size and log a refill; the integration decrements the estimate as the controller doses and resets across the controller's daily rollover.

| Purpose | Entity keys |
| --- | --- |
| Configure + refill | `number.halo_<SERIAL>_acid_bottle_size` (litres), `button.halo_<SERIAL>_log_acid_refill` (reset estimate to full + stamp the time) |
| Reservoir estimate | `acid_remaining` (L), `acid_remaining_percent`, `acid_days_remaining`, `acid_last_refill` |
| Usage | `acid_used_today` (mL), `acid_avg_daily` (mL/day) |

> [!NOTE]
> The raw field is dosing-pump seconds; the millilitre figures are derived using the acid pump dose rate (mL/min) and remain provisional until confirmed against a real acid dose. When acid usage is very low, `acid_days_remaining` can read as an unrealistically large number.

## Services

All services are under the `astralpool_halo_cloud` domain and target one Halo device via `device_id` (if you only have one Halo, it is resolved automatically). They require an active cloud session.

| Service | Purpose | Key fields |
| --- | --- | --- |
| `write_equipment_timer` | Configure one equipment timer slot (8 slots per season). | `season` (Winter/Summer), `slot_index` (0–7), `enabled`, `start_hour`/`start_min`, `start_mode` (Normal/Dusk/Dawn), `stop_hour`/`stop_min`, `stop_mode`, `equipment` (multi-select: PoolSpa, FilterPump, Heater, Outlet1–4, Valve1–4, Relay1–2), `pump_speed` (Low/Medium/High) |
| `write_lighting_timer` | Configure one lighting timer slot (2 slots per season). | `season`, `slot_index` (0–1), `enabled`, `start_hour`/`start_min`, `start_mode`, `stop_hour`/`stop_min`, `stop_mode`, `zones` (list of zero-based zone indices 0–3) |
| `write_heat_demand` | Configure the heater-demand window. All fields optional; omitted ones keep the last known value. | `enabled`, `window_enabled`, `start_hour`/`start_minute`, `stop_hour`/`stop_minute`, `activated` |
| `set_light_colour` | Set a light zone's colour/effect (colour-capable models only). An invalid name returns the valid options for your light model. | `colour` (model-specific name, e.g. "Blue", "Disco"), `zone` (0–3) |
| `synchronise_light_colour` | Sync all zones to the given zone's colour. | `zone` (0–3) |

The bundled timer card calls `write_equipment_timer`, `write_lighting_timer`, and `write_heat_demand` for you.

## Bundled Lovelace Cards

Two custom Lovelace cards ship with the integration:

- `custom:halo-timer-card`: staged editor with mode tabs for **Equipment timers** (8 slots per season), **Lighting timers** (2 slots per season), and the **Heat-demand window**.
- `custom:halo-schedule-view`: read-only timeline view of active equipment runs.

The integration serves the card modules at these paths:

```yaml
resources:
  - url: /astralpool_halo_cloud/halo-timer-card.js
    type: module
  - url: /astralpool_halo_cloud/halo-schedule-view.js
    type: module
```

Example cards:

```yaml
type: custom:halo-timer-card
entity: sensor.halo_<SERIAL>_equipment_timer_summary
device_id: <HA_DEVICE_ID>
name: Pool Equipment Timers
```

```yaml
type: custom:halo-schedule-view
entity: sensor.halo_<SERIAL>_equipment_timer_summary
name: Pool Schedule
```

Notes for wiring:

- `entity` should point at the `equipment_timer_summary` sensor for the Halo device.
- `device_id` is recommended on `halo-timer-card` so service calls target the correct chlorinator when more than one Halo device exists.
- Use `button.halo_<SERIAL>_refresh_timer_config` after changing timers if you want an immediate readback sweep.
- The timer card's mode tabs cover equipment timers, lighting timers, and the heat-demand window. `halo-schedule-view` visualises equipment runs only.

## Recommended Dashboard

This is a small Lovelace entities card for normal monitoring and careful manual control. Replace `halo_<SERIAL>` with your actual device prefix.

```yaml
type: entities
title: Pool
show_header_toggle: false
entities:
  - entity: binary_sensor.halo_<SERIAL>_connected
    name: Cloud connected
  - entity: sensor.halo_<SERIAL>_info_message
    name: Info
  - entity: sensor.halo_<SERIAL>_error_message
    name: Error
  - type: divider
  - entity: select.halo_<SERIAL>_mode_select
    name: System mode
  - entity: sensor.halo_<SERIAL>_mode
    name: Mode readback
  - entity: select.halo_<SERIAL>_pump_speed_select
    name: Manual pump speed
  - entity: sensor.halo_<SERIAL>_current_operating_speed
    name: Current operating speed
  - type: divider
  - entity: sensor.halo_<SERIAL>_water_temperature
    name: Water temperature
  - entity: sensor.halo_<SERIAL>_ph_measurement
    name: pH
  - entity: number.halo_<SERIAL>_ph_setpoint_control
    name: pH setpoint
  - entity: sensor.halo_<SERIAL>_orp_measurement
    name: ORP
  - entity: number.halo_<SERIAL>_orp_setpoint_control
    name: ORP setpoint
  - type: divider
  - entity: select.halo_<SERIAL>_heater_mode_select
    name: Heater mode
  - entity: number.halo_<SERIAL>_heater_setpoint_control
    name: Heater setpoint
  - entity: binary_sensor.halo_<SERIAL>_heater_on
    name: Heater on
  - entity: sensor.halo_<SERIAL>_heater_water_temperature
    name: Heater water temperature
  - type: divider
  - entity: number.halo_<SERIAL>_connection_pause_minutes
    name: Cloud pause duration
  - entity: button.halo_<SERIAL>_pause_cloud_connection
    name: Pause cloud connection
  - entity: button.halo_<SERIAL>_resume_cloud_connection
    name: Resume cloud connection
```

> [!NOTE]
> Some controls may be unavailable until the integration has received the first live payload and the controller has reported the relevant capability.

## Firmware Compatibility

**Read this before installing.** Whether this integration can onboard your controller depends on its firmware.

BLE onboarding (the normal setup path) works on **older Halo firmware, up to and including `2.3`**. On these versions the controller computes the pairing key locally and the integration can complete pairing on its own.

On **firmware `2.7` and newer, BLE onboarding cannot complete.** This is not a bug in this integration and it cannot be fixed here. On newer firmware AstralPool moved the pairing-key computation to their cloud and gated it behind mobile app attestation (Google Play Integrity / Apple App Attest). The controller will only accept a pairing key issued by AstralPool's server, and the server only issues one to a genuine, unmodified copy of the official app running on a non-rooted device. A third-party client has no way to produce a valid attestation token, so pairing times out after the session-key read with no error.

The reverse-engineering behind this is documented, with source-level evidence, by the community in [issue #1](https://github.com/robmarkoski/pychlorinator-cloud/issues/1). Credit to [@davidbell81](https://github.com/davidbell81) for the analysis.

| Firmware | BLE onboarding | Notes |
| --- | --- | --- |
| `2.3` and earlier | Works | Local pairing-key computation. Normal setup flow applies. |
| `2.4` to `2.6` | Uncertain | The attestation gate is keyed off the controller's protocol revision, not a fixed firmware number. Some controllers in this range may pair; others may not. Report your result on the tracker. |
| `2.7` and newer | Blocked | Server-side attestation wall. BLE onboarding will time out. |

### If you are on blocked firmware

Once the integration has your cloud credentials it does not need BLE again, so the wall is only an *onboarding* problem. If you can already obtain your controller's cloud credentials (serial number, generated username, and generated device password) by other means, the hidden manual credential path (see [Credential Model](#credential-model)) lets you set up the integration without BLE.

Extracting those credentials from the official app is the hard part and is getting harder: older app builds printed them to the Android system log, but that has been closed in recent app versions. There is no supported, general method provided here. Downgrading controller firmware to `2.3` or earlier, where supported, is the only route that restores normal BLE onboarding.

If your controller was already paired to this integration on older firmware and later took a firmware update, your stored credentials keep working — you only hit the wall when trying to onboard a controller fresh on new firmware.

## Installation

### Prerequisites

- Home Assistant Core compatible with the HACS metadata. `hacs.json` currently declares `2024.6.0`.
- HACS installed.
- A BLE adapter available to Home Assistant for onboarding.
- A Halo controller already registered and working in the official AstralPool app.
- Physical access to the controller so you can put it into Pair Mode.
- Willingness to stop the vendor app and any other BLE/cloud integrations while setting this up.

### Credential Model

This integration does **not** use your AstralPool app account email and password.

The normal setup flow is a hybrid:

1. Home Assistant discovers the chlorinator over BLE.
2. You put the chlorinator into Pair Mode.
3. The integration reads the pairing access code from the BLE advertisement.
4. The integration registers a short username on the controller, default `HAUser`.
5. The controller returns password fragments over BLE.
6. Home Assistant stores the serial number, chosen username, and generated device password.
7. Normal operation then switches to the cloud WebSocket.

There is a hidden manual credential path in the config flow, but it is a fallback/debug path for people who already know the serial number, username, and generated device password. Most users will not have those credentials until BLE pairing has succeeded.

### HACS Install

1. Open **HACS** in Home Assistant.
2. Open the HACS settings menu and choose **Custom repositories**.
3. Add this repository:

   ```text
   https://github.com/robmarkoski/pychlorinator-cloud
   ```

4. Set the repository type to **Integration**.
5. Search HACS integrations for **AstralPool Halo Cloud**.
6. Install it.
7. Restart Home Assistant before adding or reloading the integration.

### Integration Setup and Pairing

1. In Home Assistant, go to **Settings -> Devices & Services**.
2. Choose **Add Integration**.
3. Search for **AstralPool Halo Cloud**.
4. Home Assistant will look for BLE devices advertising as `HCHLOR`.
5. When the integration says it found the chlorinator, press **Submit** in Home Assistant first.
6. Immediately enable **Pair Mode** on the chlorinator panel.
7. Choose a username when prompted. Keep it 14 characters or less. The default `HAUser` is fine.
8. Wait for BLE pairing to complete. The integration registers the username and receives the generated cloud password.
9. Choose the Home Assistant device name and optional area.
10. Wait for Home Assistant to create entities. After BLE pairing, the integration deliberately waits about one minute before the first cloud connection so the controller can finish releasing its pairing session.

### Verification

After setup:

- `binary_sensor.<device>_connected` should turn on when the cloud session is active.
- `sensor.<device>_last_update` should populate after live data arrives.
- `sensor.<device>_info_message` should become something other than `unknown` once the first state payload is parsed.
- Core chemistry and temperature sensors should populate if the controller is reporting those values.
- Write controls should stay unavailable until the cloud session is connected and the needed readbacks are known.

### Common Install Pitfalls

| Symptom | Likely cause | What to try |
| --- | --- | --- |
| BLE discovery times out | Home Assistant cannot see the controller BLE advertisement | Check BLE adapter availability, range, and Bluetooth proxy routing. Move closer if needed. |
| Pairing times out | Pair Mode was not enabled at the right time | Start the HA step first, then enable Pair Mode immediately when prompted. |
| Pairing fails | Another BLE client or app is connected | Close the vendor app and stop other BLE integrations. |
| Cloud does not connect immediately after pairing | Normal post-pair cloud settle delay | Wait about one minute. The integration delays the first cloud connection after BLE pairing so the controller can finish releasing its pairing session. |
| `AstralPool Halo Cloud unreachable` notification appears, sessions die after 5 to 30 seconds, controls go unavailable | The AstralPool cloud relay (`*.connectmypool.com.au`) is degraded, or the controller's cloud bridge is stuck | First check whether it is a server problem: open the official **AstralPool Halo Chlor** app and try to connect. If the official app also cannot hold a connection (or only Bluetooth works), the cloud relay or the controller bridge is unhealthy. The integration keeps retrying with backoff. If this goes on for a while and the official app also fails, the controller's cloud bridge has likely got stuck in an "unavailable" state that does **not** clear by itself. Turn the controller off at the mains, wait about 30 seconds, then turn it back on. See the power-cycle warning below. |
| Cloud connection fails or disconnects | Vendor app or another integration owns the cloud session | Close the vendor app. Stop any BLE/cloud integration using the same controller. |
| Entities stay `unknown` | No first live payload yet, or cloud session churn | Wait briefly, then check Home Assistant logs for connect, busy, auth, or timeout errors. |

## Local and BLE Mode

This Home Assistant integration is cloud-first.

BLE is currently used for onboarding only: discovery, Pair Mode detection, username registration, and generated password retrieval. The integration does not use BLE for normal polling or control after setup.

The Python package contains lower-level local/LAN DTLS client code that can derive a local session key from the four-character access code. That code is not exposed as a normal Home Assistant mode, is not presented as a supported LAN runtime here, and should be treated as experimental library plumbing.

## Operational Warnings

> [!WARNING]
> **Do not run the vendor app and Home Assistant at the same time unless you expect disconnects.**
>
> The cloud service appears to allow very limited simultaneous access. If the vendor app is open, this integration may be disconnected or may be unable to reconnect.

> [!WARNING]
> **Do not run a BLE integration and this cloud integration against the same chlorinator at the same time.**
>
> Pick one control path. Running both can cause cloud disconnects, BLE pairing failures, stale state, and unpredictable controller behavior.

> [!WARNING]
> **If it stops connecting, power-cycle the controller.**
>
> The controller allows one cloud session at a time, and its cloud bridge can get stuck in an "unavailable" state after too many rapid reconnects or after fighting the vendor app for the slot. When that happens, sessions drop after a few seconds or the relay refuses to connect at all, and it does not recover on its own. Turn the controller off at the mains, wait about 30 seconds, then turn it back on. That clears it.
>
> While it is stuck, do not keep forcing reconnects and do not run the vendor app at the same time, as both make it worse. Use the Pause Cloud Connection button to back off until you can power-cycle.

> [!CAUTION]
> Pool equipment can affect water chemistry and hardware. Keep the vendor app, the physical controller, and direct water testing as ground truth. Dosing controls in Home Assistant are not a substitute for water testing.

## Planned Features

No promises and no timeline. These are the main known gaps:

- Entity relevance smarts. Hide acid entities when the controller has no dosing hardware (`dosing_capable`), disable manual pump/mode controls while AI mode is active, and hide unconnected equipment (unenabled valves/GPOs) from the timer card.
- Discover real equipment on pairing. Generate named entities for the valves/GPOs/relays that are actually connected (using the controller's custom names) and support assigning them to rooms/areas.
- FlexSettings (`0x006B`). The controller sends salt/mineral mode, a mineral-replace reminder date, and — on this firmware — an acid pump flow rate and daily acid dose limit (as float fields). Decoding the newer float layout is pending deeper firmware analysis; the real flow rate would replace the assumed mL/min used by the acid reservoir estimate.
- Event/fault log (`0x025B`) as a "recent alerts" sensor.
- Heater cooldown countdown sensor (`0x0450`), solar config/control (`0x04B1`), and other unextracted characteristics (equipment config v2, device profile, firmware update entity).
- Pump speed and system mode write reliability. There is a known revert issue being investigated.
- Local-mode / direct-controller bypass. Currently the integration is cloud-only via AstralPool's relay. A direct-WiFi or LAN-discovery path would survive cloud outages but is not implemented.
- Optional logical child devices for pump, lights, valves, and other accessory groups, using Home Assistant `via_device` for better area targeting and dashboards.
- Broader firmware and hardware matrix testing. This has only been tested on one controller so far.
- HACS default repository inclusion. For now it must be added as a custom repository.

## Troubleshooting and Bug Reports

Open an issue at:

```text
https://github.com/robmarkoski/pychlorinator-cloud/issues
```

Include:

- Home Assistant Core version.
- Integration version.
- Controller firmware if you can see it.
- Which install path you used: HACS custom repository, manual copy, or development checkout.
- Whether the vendor app, a BLE integration, or another cloud client was connected at the same time.
- Sanitised Home Assistant logs around setup, connect, disconnect, or write failures.

Do not include:

- AstralPool account credentials.
- Generated device passwords.
- Full serial numbers if you are not comfortable sharing them publicly.
- Raw logs containing tokens, passwords, or private network details.

## Contributing

Contributions are welcome, but this project needs careful testing more than broad feature churn.

Run the isolated setpoint and number-platform tests with:

```sh
python -m pip install 'websockets>=12.0'
python -m unittest discover -s tests -t . -v
```

The tests use Home Assistant stubs and mock network sends. They verify command
encoding, preserved setpoints, validation, and entity behaviour; they do not
verify Home Assistant setup or acceptance of commands by a physical controller.

Good issues and pull requests include:

- Clear reproduction steps.
- Exact entity names and states.
- Sanitised logs.
- Firmware/controller details where available.
- Confirmation that no vendor app or BLE integration was connected during the test, unless contention is the issue being reported.

Please keep safety-critical or dosing-related changes conservative.

## Acknowledgements

This project builds on the open-source Halo BLE ecosystem, especially the parser concepts and terminology established by earlier `pychlorinator` and Home Assistant BLE integration work. That prior work made the cloud packet mapping much easier to validate.

It also depends on Home Assistant and HACS for the integration and distribution model.

## License

This project is licensed under the [MIT License](LICENSE).

## Final Warning

> [!CAUTION]
> **Stop before installing this. This is work in progress, pre-release software.**
>
> This integration may break at any time. There is no stability promise, no warranty, and no vendor support path.
>
> It has been tested on **one firmware version only: `pbver 2.3`**. Other firmware versions, controller variants, regions, and accessory combinations are untested.
>
> **Strong recommendation: do NOT use this. Use a BLE-based integration instead.** The cloud path is contended, brittle, depends on internet access, and depends on Astral's servers.
>
> **DO NOT run this integration and a BLE integration against the same chlorinator at the same time. Pick one.** Running both can cause cloud disconnects, BLE pairing failures, and unpredictable controller state.
>
> This project is unofficial, reverse-engineered, not affiliated with AstralPool, Astral, Fluidra, or Astral Labs, and may stop working whenever the vendor backend changes.
