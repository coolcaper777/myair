# Architecture notes

Internal notes on how `plugin.py` is put together, for future maintenance. See `README.md` for user-facing documentation.

## Threading model

MyAir's local API is a plain synchronous HTTP GET/query API, so this plugin needs no `asyncio` event loop or background thread — it uses Indigo's standard `runConcurrentThread()` polling loop directly on the concurrent thread Indigo already provides.

```
runConcurrentThread():
    while True:
        for each enabled+configured acSystem device:
            _poll_system(dev)
        self.sleep(self.poll_interval)   # 15s
```

Actions (`actionControlThermostat`, `setAcMode`, etc.) run synchronously on the main thread and block briefly on the HTTP request in `_send_command`/`_get_system_data` (5s timeout) — acceptable here since MyAir controllers respond quickly on the local network and there's no persistent connection to manage.

## HTTP layer

Two thin wrappers around `urllib.request`, both centralizing error handling so every caller gets consistent behavior:

- **`_get_system_data(ip, retries=2, retry_delay=1.0)`** — `GET /getSystemData`. The controller can briefly return a response with no `aircons` key right after processing a command (observed in practice, and guarded against in `homebridge-myplace`'s own retry logic) — this is tolerated with up to 2 retries before being treated as a real failure. A connection-level failure (bad IP, offline controller) fails fast without retrying, since retrying a dead host 3x just delays the warning.
- **`_send_command(ip, payload)`** — `GET /setAircon?json=<url-encoded payload>`. Returns `True`/`False`; every caller checks the result before logging success and re-polling (`self._poll_system(dev)`) to pull the confirmed state back rather than assuming the command applied instantly.

Both log at `warning` for connectivity failures and `exception` for anything unexpected (bad JSON, wrong response shape), so a persistently offline controller doesn't spam full tracebacks every 15s.

## Poll → state mapping (`_poll_system`)

Reads `data["aircons"]["ac1"]["info"]` and `["zones"]`, then:

1. Computes `hvac_mode` from `state`+`mode` (MyAir has no notion of "off" as a mode — `state == "off"` always maps to `indigo.kHvacMode.Off` regardless of what `mode` says).
2. `hvacHeaterIsOn`/`hvacCoolerIsOn` are approximated as "on and in that mode" — MyAir doesn't report compressor activity separately. This drives the device list's orange/blue heat/cool icon.
3. Writes the thermostat's native states plus the custom `acMode`/`acFanSpeed`/`acZoneCount`/`acCountdownOn`/`acCountdownOff`.
4. If the device still has Indigo's placeholder name (`NEW_DEVICE_NAME_RE`, e.g. "new device"), **skips** `_discover_zones`/`_discover_mode_buttons`/`_discover_fan_controls` for this poll — otherwise child devices would get named after the placeholder permanently. The very next poll after the user renames the device picks discovery back up.
5. Always calls `_poll_zones`/`_sync_mode_buttons`/`_sync_fan_controls` regardless (existing children still need their state refreshed even while discovery is deferred).

### `ShowCoolHeatEquipmentStateUI` prop backfill

`deviceStartComm` explicitly pushes `ShowCoolHeatEquipmentStateUI = True` onto the device's `pluginProps` if it isn't already set, then calls `stateListOrDisplayStateIdChanged()` and sleeps 1s before the first poll. `Devices.xml`'s `defaultValue="true"` only applies to devices *created* after that default was added — existing devices keep whatever props they were created with, so this is what actually adds `hvacHeaterIsOn`/`hvacCoolerIsOn` to already-existing systems' state lists. The 1s sleep mirrors what Home Assistant Agent (installed on the same server) does after the same two calls, since the state-list/capability rebuild triggered by `replacePluginPropsOnServer` + `stateListOrDisplayStateIdChanged` isn't synchronous.

## Child device discovery/sync split

Each child device type has a `_discover_*` (create if missing) and `_sync_*`/`_poll_*` (reflect current state) pair, both keyed off `pluginProps["systemDevice"] == str(system_dev.id)`:

| Type | Discover | Sync |
|---|---|---|
| Zone | `_discover_zones` — one per key in `zones` dict, named from the controller's own zone name | `_poll_zones` — writes `onOffState`/`brightnessLevel`/`zoneTemp`/`zoneSetTemp`/`zoneSignalStrength`; flags via `setErrorStateOnServer` if a zone stops being reported |
| Mode button (Vent/Dry) | `_discover_mode_buttons` — one per `MODE_BUTTON_LABELS` entry | `_sync_mode_buttons` — `onOffState = (this button's mode == active mode)` |
| Fan speed control | `_discover_fan_controls` | `_sync_fan_controls` — `speedIndex` from `FAN_SPEED_TO_INDEX` |
| Fan auto button | `_discover_fan_controls` (same call, second half) | `_sync_fan_controls` — `onOffState = fan in FAN_AUTO_VALUES` |

`_get_system_device(child_dev)` resolves a child back to its parent `acSystem` device via the `systemDevice` prop — every child action handler starts here.

## Constants and API quirks (top of file)

- `FAN_AUTO_VALUES = {"autoAA", "auto"}` — MyAir always *sets* fan speed as `"autoAA"`, but `getSystemData` has been observed *reporting back* either `"autoAA"` or plain `"auto"` for the same state, so both are recognized on read.
- `NEW_DEVICE_NAME_RE` — matches Indigo's placeholder device names (`"new device"`, `"new device 2"`, …), used solely to defer child-device creation (see above).
- `FAN_SPEED_TO_INDEX` / `FAN_INDEX_TO_SPEED` — Low/Medium/High ↔ speedcontrol index 1/2/3; index 0 has no MyAir equivalent (see README).

## Action handlers

All action handlers follow the same shape: resolve the parent system device (`_get_system_device` for children) → resolve its IP → build the minimal JSON payload MyAir expects → `_send_command` → on success, log at `info` and call `_poll_system` to pull back confirmed state. `actionControlDimmerRelay` is shared across zone, mode-button, and fan-auto-button devices and dispatches on `dev.deviceTypeId` at the top.

## Debug logging

`self.debug` / `self.indigo_log_handler` level is set from the `showDebugInfo` plugin preference (`PluginConfig.xml`) in `__init__` and re-applied live in `closedPrefsConfigUi`. Debug-level calls cover the HTTP layer (request URL, full response/command payloads), per-poll parsed state, per-zone updates, state-sync transitions, and every action entry point — since this plugin's local API integration is the least-tested part of the codebase, verbose payload logging is the fastest way to diagnose a field the controller reports differently than expected.
