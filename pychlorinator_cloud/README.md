# pychlorinator_cloud

`pychlorinator_cloud` is a Python client for AstralPool Halo chlorinators that talks to the vendor cloud protocol instead of Bluetooth.

It was built by reverse-engineering the HaloChlorGO mobile app and validating the cloud payloads against real device data and existing Home Assistant BLE entities.

## Why this exists

The existing Halo ecosystem is heavily BLE-centric:

- the upstream `pychlorinator` library speaks Bluetooth
- the existing Home Assistant integration depends on BLE and often an ESP32 proxy
- remote access depends on the vendor app

This package exists to provide a cloud-native alternative:

- no BLE adapter required
- no ESP32 proxy required
- no physical proximity required
- usable as a standalone Python library
- suitable as the basis for a Home Assistant cloud integration

## Current status

### Read side

Working and validated against live hardware:

- WebSocket cloud connection
- live state streaming
- pH
- ORP
- water temperature
- pump state
- pump speed
- cell state
- cell current
- cell level
- chlorine status
- info/error state
- statistics and runtime counters from additional commands

### Write side

Partially reverse-engineered but **not yet fully confirmed**:

- the app clearly uses action writes and dedicated setpoint writes
- the action and setpoint formats are now much better understood from the decompiled code
- a complete, live-confirmed cloud write implementation is still in progress

### BLE pairing

Documented, not yet implemented in this package:

- the chlorinator generates the cloud password during BLE pairing
- the password generation flow has been identified from the decompiled app and related BLE parsers
- this matters for first-time setup when credentials are not already known

## Key findings

## 1. The current cloud protocol is WebSocket-based

The live cloud protocol used by current Halo devices is simpler than the older STUN/DTLS path still visible in the decompiled app.

The working path is:

1. connect to the signalling WebSocket
2. authenticate with the chlorinator serial number, username, and password
3. receive `connectresp`
4. receive live `dataexchange` messages containing base64-encoded binary packets
5. send periodic `keepalive` messages

In practice this means the modern read path does **not** require direct DTLS/WebRTC handling.

## 2. The cloud payloads reuse BLE-style binary structures

The most important reverse-engineering result is that cloud packets reuse the same underlying binary layouts as the BLE protocol, or very close variants of them.

That means the existing BLE parsers and decompiled struct definitions are the best reference for understanding cloud payloads.

Examples:

- `0x0068` is the main live state packet
- `0x0259` carries precise measurement data such as water temperature and cell current
- `0x0324` carries configuration/setup-style data, including pump speed in one sub-command
- `0x0066` carries statistics/runtime counters
- `0x0009` carries info/error-related state

## 3. `0x0068` is the core state packet

The cloud `0x0068` payload was matched back to the authoritative Halo BLE `StateCharacteristic3` layout from the decompiled/parser sources.

That mapping is the foundation for read-side parity.

From the cloud body:

- byte `0`: flags bitfield
- byte `1`: real cell level
- bytes `2:4`: cell current (uint16 LE)
- byte `4`: main/info state enum
- byte `5`: chlorine status enum
- bytes `6:8`: ORP measurement (uint16 LE)
- byte `8`: pH status enum
- byte `9`: pH × 10
- byte `10`: timer/status enum
- bytes `11:13`: timer extra bytes
- bytes `13:15`: error enum/code
- byte `15`: extra state/flag byte
- byte `16`: trailing extra byte seen in the cloud form

## 4. The chlorinator effectively supports one active control path at a time

In practice, BLE and cloud access do not coexist well.

Observed behaviour:

- if BLE is actively connected or polling, cloud sessions can fail with busy errors
- even when the cloud path connects, the device can be unstable if BLE is still in use
- for best results, cloud validation should happen while BLE is disabled

This is an architectural constraint of the device, not just a parser issue.

## 5. The decompiled app contains both legacy and current paths

The APK still contains older signalling/STUN/DTLS code, which initially suggested that the whole protocol needed direct peer-to-peer DTLS.

That code is still useful background, but live testing showed a newer protocol path is now active:

- signalling WebSocket with app-level Basic Auth
- `connect` / `connectresp`
- `dataexchange`
- `keepalive`
- `disconnect`

The repository keeps the older DTLS/STUN analysis because it helps explain the product history and may still matter for older devices or LAN cases.

## Getting credentials

To connect to a chlorinator you need:

- serial number
- username
- generated device password

That password is created during BLE pairing.

High-level pairing flow:

1. connect over BLE
2. send the username using BLE command `719`
3. receive password fragments using BLE command `720`
4. concatenate the fragments to form the stored cloud password

If the device has already been added in the mobile app, those credentials can be recovered from traffic or app state during research. A clean implementation of pairing is still planned.

## How the cloud protocol works

### Signalling

The client opens a WebSocket to the Halo signalling endpoint and sends a JSON connect message.

The server replies with `connectresp` and then starts streaming live packets.

### Live packets

Each `dataexchange` message contains base64-encoded binary.

That binary is structured as:

- byte `0`: prefix
- bytes `1:3`: command id, little-endian
- bytes `3:`: command-specific body

### Keepalive

The live service disconnects quickly if keepalives are not sent.

The decompiled app uses:

- transmit keepalive every ~2 seconds
- receive timeout around 8 seconds

