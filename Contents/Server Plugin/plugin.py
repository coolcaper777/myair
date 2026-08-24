try:
    import indigo
except ImportError:
    pass

import json
import logging
import re
import time
import urllib.error
import urllib.request
import urllib.parse
from typing import Optional

# Indigo's placeholder name for a device that hasn't been renamed yet
# (e.g. "new device", "new device 2" if there's a name collision).
NEW_DEVICE_NAME_RE = re.compile(r"^new device(\s+\d+)?$", re.IGNORECASE)

# Field/value names (mode/fan/state/zone key format) cross-checked against
# uswong/homebridge-myplace's MyPlace.sh, an actively-maintained shell script
# that talks to the real MyAir local API (GET /getSystemData,
# GET /setAircon?json=...). Still untested against real hardware from this
# plugin itself, but the shape of the API is now confirmed rather than guessed.

HVAC_MODE_TO_MYAIR = {
    indigo.kHvacMode.Cool: "cool",
    indigo.kHvacMode.Heat: "heat",
}
MYAIR_MODE_TO_HVAC = {v: k for k, v in HVAC_MODE_TO_MYAIR.items()}

# MyAir always expects "autoAA" when *setting* fan speed, but per
# uswong/homebridge-myplace's MyPlace.sh ("one other possibility is
# autoAA/auto"), getSystemData can *report back* either "autoAA" or plain
# "auto" for the same state - so both need to be recognized when reading.
FAN_AUTO_VALUES = {"autoAA", "auto"}
FAN_SPEED_DISPLAY = {"low": "Low", "medium": "Medium", "high": "High", "autoAA": "Auto", "auto": "Auto"}

# Modes with no native Indigo thermostat mode equivalent - these get a
# dedicated on/off child device instead, since Cool/Heat/Off already have
# the native thermostat dial.
MODE_BUTTON_LABELS = {"vent": "Vent (fan only)", "dry": "Dry"}

# Fan speed: Low/Medium/High map onto speedcontrol's speedIndex 1/2/3.
# Auto is a separate on/off button device - it's a genuinely different
# concept from "off" (MyAir has no fan-off independent of the AC itself),
# so it doesn't get folded into speedIndex 0.
FAN_SPEED_TO_INDEX = {"low": 1, "medium": 2, "high": 3}
FAN_INDEX_TO_SPEED = {v: k for k, v in FAN_SPEED_TO_INDEX.items()}


