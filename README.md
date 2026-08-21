# MyAir

An [Indigo Domotics](https://www.indigodomo.com/) plugin that integrates **Advantage Air MyAir** ducted air conditioning controllers over their local network API, exposing the AC system, its zones, and its fan/mode controls as native Indigo devices.

- **Requires:** Indigo Server API 3.1+
- **Controller connection:** local HTTP, no cloud/account required — talks directly to the MyAir controller/tablet on port 2025

> The MyAir local API's field/value shapes were cross-checked against [uswong/homebridge-myplace](https://github.com/uswong/homebridge-myplace)'s `MyPlace.sh`, an actively-maintained script that talks to the same API. The shape is confirmed, but this plugin itself is still lightly tested against real hardware — enable debug logging (below) if something looks off and compare the logged payloads against what your controller actually reports.

## Installation

1. Download the latest release's zip from the [Releases page](https://github.com/coolcaper777/myair/releases) — Safari extracts it automatically into a folder named `MyAir.indigoPlugin`.
   - *(Cloning the repo instead? The checked-out folder is named `myair`, not `MyAir.indigoPlugin` — rename it before installing, or Indigo won't recognize it as a plugin.)*
2. Double-click `MyAir.indigoPlugin` (or drag it onto the Indigo Server icon) to install, or copy it into `~/Library/Application Support/Perceptive Automation/Indigo <version>/Plugins/`.
3. Restart the plugin from Indigo's Plugins menu. No external Python dependencies are required.
4. Create a **MyAir AC System** device with your controller's IP address; zones, mode buttons, and fan controls are then auto-discovered.

## What it does

- Polls the MyAir controller every 15 seconds for system and per-zone state.
- Auto-discovers and creates child devices for every zone, plus Vent/Dry mode buttons and fan speed controls, the first time it sees a configured AC System device.
- Maps AC state onto a native Indigo **thermostat** device (so Cool/Heat/Off and setpoints work with Indigo's built-in thermostat UI and triggers).

## Devices

### MyAir AC System (`acSystem`) — thermostat

| Config field | Description |
|---|---|
| MyAir Controller IP Address | Local IP of the MyAir tablet/controller |

Uses Indigo's native thermostat setpoint/mode (Cool/Heat/Off) plus custom states for what doesn't fit that model:

| State | Description |
|---|---|
| `acMode` | Raw MyAir mode (`Cool`/`Heat`/`Vent`/`Dry`) |
| `acFanSpeed` | `Low`/`Medium`/`High`/`Auto` |
| `acZoneCount` | Number of zones reported |
| `acCountdownOn` / `acCountdownOff` | Minutes until a scheduled on/off, if set |

Vent and Dry have no native Indigo HVAC mode equivalent, so they're only reachable via the **Set AC Mode** action (see below) — the native thermostat mode control only covers Cool/Heat/Off.

### MyAir Zone (`zoneDevice`) — dimmer

One per zone, auto-created under the AC System it belongs to. Brightness = damper open %.

| Config field | Description |
|---|---|
| AC System | Which AC System this zone belongs to |
| Zone Number | As configured on the MyAir controller |
| Log events and changes to Indigo log | Per-device toggle for info-level action logging |

| State | Description |
|---|---|
| `zoneTemp` | Zone's own measured temperature, if it has a sensor |
| `zoneSetTemp` | Zone's own target temperature, if supported |
| `zoneSignalStrength` | Zone sensor RF signal |

A zone no longer reported by the controller (e.g. removed on the MyAir side) is flagged with an Indigo error state rather than silently going stale.

### MyAir Mode Button (`modeButton`) — relay

Auto-created (one for Vent, one for Dry) per AC System. Turning one **on** switches the AC to that mode; turning one **off** turns the AC off entirely (there's no other sensible meaning for "mode off").

### MyAir Fan Speed (`fanSpeedControl`) — speed control

Auto-created per AC System. Native Low/Medium/High map to speed index 1/2/3. There's no "off" fan speed independent of the AC itself, so index 0 isn't wired to anything — use the thermostat's Off mode instead.

### MyAir Fan Auto (`fanAutoButton`) — relay

Auto-created per AC System, alongside Fan Speed. Turning on sets the fan to Auto — a genuinely different concept from "off" that doesn't fit into the speed control's index range.

## Actions

| Action | Applies to | Description |
|---|---|---|
| Set AC Mode | AC System | Set Cool/Heat/Vent/Dry — the only way to reach Vent/Dry |
| Set Fan Speed | AC System | Low/Medium/High/Auto |
| Set Zone Temperature | Zone | Only has an effect on zones with their own temperature sensor; silently ignored otherwise |

Standard thermostat actions (native mode Cool/Heat/Off, setpoints, Request Status) and standard dimmer/relay/speed-control actions on the child devices are also supported.

## Debug logging

Plugin → Configure → **Enable Debug Logging** turns on verbose logging of every controller request/response, per-poll parsed state, per-zone updates, and every action Indigo sends before it's translated into a controller command. It can be toggled live without restarting the plugin. Turn it on when a zone, mode, or fan control isn't behaving as expected — the logged payloads show exactly what the controller returned.

## Troubleshooting

- **"No IP address specified":** the AC System (or its zone/button/speed-control's parent AC System) hasn't been configured with a controller IP yet.
- **Zone/system state not updating:** confirm the controller is reachable at the configured IP on port 2025. Enable debug logging to see the raw request/response for each poll.
- **New AC System device not creating zone/mode/fan children:** child devices are deferred while the AC System device still has Indigo's placeholder name (e.g. "new device") — rename it and wait for the next poll (≤15s).

## Credits

- **Plugin:** authored by [coolcaper777](https://github.com/coolcaper777).
- **API reference:** field/value shapes (mode, fan, state, and zone key formats) were cross-checked against [uswong/homebridge-myplace](https://github.com/uswong/homebridge-myplace)'s `MyPlace.sh`, an actively-maintained script that talks to the same MyAir local API.
- **Hardware:** [Advantage Air](https://advantageair.com.au/) MyAir ducted air conditioning controllers.
- **Debug logging toggle, verbose instrumentation, and this documentation** were added with [Claude Code](https://claude.com/claude-code) (Anthropic).