In practice, a keepalive interval of about 2–3 seconds is required for a stable session.

## Read coverage

The following command mappings are the most important currently understood ones.

| Command | Purpose | Notes |
|---|---|---|
| `0x0068` | Main live state | pH, ORP, flags, cell level, cell current, info/error-related state |
| `0x0259` | Extended measurements | precise water temperature, cell current |
| `0x0009` | Info/error state | info/error-related message bytes |
| `0x0324` | Config/setup subcommands | includes pump speed in sub-command `0x03` |
| `0x0066` | Statistics/runtime | cell usage/runtime-style counters |
| `0x0064` / `0x0065` | Capabilities/config | still partially mapped |
| `0x044e` | Unknown | seen in captures, not yet fully understood |

## Write-path findings

The decompiled app shows that the write path is more nuanced than a single generic command.

### App actions

The app uses action helpers such as:

- mode to auto/manual/off
- speed to low/medium/high
- pool/spa selection
- dismiss info message
- disable acid dosing
- maintenance period actions
- heater/solar/lighting actions

The decompiled code strongly suggests these go through a `SendAppAction(action, payload)` path.

Important observation:

- some actions include extra payload bytes, not just an action enum
- maintenance actions pass period bytes
- equipment actions pass a one-byte equipment index
- acid dosing actions pass either a period or a one-byte enable/disable payload

### Action frame structure

The upstream Halo parser definitions point to a chlorinator action layout equivalent to:

- header bytes: `03 f4 01`
- action byte
- 4-byte integer payload/period
- padding

This is a strong lead for cloud writes, but it has not yet been fully live-confirmed over the WebSocket cloud path.

### Setpoints are separate

The decompiled app also shows that pH/ORP setpoint changes do **not** go through the generic app action path.

Instead, the app updates internal setpoint properties and calls a separate `WriteSetPoint()` path.

That means cloud writes likely break down into at least two categories:

1. app actions
2. setpoint writes

That distinction matters for any future stable control implementation.

### Setpoint bounds

The library now keeps explicit pH/ORP validation alongside the setpoint write helper
in `pychlorinator_cloud/setpoints.py`.

Current best-supported bounds:

- pH setpoint: `7.2` to `7.6` in `0.1` steps
- ORP setpoint: `650` to `800` mV

Source quality note:

- the available decompiled app artefacts in this repo clearly confirm the separate
  `WriteSetPoint()` path and the `0x0066` setpoint packet shape
- but this repo's current decompiled source bundle does **not** expose a directly
  recoverable hard-coded min/max pair for pH/ORP
- so the bounds above are the best-supported Halo manual / product-document values
  currently available in the project research, and are enforced to stop obviously
  out-of-family writes until tighter app-confirmed limits are recovered

## Home Assistant

This repository also contains a custom Home Assistant component under:

`custom_components/astralpool_halo_cloud/`

The current direction is:

- keep the Python library generic
- use the custom component as an integration layer
- get read-side parity with BLE first
- then add safe controls once write behaviour is fully confirmed

## Project layout

```text
projects/chlorinator-capture/
├── pychlorinator_cloud/                 # Python package
├── custom_components/
│   └── astralpool_halo_cloud/           # Home Assistant custom component
├── docs/
│   ├── protocol-analysis.md
│   └── commands-and-auth.md
├── examples/
├── scripts/
├── captures/
└── apk/                                 # Decompiled app + extracted artefacts
```

## Package usage

```python
import asyncio
from pychlorinator_cloud.websocket_client import HaloWebSocketClient


async def main():
    client = HaloWebSocketClient(
        serial_number="YOUR_SERIAL",
        username="YOUR_USERNAME",
        password="YOUR_PASSWORD",
    )

    ok = await client.connect()
    if not ok:
        raise RuntimeError("Failed to connect")

    await asyncio.sleep(10)

    print("Mode:", client.data.mode)
    print("pH:", client.data.ph_measurement)
    print("ORP:", client.data.orp_mv)
    print("Temperature:", client.data.water_temperature_c)
    print("Pump speed:", client.data.pump_speed)
    print("Cell current:", client.data.cell_current_ma)

    await client.disconnect()


asyncio.run(main())
```

## Known limitations

- write support is not yet fully confirmed
- BLE pairing/password generation is not yet implemented in-package
- some cloud-only or BLE-only fields are still being mapped
- the device does not behave well with concurrent BLE and cloud sessions
- the vendor could change the cloud protocol at any time

## Reverse-engineering references

Useful reference points used during this work:

- decompiled HaloChlorGO Android app
- extracted Halo P2P/STUN/WebSocket code
- upstream `pychlorinator` BLE parser definitions
- existing Home Assistant BLE integration
- live captures and direct cloud session testing against real hardware

## Safety and legality

This package is based on reverse engineering performed for interoperability with personally owned hardware.

You should:

- only use it with devices you own or are authorised to test
- expect the vendor protocol to change
- avoid assuming control features are safe until they are fully validated

## Release outlook

For a future public release, the likely milestones are:

- stable read-side API
- fully documented packet mapping
- confirmed action writes
- confirmed setpoint writes
- optional BLE pairing helper
- polished Home Assistant integration
- packaging and distribution

Until then, this repository should be treated as an active reverse-engineering and integration project rather than a finished public library.