class Plugin(indigo.PluginBase):
    HTTP_TIMEOUT = 5  # seconds

    def __init__(self, pluginId: str, pluginDisplayName: str, pluginVersion: str, pluginPrefs: indigo.Dict) -> None:
        """Initialize the plugin instance and set the debug logging level.

        Args:
            pluginId (str): This plugin's bundle identifier.
            pluginDisplayName (str): The plugin's display name.
            pluginVersion (str): The plugin's version string.
            pluginPrefs (indigo.Dict): Saved plugin preferences.
        """
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.poll_interval = 15  # seconds between polls of each AC system
        self.debug = self.pluginPrefs.get("showDebugInfo", False)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)

    def startup(self) -> None:
        """Called once by Indigo when the plugin starts running."""
        self.logger.info("MyAir plugin starting up...")
        systems = list(indigo.devices.iter("self.acSystem"))
        self.logger.info(f"MyAir plugin ready - watching {len(systems)} AC system(s)")

    def shutdown(self) -> None:
        """Called once by Indigo when the plugin is stopping."""
        self.logger.info("MyAir plugin shutting down...")

    def closedPrefsConfigUi(self, valuesDict: indigo.Dict, userCancelled: bool) -> None:
        """Apply the plugin preferences dialog's saved values.

        Re-applies the debug logging level live so toggling it takes effect
        immediately, without requiring a plugin restart.

        Args:
            valuesDict (indigo.Dict): The saved preference values.
            userCancelled (bool): True if the dialog was cancelled instead of saved.
        """
        if userCancelled:
            return
        self.debug = valuesDict.get("showDebugInfo", False)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.logger.info(f"Debug logging {'enabled' if self.debug else 'disabled'}")

    def runConcurrentThread(self) -> None:
        """Indigo's polling loop entry point: poll every enabled AC System every ``poll_interval`` seconds."""
        try:
            while True:
                for dev in indigo.devices.iter("self.acSystem"):
                    if not dev.enabled or not dev.configured:
                        continue
                    self.logger.debug(f"Polling {dev.name}...")
                    try:
                        self._poll_system(dev)
                    except Exception:
                        self.logger.exception(f"Error polling {dev.name}")
                self.sleep(self.poll_interval)
        except self.StopThread:
            pass

    def deviceStartComm(self, dev: indigo.Device) -> None:
        """Start communication with a device when it's enabled/created.

        For an AC System device, backfills the ``ShowCoolHeatEquipmentStateUI``
        prop if it predates that Devices.xml default (needed so already-existing
        devices pick up the ``hvacHeaterIsOn``/``hvacCoolerIsOn`` states), then
        polls it immediately if configured.

        Args:
            dev (indigo.Device): The device being started.
        """
        super().deviceStartComm(dev)
        self.logger.debug(f"deviceStartComm: {dev.name} (type: {dev.deviceTypeId})")
        # Refresh the state list in case Devices.xml gained new states since
        # this device was created.
        dev.stateListOrDisplayStateIdChanged()
        if dev.deviceTypeId != "acSystem":
            return
        # ShowCoolHeatEquipmentStateUI's Devices.xml default only applies to
        # devices created after it was added - existing devices keep whatever
        # pluginProps they were created with, so push it explicitly. This is
        # what actually adds hvacHeaterIsOn/hvacCoolerIsOn to the state list.
        if str(dev.pluginProps.get("ShowCoolHeatEquipmentStateUI", "")).lower() != "true":
            new_props = dev.pluginProps
            new_props["ShowCoolHeatEquipmentStateUI"] = True
            dev.replacePluginPropsOnServer(new_props)
            dev.stateListOrDisplayStateIdChanged()
            # The state-list/capability rebuild triggered above isn't synchronous -
            # Home Assistant Agent's plugin (installed on this same server) waits
            # exactly this long after the same two calls before writing any states,
            # so do the same before our first poll writes hvacHeaterIsOn/etc. Only
            # needed this one time, right when the prop actually just changed -
            # not on every device start, and not for the child device types.
            time.sleep(1.0)
        if dev.configured:
            # dev.configured is False for the moment between "New Device" and
            # Save being clicked (pluginProps are still empty then) - polling
            # during that window just produces a misleading "no IP" error.
            self._poll_system(dev)

    def deviceStopComm(self, dev: indigo.Device) -> None:
        """Stop communication with a device when it's disabled/deleted.

        Args:
            dev (indigo.Device): The device being stopped.
        """
        super().deviceStopComm(dev)
        self.logger.debug(f"deviceStopComm: {dev.name} (type: {dev.deviceTypeId})")

    def deviceDeleted(self, dev: indigo.Device) -> None:
        """Flag an AC System's zone/mode/fan-control children as orphaned when it's deleted.

        Polling is entirely driven by iterating existing acSystem devices, so
        deleting one leaves its children with nothing ever looking at them
        again - this flags them rather than letting them silently go stale
        forever.

        Args:
            dev (indigo.Device): The device that was deleted.
        """
        if dev.deviceTypeId != "acSystem":
            return
        system_id = str(dev.id)
        for type_id in ("zoneDevice", "modeButton", "fanSpeedControl", "fanAutoButton"):
            for child in indigo.devices.iter(f"self.{type_id}"):
                if child.pluginProps.get("systemDevice") != system_id:
                    continue
                if not child.errorState:
                    child.setErrorStateOnServer(f'Parent AC System "{dev.name}" was deleted')
                    self.logger.warning(f'{child.name}: parent AC System "{dev.name}" was deleted - this device is now orphaned')

    def closedDeviceConfigUi(self, valuesDict: indigo.Dict, userCancelled: bool, typeId: str, devId: int) -> None:
        """Poll a newly-saved AC System immediately so its name is already finalized.

        Fires the instant the New/Edit Device dialog is dismissed with Save -
        by now the device has whatever name the user actually typed, so this
        is what lets child devices get created under the real name right away
        instead of waiting on the next poll after a rename.

        Args:
            valuesDict (indigo.Dict): The dialog's saved field values.
            userCancelled (bool): True if the dialog was cancelled instead of saved.
            typeId (str): The device type that was configured.
            devId (int): The device's ID.
        """
        if not userCancelled and typeId == "acSystem" and devId in indigo.devices:
            dev = indigo.devices[devId]
            if dev.configured:
                self._poll_system(dev)

    def validateDeviceConfigUi(self, valuesDict: indigo.Dict, typeId: str, devId: int) -> tuple:
        """Validate the New/Edit Device dialog before it's allowed to save.

        Args:
            valuesDict (indigo.Dict): The dialog's current field values.
            typeId (str): The device type being configured.
            devId (int): The device's ID (0 for a device being newly created).

        Returns:
            tuple: ``(True, valuesDict)`` if valid, or
                ``(False, valuesDict, errorsDict)`` with per-field error messages
                if not.
        """
        errors_dict = indigo.Dict()
        if typeId == "acSystem":
            address = valuesDict.get("address", "").strip()
            if not address:
                errors_dict["address"] = "MyAir controller IP address is required."
            elif " " in address:
                errors_dict["address"] = "IP address must not contain spaces."
        elif typeId == "zoneDevice":
            if not valuesDict.get("systemDevice", ""):
                errors_dict["systemDevice"] = "Please select the AC System this zone belongs to."
            if not valuesDict.get("zoneNumber", "").strip():
                errors_dict["zoneNumber"] = "Zone number is required."
        elif typeId in ("modeButton", "fanSpeedControl", "fanAutoButton"):
            if not valuesDict.get("systemDevice", ""):
                errors_dict["systemDevice"] = "Please select the AC System this device belongs to."
        if errors_dict:
            return (False, valuesDict, errors_dict)
        return (True, valuesDict)

    def get_ac_systems(self, filter: str = "", valuesDict: Optional[indigo.Dict] = None, typeId: str = "", targetId: int = 0) -> list:
        """Dynamic menu list for the zone device's 'AC System' picker.

        Args:
            filter (str): Indigo's dynamic-list filter string (unused).
            valuesDict (Optional[indigo.Dict]): The dialog's current field values (unused).
            typeId (str): The device type being configured (unused).
            targetId (int): The device's ID (unused).

        Returns:
            list: ``(device_id, device_name)`` tuples for every AC System device.
        """
        return [(dev.id, dev.name) for dev in indigo.devices.iter("self.acSystem")]

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get_system_data(self, ip: str, retries: int = 2, retry_delay: float = 1.0) -> Optional[dict]:
        """Fetch the controller's full system/zone status via ``GET /getSystemData``.

        The controller can briefly return an empty/incomplete response right
        after processing a command (seen in practice, and guarded against by
        homebridge-myplace's own retry logic) - tolerate a couple of retries
        before treating it as a real failure.

        Args:
            ip (str): The MyAir controller's local IP address.
            retries (int): How many times to retry an empty/incomplete response.
            retry_delay (float): Seconds to wait between retries.

        Returns:
            Optional[dict]: The parsed JSON response, or None on failure.
        """
        url = f"http://{ip}:2025/getSystemData"
        self.logger.debug(f"GET {url}")
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(url, timeout=self.HTTP_TIMEOUT) as resp:
                    raw = resp.read()
                    data = json.loads(raw)
            except (urllib.error.URLError, TimeoutError) as e:
                # Connection-level failure (bad IP, offline controller, network
                # issue) - not the "briefly empty response" quirk the retry loop
                # is for, so fail fast with a short message instead of retrying
                # a dead host 3x and logging a full traceback each time.
                self.logger.warning(f"Could not reach MyAir controller at {ip}: {e}")
                return None
            except Exception:
                self.logger.exception(f"Unexpected error fetching system data from MyAir controller at {ip}")
                return None
            if data.get("aircons"):
                self.logger.debug(f"Response from {ip}: {data}")
                return data
            if attempt < retries:
                self.logger.debug(
                    f"Empty/incomplete response from {ip} (attempt {attempt + 1}/{retries + 1}), retrying..."
                )
                time.sleep(retry_delay)
        self.logger.warning(f"MyAir controller at {ip} kept returning an incomplete response - it may have been mid-command")
        return None

    def _send_command(self, ip: str, payload: dict) -> bool:
        """Send a command to the controller via ``GET /setAircon``.

        Args:
            ip (str): The MyAir controller's local IP address.
            payload (dict): The command payload, e.g. ``{"ac1": {"info": {...}}}``.

        Returns:
            bool: True if the controller accepted the request, False otherwise.
        """
        url = f"http://{ip}:2025/setAircon?json={urllib.parse.quote(json.dumps(payload))}"
        self.logger.debug(f"Sending command to {ip}: {payload}")
        try:
            with urllib.request.urlopen(url, timeout=self.HTTP_TIMEOUT) as resp:
                resp.read()
            self.logger.debug(f"Command to {ip} succeeded")
            return True
        except (urllib.error.URLError, TimeoutError) as e:
            self.logger.warning(f"Could not reach MyAir controller at {ip}: {e}")
            return False
        except Exception:
            self.logger.exception(f"Unexpected error sending command to MyAir controller at {ip}")
            return False

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll_system(self, dev: indigo.Device) -> None:
        """Poll one AC System device: fetch, parse, and write its current state.

        Writes the thermostat's native states plus the custom
        acMode/acFanSpeed/acZoneCount/acCountdownOn/acCountdownOff states, then
        discovers/syncs its zone, mode-button, and fan-control child devices
        (discovery is deferred while the device still has Indigo's placeholder
        name, to avoid naming children after it permanently).

        Args:
            dev (indigo.Device): The AC System device to poll.
        """
        ip = dev.pluginProps.get("address", "")
        if not ip:
            self.logger.error(f"No IP address specified for {dev.name}")
            dev.setErrorStateOnServer("No IP address configured")
            return
        data = self._get_system_data(ip)
        if data is None:
            # _get_system_data already logged the specific reason (unreachable
            # vs. a real error) - just reflect "not OK" onto the device itself,
            # since until now only zone devices ever got an Indigo error state,
            # never the parent AC System that's actually polling the network.
            dev.setErrorStateOnServer("Could not reach controller")
            return
        try:
            ac = data["aircons"]["ac1"]
            info = ac["info"]
        except (KeyError, TypeError):
            self.logger.error(f"Unexpected response shape from {dev.name} at {ip}: {data}")
            dev.setErrorStateOnServer("Unexpected response from controller")
            return

        state = info.get("state", "off")
        mode = info.get("mode", "cool")
        set_temp = info.get("setTemp")
        fan = info.get("fan", "autoAA")
        my_zone = info.get("myZone")
        no_of_zones = info.get("noOfZones")
        countdown_on = info.get("countDownToOn")
        countdown_off = info.get("countDownToOff")
        zones = ac.get("zones", {})

        measured_temp = None
        if my_zone is not None:
            zone = zones.get(f"z{int(my_zone):02d}")
            if zone:
                measured_temp = zone.get("measuredTemp")

        hvac_mode = MYAIR_MODE_TO_HVAC.get(mode, indigo.kHvacMode.Off) if state == "on" else indigo.kHvacMode.Off

        self.logger.debug(
            f"{dev.name}: state={state} mode={mode} setTemp={set_temp} fan={fan} "
            f"myZone={my_zone} measuredTemp={measured_temp} noOfZones={no_of_zones} "
            f"countdownOn={countdown_on} countdownOff={countdown_off}"
        )

        # MyAir doesn't report compressor activity separately from mode+state,
        # so "actively heating/cooling" is approximated as "on and in that
        # mode" - this is what actually drives the device list's orange/blue
        # icon (ShowCoolHeatEquipmentStateUI above), not updateStateImageOnServer.
        states = [
            {"key": "hvacOperationMode", "value": hvac_mode},
            {"key": "hvacHeaterIsOn", "value": state == "on" and mode == "heat"},
            {"key": "hvacCoolerIsOn", "value": state == "on" and mode == "cool"},
            {"key": "acMode", "value": str(mode).title()},
            {"key": "acFanSpeed", "value": FAN_SPEED_DISPLAY.get(fan, str(fan).title())},
        ]
        if no_of_zones is not None:
            states.append({"key": "acZoneCount", "value": no_of_zones})
        if countdown_on is not None:
            states.append({"key": "acCountdownOn", "value": countdown_on})
        if countdown_off is not None:
            states.append({"key": "acCountdownOff", "value": countdown_off})
        if set_temp is not None:
            states.append({"key": "setpointCool", "value": set_temp, "uiValue": f"{set_temp:.1f} °"})
            states.append({"key": "setpointHeat", "value": set_temp, "uiValue": f"{set_temp:.1f} °"})
        # Indigo's native thermostat device list State column always shows
        # temperatureInput1, ignoring UiDisplayStateId - stateColumnTemp picks
        # what that state actually holds: the selected/target temp, or the
        # measured temp from myZone's sensor. No fallback between the two -
        # if "measured" is chosen and myZone isn't reporting a reading, this
        # writes an explicit 0 rather than leaving whatever was last written
        # (which would otherwise freeze on a stale setpoint from before the
        # setting was switched, making the toggle look like it does nothing).
        show_measured = dev.pluginProps.get("stateColumnTemp", "setpoint") == "measured"
        if show_measured:
            display_temp = measured_temp if measured_temp is not None else 0
        else:
            display_temp = set_temp
        if display_temp is not None:
            states.append({"key": "temperatureInput1", "value": display_temp, "uiValue": f"{display_temp:.1f} °"})
        dev.updateStatesOnServer(states)
        dev.setErrorStateOnServer(None)

        if NEW_DEVICE_NAME_RE.match(dev.name.strip()):
            # Skip creating child devices while this still has Indigo's
            # placeholder name - they'd get named after it permanently.
            # The next regular poll (after it's renamed) will pick this back up.
            self.logger.debug(f"Deferring child device creation for \"{dev.name}\" until it's renamed")
        else:
            self._discover_zones(dev, zones)
            self._discover_mode_buttons(dev)
            self._discover_fan_controls(dev)
        self._poll_zones(dev, zones)
        self._sync_mode_buttons(dev, mode, state)
        self._sync_fan_controls(dev, fan)

    def _discover_fan_controls(self, system_dev: indigo.Device) -> None:
        """Create the fan speed control and Fan Auto button for a system if missing.

        Args:
            system_dev (indigo.Device): The AC System device to create controls for.
        """
        has_speed_control = any(
            d.pluginProps.get("systemDevice") == str(system_dev.id)
            for d in indigo.devices.iter("self.fanSpeedControl")
        )
        if not has_speed_control:
            new_dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                deviceTypeId="fanSpeedControl",
                name=f"{system_dev.name} - Fan Speed",
                pluginId=self.pluginId,
                props={"systemDevice": str(system_dev.id)},
            )
            new_dev.updateStateOnServer("onOffState", True)
            self.logger.info(f"Created fan speed control: {new_dev.name}")

        has_auto_button = any(
            d.pluginProps.get("systemDevice") == str(system_dev.id)
            for d in indigo.devices.iter("self.fanAutoButton")
        )
        if not has_auto_button:
            new_dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                deviceTypeId="fanAutoButton",
                name=f"{system_dev.name} - Fan Auto",
                pluginId=self.pluginId,
                props={"systemDevice": str(system_dev.id)},
            )
            new_dev.updateStateOnServer("onOffState", False)
            self.logger.info(f"Created fan auto button: {new_dev.name}")

    def _sync_fan_controls(self, system_dev: indigo.Device, fan: str) -> None:
        """Reflect the AC's real fan speed onto the speed control and Fan Auto button.

        Args:
            system_dev (indigo.Device): The AC System device whose fan controls to sync.
            fan (str): The controller's current fan speed value (e.g. ``"low"``, ``"autoAA"``).
        """
        speed_index = FAN_SPEED_TO_INDEX.get(fan, 0)
        for d in indigo.devices.iter("self.fanSpeedControl"):
            if not d.enabled:
                continue
            if d.pluginProps.get("systemDevice") != str(system_dev.id):
                continue
            if d.states.get("speedIndex") != speed_index:
                self.logger.debug(f"{d.name}: speedIndex {d.states.get('speedIndex')} -> {speed_index}")
                d.updateStateOnServer("speedIndex", speed_index)
        is_auto = fan in FAN_AUTO_VALUES
        for d in indigo.devices.iter("self.fanAutoButton"):
            if not d.enabled:
                continue
            if d.pluginProps.get("systemDevice") != str(system_dev.id):
                continue
            if d.states.get("onOffState") != is_auto:
                self.logger.debug(f"{d.name}: onOffState {d.states.get('onOffState')} -> {is_auto}")
                d.updateStateOnServer("onOffState", is_auto)

    def _discover_mode_buttons(self, system_dev: indigo.Device) -> None:
        """Create the Vent/Dry mode-button child devices for a system if missing.

        Args:
            system_dev (indigo.Device): The AC System device to create mode buttons for.
        """
        existing = {
            md.pluginProps.get("mode")
            for md in indigo.devices.iter("self.modeButton")
            if md.pluginProps.get("systemDevice") == str(system_dev.id)
        }
        for mode_value, label in MODE_BUTTON_LABELS.items():
            if mode_value in existing:
                continue
            new_dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                deviceTypeId="modeButton",
                name=f"{system_dev.name} - {label}",
                pluginId=self.pluginId,
                props={"systemDevice": str(system_dev.id), "mode": mode_value},
            )
            new_dev.updateStateOnServer("onOffState", False)
            self.logger.info(f"Created mode button: {new_dev.name}")

    def _sync_mode_buttons(self, system_dev: indigo.Device, mode: str, state: str) -> None:
        """Reflect the AC's real mode onto the Vent/Dry button devices' on/off state.

        Args:
            system_dev (indigo.Device): The AC System device whose mode buttons to sync.
            mode (str): The controller's current mode (e.g. ``"vent"``, ``"dry"``, ``"cool"``).
            state (str): The controller's current power state (``"on"`` or ``"off"``).
        """
        active_mode = mode if state == "on" else None
        for md in indigo.devices.iter("self.modeButton"):
            if not md.enabled:
                continue
            if md.pluginProps.get("systemDevice") != str(system_dev.id):
                continue
            is_on = md.pluginProps.get("mode") == active_mode
            if md.states.get("onOffState") != is_on:
                self.logger.debug(f"{md.name}: onOffState {md.states.get('onOffState')} -> {is_on}")
                md.updateStateOnServer("onOffState", is_on)

    def _discover_zones(self, system_dev: indigo.Device, zones: dict) -> None:
        """Create a Zone child device for any zone reported by the controller that
        doesn't already have one, using the name/number MyAir itself reports.

        Args:
            system_dev (indigo.Device): The AC System device the zones belong to.
            zones (dict): The controller's current ``zones`` mapping (e.g. ``{"z01": {...}}``).
        """
        existing = {
            zd.pluginProps.get("zoneNumber")
            for zd in indigo.devices.iter("self.zoneDevice")
            if zd.pluginProps.get("systemDevice") == str(system_dev.id)
        }
        for zone_key, zone in zones.items():
            try:
                zone_number = str(int(zone_key.lstrip("z")))
            except ValueError:
                continue
            if zone_number in existing:
                continue
            zone_name = zone.get("name") or f"Zone {zone_number}"
            new_dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                deviceTypeId="zoneDevice",
                name=f"{system_dev.name} - {zone_name}",
                pluginId=self.pluginId,
                props={"systemDevice": str(system_dev.id), "zoneNumber": zone_number},
            )
            new_dev.updateStatesOnServer([
                {"key": "onOffState", "value": zone.get("state") == "open"},
                {"key": "brightnessLevel", "value": zone.get("value", 0)},
            ])
            self.logger.info(f"Discovered MyAir zone: {new_dev.name}")

    def _poll_zones(self, system_dev: indigo.Device, zones: dict) -> None:
        """Write each of a system's zone devices' current state from the controller's response.

        Flags a zone device with an Indigo error state if the controller stops
        reporting it (e.g. removed on the MyAir side), rather than letting it
        go stale silently.

        Args:
            system_dev (indigo.Device): The AC System device the zones belong to.
            zones (dict): The controller's current ``zones`` mapping (e.g. ``{"z01": {...}}``).
        """
        for zone_dev in indigo.devices.iter("self.zoneDevice"):
            if not zone_dev.enabled:
                continue
            if zone_dev.pluginProps.get("systemDevice") != str(system_dev.id):
                continue
            zone_number = zone_dev.pluginProps.get("zoneNumber", "")
            zone = zones.get(f"z{int(zone_number):02d}") if zone_number else None
            if not zone:
                if not zone_dev.errorState:
                    self.logger.warning(f"Zone {zone_dev.name} (zone {zone_number}) is no longer reported by {system_dev.name}'s controller")
                    zone_dev.setErrorStateOnServer("No longer reported by controller")
                continue
            if zone_dev.errorState:
                self.logger.info(f"Zone {zone_dev.name} is being reported again")
                zone_dev.setErrorStateOnServer(None)
            is_open = zone.get("state") == "open"
            damper = zone.get("value", 0)
            measured = zone.get("measuredTemp")
            set_temp = zone.get("setTemp")
            signal = zone.get("rssi")
            z_states = [
                {"key": "onOffState", "value": is_open},
                {"key": "brightnessLevel", "value": damper if is_open else 0},
            ]
            if measured is not None:
                z_states.append({"key": "zoneTemp", "value": measured})
            if set_temp is not None:
                z_states.append({"key": "zoneSetTemp", "value": set_temp})
            if signal is not None:
                z_states.append({"key": "zoneSignalStrength", "value": signal})
            self.logger.debug(
                f"Zone {zone_dev.name}: open={is_open} damper={damper} measured={measured} "
                f"setTemp={set_temp} signal={signal}"
            )
            zone_dev.updateStatesOnServer(z_states)

    def _get_system_device(self, child_dev: indigo.Device) -> Optional[indigo.Device]:
        """Resolve a zone/mode-button/fan-control child device back to its parent AC System.

        Args:
            child_dev (indigo.Device): The child device whose ``systemDevice`` prop to resolve.

        Returns:
            Optional[indigo.Device]: The parent AC System device, or None if unset/missing.
        """
        system_id = child_dev.pluginProps.get("systemDevice", "")
        if system_id and int(system_id) in indigo.devices:
            return indigo.devices[int(system_id)]
        return None

    # ------------------------------------------------------------------
    # AC System (thermostat) actions
    # ------------------------------------------------------------------

    def actionControlThermostat(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Handle a native thermostat action (mode, setpoints, request status) on an AC System.

        Args:
            action (indigo.PluginAction): The thermostat action to perform.
            dev (indigo.Device): The AC System device the action applies to.
        """
        self.logger.debug(
            f"actionControlThermostat: {dev.name} action={action.thermostatAction} "
            f"actionValue={getattr(action, 'actionValue', None)} actionMode={getattr(action, 'actionMode', None)}"
        )
        ip = dev.pluginProps.get("address", "")
        if not ip:
            self.logger.error(f"No IP address specified for {dev.name}")
            return
        ta = indigo.kThermostatAction
        if action.thermostatAction == ta.SetHvacMode:
            self._set_hvac_mode(dev, ip, action.actionMode)
        elif action.thermostatAction in (ta.SetCoolSetpoint, ta.SetHeatSetpoint):
            self._set_temp(dev, ip, action.actionValue)
        elif action.thermostatAction == ta.DecreaseCoolSetpoint:
            self._set_temp(dev, ip, dev.coolSetpoint - action.actionValue)
        elif action.thermostatAction == ta.IncreaseCoolSetpoint:
            self._set_temp(dev, ip, dev.coolSetpoint + action.actionValue)
        elif action.thermostatAction == ta.DecreaseHeatSetpoint:
            self._set_temp(dev, ip, dev.heatSetpoint - action.actionValue)
        elif action.thermostatAction == ta.IncreaseHeatSetpoint:
            self._set_temp(dev, ip, dev.heatSetpoint + action.actionValue)
        elif action.thermostatAction in (ta.RequestStatusAll, ta.RequestMode, ta.RequestTemperatures, ta.RequestSetpoints):
            self._poll_system(dev)

    def _set_hvac_mode(self, dev: indigo.Device, ip: str, hvac_mode: int) -> None:
        """Send a native Indigo HVAC mode (Cool/Heat/Off) to the controller.

        Args:
            dev (indigo.Device): The AC System device.
            ip (str): The controller's local IP address.
            hvac_mode (int): One of the ``indigo.kHvacMode`` constants.
        """
        mode_str = HVAC_MODE_TO_MYAIR.get(hvac_mode)
        if mode_str is None and hvac_mode != indigo.kHvacMode.Off:
            self.logger.error(f"Unsupported HVAC mode for {dev.name} - use the Set AC Mode action for Vent/Dry")
            return
        payload = {"ac1": {"info": {"state": "off" if hvac_mode == indigo.kHvacMode.Off else "on"}}}
        if mode_str:
            payload["ac1"]["info"]["mode"] = mode_str
        if self._send_command(ip, payload):
            self.logger.info(f"{dev.name}: set mode to {mode_str or 'off'}")
            self._poll_system(dev)

    def _set_temp(self, dev: indigo.Device, ip: str, new_setpoint: float) -> None:
        """Send a new setpoint temperature to the controller.

        Args:
            dev (indigo.Device): The AC System device.
            ip (str): The controller's local IP address.
            new_setpoint (float): The target temperature.
        """
        if self._send_command(ip, {"ac1": {"info": {"setTemp": round(new_setpoint, 1)}}}):
            self.logger.info(f"{dev.name}: set temperature to {new_setpoint:.1f}°")
            self._poll_system(dev)

    def setAcMode(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Custom action: set Cool/Heat/Vent/Dry (Vent and Dry aren't reachable via the native thermostat mode control).

        Args:
            action (indigo.PluginAction): The triggering Indigo action, carrying the ``mode`` prop.
            dev (indigo.Device): The AC System device the action applies to.
        """
        ip = dev.pluginProps.get("address", "")
        mode = action.props.get("mode", "cool")
        self.logger.debug(f"setAcMode: {dev.name} mode={mode}")
        if not ip:
            self.logger.error(f"No IP address specified for {dev.name}")
            return
        if self._send_command(ip, {"ac1": {"info": {"state": "on", "mode": mode}}}):
            self.logger.info(f"{dev.name}: set mode to {mode}")
            self._poll_system(dev)

    def setFanSpeed(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Custom action: set the AC's fan speed (Low/Medium/High/Auto).

        Args:
            action (indigo.PluginAction): The triggering Indigo action, carrying the ``fanSpeed`` prop.
            dev (indigo.Device): The AC System device the action applies to.
        """
        ip = dev.pluginProps.get("address", "")
        fan = action.props.get("fanSpeed", "autoAA")
        self.logger.debug(f"setFanSpeed: {dev.name} fan={fan}")
        if not ip:
            self.logger.error(f"No IP address specified for {dev.name}")
            return
        if self._send_command(ip, {"ac1": {"info": {"fan": fan}}}):
            self.logger.info(f"{dev.name}: set fan speed to {fan}")
            self._poll_system(dev)

    # ------------------------------------------------------------------
    # Zone (dimmer) actions
    # ------------------------------------------------------------------

    def actionControlDimmerRelay(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Handle a native dimmer/relay action, dispatching by device type.

        Shared entry point for zone, mode-button, and fan-auto-button devices
        (all use the dimmer/relay action namespace) - mode-button and
        fan-auto-button are delegated to their own handlers; only zone devices
        are handled directly here.

        Args:
            action (indigo.PluginAction): The dimmer/relay action to perform.
            dev (indigo.Device): The device the action applies to.
        """
        self.logger.debug(f"actionControlDimmerRelay: {dev.name} ({dev.deviceTypeId}) action={action.deviceAction} value={getattr(action, 'actionValue', None)}")
        if dev.deviceTypeId == "modeButton":
            self._control_mode_button(action, dev)
            return
        if dev.deviceTypeId == "fanAutoButton":
            self._control_fan_auto_button(action, dev)
            return
        if dev.deviceTypeId != "zoneDevice":
            return
        system_dev = self._get_system_device(dev)
        if not system_dev:
            self.logger.error(f"No AC System configured for {dev.name}")
            return
        ip = system_dev.pluginProps.get("address", "")
        zone_number = dev.pluginProps.get("zoneNumber", "")
        if not ip or not zone_number:
            self.logger.error(f"Zone {dev.name} is not fully configured")
            return
        zone_key = f"z{int(zone_number):02d}"

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            self._send_command(ip, {"ac1": {"zones": {zone_key: {"state": "open"}}}})
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            self._send_command(ip, {"ac1": {"zones": {zone_key: {"state": "close"}}}})
        elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
            self._send_command(ip, {"ac1": {"zones": {zone_key: {"state": "open", "value": action.actionValue}}}})
        elif action.deviceAction == indigo.kDeviceAction.BrightenBy:
            new_value = min(100, dev.states.get("brightnessLevel", 0) + action.actionValue)
            self._send_command(ip, {"ac1": {"zones": {zone_key: {"state": "open", "value": new_value}}}})
        elif action.deviceAction == indigo.kDeviceAction.DimBy:
            new_value = max(0, dev.states.get("brightnessLevel", 0) - action.actionValue)
            state = "open" if new_value > 0 else "close"
            self._send_command(ip, {"ac1": {"zones": {zone_key: {"state": state, "value": new_value}}}})
        elif action.deviceAction == indigo.kDeviceAction.RequestStatus:
            self._poll_system(system_dev)
            return
        else:
            return

        if str(dev.pluginProps.get("logChanges", True)).lower() == "true":
            self.logger.info(f"Zone {dev.name}: {action.deviceAction}")
        self._poll_system(system_dev)

    def setZoneTemp(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Custom action: set a zone's own target temperature.

        Only has any effect on zones with their own temperature sensor - MyAir
        silently ignores it on zones that don't, so there's nothing to
        detect/guard here.

        Args:
            action (indigo.PluginAction): The triggering Indigo action, carrying the ``temperature`` prop.
            dev (indigo.Device): The zone device the action applies to.
        """
        system_dev = self._get_system_device(dev)
        if not system_dev:
            self.logger.error(f"No AC System configured for {dev.name}")
            return
        ip = system_dev.pluginProps.get("address", "")
        zone_number = dev.pluginProps.get("zoneNumber", "")
        if not ip or not zone_number:
            self.logger.error(f"Zone {dev.name} is not fully configured")
            return
        try:
            temperature = float(action.props.get("temperature", ""))
        except (TypeError, ValueError):
            self.logger.error(f"Set zone temperature for {dev.name}: invalid temperature value")
            return
        self.logger.debug(f"setZoneTemp: {dev.name} temperature={temperature}")
        zone_key = f"z{int(zone_number):02d}"
        if self._send_command(ip, {"ac1": {"zones": {zone_key: {"setTemp": round(temperature, 1)}}}}):
            self.logger.info(f"{dev.name}: set zone temperature to {temperature:.1f}°")
            self._poll_system(system_dev)

    def _control_mode_button(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Handle On/Off/Request Status for a Vent/Dry mode-button device.

        Turning a mode button off always means "turn the AC off entirely" -
        there's no other sensible meaning for switching a mode "off".

        Args:
            action (indigo.PluginAction): The relay action to perform.
            dev (indigo.Device): The mode-button device the action applies to.
        """
        system_dev = self._get_system_device(dev)
        if not system_dev:
            self.logger.error(f"No AC System configured for {dev.name}")
            return
        ip = system_dev.pluginProps.get("address", "")
        mode = dev.pluginProps.get("mode", "")
        if not ip or not mode:
            self.logger.error(f"{dev.name} is not fully configured")
            return

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            if self._send_command(ip, {"ac1": {"info": {"state": "on", "mode": mode}}}):
                self.logger.info(f"{dev.name}: mode set to {mode}")
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            # Turning off a mode button always means "turn the AC off" - there's
            # no other sensible meaning for switching a mode "off".
            if self._send_command(ip, {"ac1": {"info": {"state": "off"}}}):
                self.logger.info(f"{dev.name}: turned AC off")
        elif action.deviceAction != indigo.kDeviceAction.RequestStatus:
            return
        self._poll_system(system_dev)

    def _control_fan_auto_button(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Handle On/Off/Request Status for the Fan Auto button device.

        There's no sensible "turn auto off" - the fan speed is left as-is and
        the user is pointed at the Fan Speed device instead.

        Args:
            action (indigo.PluginAction): The relay action to perform.
            dev (indigo.Device): The fan-auto-button device the action applies to.
        """
        system_dev = self._get_system_device(dev)
        if not system_dev:
            self.logger.error(f"No AC System configured for {dev.name}")
            return
        ip = system_dev.pluginProps.get("address", "")
        if not ip:
            self.logger.error(f"No IP address specified for {dev.name}")
            return

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            if self._send_command(ip, {"ac1": {"info": {"fan": "autoAA"}}}):
                self.logger.info(f"{dev.name}: fan speed set to Auto")
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            # No sensible "turn auto off" - pick a speed on the Fan Speed
            # device instead. Leave the fan speed as-is.
            self.logger.info(f"{dev.name}: pick Low/Medium/High on the Fan Speed device instead of turning Auto off")
            return
        elif action.deviceAction != indigo.kDeviceAction.RequestStatus:
            return
        self._poll_system(system_dev)

    # ------------------------------------------------------------------
    # Fan speed (speedcontrol) actions
    # ------------------------------------------------------------------

    def actionControlSpeedControl(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Handle a native speed-control action on the Fan Speed device.

        MyAir has no fan-off or continuous-level concept, only discrete
        Low/Medium/High/Auto, so TurnOn/TurnOff/Toggle/SetSpeedLevel are not
        meaningful and just log a pointer to the right device/action instead.

        Args:
            action (indigo.PluginAction): The speed-control action to perform.
            dev (indigo.Device): The fan-speed-control device the action applies to.
        """
        self.logger.debug(f"actionControlSpeedControl: {dev.name} action={action.speedControlAction} value={getattr(action, 'actionValue', None)}")
        if dev.deviceTypeId != "fanSpeedControl":
            return
        system_dev = self._get_system_device(dev)
        if not system_dev:
            self.logger.error(f"No AC System configured for {dev.name}")
            return
        ip = system_dev.pluginProps.get("address", "")
        if not ip:
            self.logger.error(f"No IP address specified for {dev.name}")
            return

        sc = indigo.kSpeedControlAction
        if action.speedControlAction == sc.SetSpeedIndex:
            self._set_fan_speed_index(dev, system_dev, ip, action.actionValue)
        elif action.speedControlAction == sc.IncreaseSpeedIndex:
            new_index = min(3, dev.states.get("speedIndex", 0) + action.actionValue)
            self._set_fan_speed_index(dev, system_dev, ip, new_index)
        elif action.speedControlAction == sc.DecreaseSpeedIndex:
            new_index = max(0, dev.states.get("speedIndex", 0) - action.actionValue)
            self._set_fan_speed_index(dev, system_dev, ip, new_index)
        elif action.speedControlAction == sc.RequestStatus:
            self._poll_system(system_dev)
        else:
            # TurnOn/TurnOff/Toggle/SetSpeedLevel: MyAir has no fan-off or
            # continuous-level concept, only discrete Low/Medium/High/Auto.
            self.logger.info(f"{dev.name}: use Low/Medium/High (this device) or the Fan Auto device for automatic speed")

    def _set_fan_speed_index(self, dev: indigo.Device, system_dev: indigo.Device, ip: str, speed_index: int) -> None:
        """Translate a speedcontrol index (1/2/3) to MyAir's fan value and send it.

        Args:
            dev (indigo.Device): The fan-speed-control device, used only for log messages.
            system_dev (indigo.Device): The parent AC System device to command/re-poll.
            ip (str): The controller's local IP address.
            speed_index (int): The target speedcontrol index (1=Low, 2=Medium, 3=High).
        """
        fan_value = FAN_INDEX_TO_SPEED.get(speed_index)
        if fan_value is None:
            self.logger.info(f"{dev.name}: index {speed_index} has no MyAir equivalent - use the Fan Auto device for automatic speed")
            return
        if self._send_command(ip, {"ac1": {"info": {"fan": fan_value}}}):
            self.logger.info(f"{dev.name}: set fan speed to {fan_value}")
            self._poll_system(system_dev)
