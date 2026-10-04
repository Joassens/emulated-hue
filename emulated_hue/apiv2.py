"""Support for the Hue CLIP API v2 to control Home Assistant."""
# https://developers.meethue.com/develop/hue-api-v2/
import asyncio
import contextlib
import datetime
import functools
import json
import logging
import uuid
from typing import Any

import tzlocal
from aiohttp import web

from emulated_hue import const
from emulated_hue.controllers import Controller
from emulated_hue.controllers.devices import (
    BrightnessDevice,
    CTDevice,
    OnOffDevice,
    RGBDevice,
    RGBWWDevice,
    async_get_device,
)
from emulated_hue.utils import ClassRouteTableDef, clamp

LOGGER = logging.getLogger(__name__)

HEADER_APP_KEY = "hue-application-key"
EVENTSTREAM_KEEPALIVE_SECONDS = 10

RTYPE_BRIDGE = "bridge"
RTYPE_BRIDGE_HOME = "bridge_home"
RTYPE_DEVICE = "device"
RTYPE_LIGHT = "light"
RTYPE_GROUPED_LIGHT = "grouped_light"
RTYPE_ROOM = "room"
RTYPE_ZONE = "zone"
RTYPE_ZIGBEE_CONNECTIVITY = "zigbee_connectivity"

# keys of a light resource which are sent in eventstream updates
LIGHT_STATE_KEYS = ("on", "dimming", "color_temperature", "color")

# pylint: disable=invalid-name
routes = ClassRouteTableDef()
# pylint: enable=invalid-name


def send_v2_response(
    data: list | None = None, errors: list | None = None, status: int = 200
) -> web.Response:
    """Send a CLIP v2 formatted response."""
    return web.Response(
        text=json.dumps(
            {"errors": errors or [], "data": data or []},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        status=status,
        content_type="application/json",
        headers={"server": "nginx", "Access-Control-Allow-Origin": "*"},
    )


def send_v2_error(description: str, status: int) -> web.Response:
    """Send a CLIP v2 formatted error."""
    return send_v2_response(errors=[{"description": description}], status=status)


def check_v2_request(func):
    """Validate the application key and unpack the json body (used as decorator)."""

    @functools.wraps(func)
    async def wrapped_func(cls: "HueApiV2Endpoints", request: web.Request):
        LOGGER.debug("[%s] %s %s", request.remote, request.method, request.path)
        app_key = request.headers.get(HEADER_APP_KEY)
        if not app_key or not await cls.ctl.config_instance.async_get_user(app_key):
            LOGGER.debug("[%s] Invalid application key", request.remote)
            return send_v2_error("unauthorized user", 403)
        if request.method in ["PUT", "POST"]:
            request_text = await request.text()
            try:
                request_data = json.loads(request_text.rstrip("\x00") or "{}")
            except ValueError:
                LOGGER.warning(
                    "Invalid json in request: %s --> %s", request, request_text
                )
                return send_v2_error("body contains invalid json", 400)
            LOGGER.debug(request_text)
            return await func(cls, request, request_data)
        return await func(cls, request)

    return wrapped_func


def rref(rid: str, rtype: str) -> dict:
    """Return a resource identifier."""
    return {"rid": rid, "rtype": rtype}


class HueApiV2Endpoints:
    """Hue CLIP API v2 endpoints."""

    def __init__(self, ctl: Controller):
        """Initialize the v2 api."""
        self.ctl = ctl
        self._namespace = uuid.uuid5(
            uuid.NAMESPACE_URL, f"emulated-hue:{ctl.config_instance.bridge_id}"
        )
        self._event_queues: set[asyncio.Queue] = set()
        self._last_light_states: dict[str, dict] = {}
        self._remove_state_listener = None

    def get_routes(self) -> ClassRouteTableDef:
        """Return routes (bound to this instance) for external access."""
        table = ClassRouteTableDef()
        table.add_class_routes(self)
        return table

    async def async_setup(self):
        """Start listening for Home Assistant state changes (eventstream)."""
        self._remove_state_listener = self.ctl.controller_hass.register_event_callback(
            self._async_on_state_changed, event_filter="state_changed"
        )

    async def async_stop(self):
        """Stop the v2 api."""
        if self._remove_state_listener:
            self._remove_state_listener()
        for queue in self._event_queues:
            queue.put_nowait(None)

    def rid(self, rtype: str, key: str = "") -> str:
        """Return a stable resource id for a (resource type, local key) combination."""
        return str(uuid.uuid5(self._namespace, f"{rtype}:{key}"))

    # ---------------------------------------------------------------------
    # Endpoints
    # ---------------------------------------------------------------------

    @routes.get("/clip/v2/resource")
    @check_v2_request
    async def async_get_all_resources(self, request: web.Request):
        """Return all resources."""
        return send_v2_response(await self.async_get_resources())

    @routes.get("/clip/v2/resource/{rtype}")
    @check_v2_request
    async def async_get_resources_by_type(self, request: web.Request):
        """Return all resources of a given type."""
        rtype = request.match_info["rtype"]
        return send_v2_response(await self.async_get_resources(rtype))

    @routes.get("/clip/v2/resource/{rtype}/{rid}")
    @check_v2_request
    async def async_get_resource(self, request: web.Request):
        """Return a single resource."""
        rtype = request.match_info["rtype"]
        rid = request.match_info["rid"]
        for resource in await self.async_get_resources(rtype):
            if resource["id"] == rid:
                return send_v2_response([resource])
        return send_v2_error("Not Found", 404)

    @routes.put("/clip/v2/resource/{rtype}/{rid}")
    @check_v2_request
    async def async_put_resource(self, request: web.Request, request_data: dict):
        """Update a single resource (e.g. control a light)."""
        rtype = request.match_info["rtype"]
        rid = request.match_info["rid"]
        if rtype == RTYPE_LIGHT:
            entity_ids = [
                entity_id
                for entity_id in await self._async_get_enabled_entities()
                if self.rid(RTYPE_LIGHT, entity_id) == rid
            ]
        elif rtype == RTYPE_GROUPED_LIGHT:
            entity_ids = None
            for group in await self._async_get_groups():
                if self.rid(RTYPE_GROUPED_LIGHT, group["id"]) == rid:
                    entity_ids = group["entities"]
                    break
            if rid == self.rid(RTYPE_GROUPED_LIGHT, "0"):
                entity_ids = await self._async_get_enabled_entities()
        elif rtype in (RTYPE_DEVICE, RTYPE_ROOM, RTYPE_ZONE):
            return await self._async_update_metadata(rtype, rid, request_data)
        else:
            return send_v2_error(f"method, PUT, not available for {rtype}", 405)

        if not entity_ids:
            return send_v2_error("Not Found", 404)
        await asyncio.gather(
            *(self._async_light_action(e, request_data) for e in entity_ids)
        )
        return send_v2_response([rref(rid, rtype)])

    @routes.get("/eventstream/clip/v2")
    @check_v2_request
    async def async_eventstream(self, request: web.Request):
        """Stream resource changes as server-sent events."""
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        await response.write(b": hi\n\n")
        queue: asyncio.Queue = asyncio.Queue()
        self._event_queues.add(queue)
        LOGGER.debug("[%s] Eventstream client connected", request.remote)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), EVENTSTREAM_KEEPALIVE_SECONDS
                    )
                except asyncio.TimeoutError:
                    await response.write(b": hi\n\n")
                    continue
                if event is None:
                    break
                await response.write(event)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._event_queues.discard(queue)
            LOGGER.debug("[%s] Eventstream client disconnected", request.remote)
        return response

    # ---------------------------------------------------------------------
    # Resource building
    # ---------------------------------------------------------------------

    async def async_get_resources(self, rtype: str | None = None) -> list[dict]:
        """Build the (filtered) list of v2 resources."""
        resources: list[dict] = []
        bridge_device_id = self.rid(RTYPE_DEVICE, "bridge")
        light_device_ids: dict[str, str] = {}

        # bridge + bridge device
        resources.append(self._bridge_resource(bridge_device_id))
        resources.append(self._bridge_device_resource(bridge_device_id))

        # lights, light devices and connectivity
        all_entities = await self._async_get_enabled_entities()
        for entity_id in all_entities:
            device = await async_get_device(self.ctl, entity_id)
            device_rid = self.rid(RTYPE_DEVICE, entity_id)
            light_device_ids[entity_id] = device_rid
            resources.append(self._light_device_resource(device, device_rid))
            resources.append(self._light_resource(device, device_rid))
            resources.append(self._zigbee_resource(device, device_rid))

        # rooms/zones and their grouped lights
        devices_in_rooms: set[str] = set()
        for group in await self._async_get_groups():
            group_rtype = RTYPE_ZONE if group["type"] == "Zone" else RTYPE_ROOM
            group_rid = self.rid(group_rtype, group["id"])
            if group_rtype == RTYPE_ROOM:
                children = [
                    rref(light_device_ids[e], RTYPE_DEVICE)
                    for e in group["entities"]
                    if e in light_device_ids
                ]
                devices_in_rooms.update(group["entities"])
            else:
                children = [
                    rref(self.rid(RTYPE_LIGHT, e), RTYPE_LIGHT)
                    for e in group["entities"]
                    if e in light_device_ids
                ]
            grouped_light_rid = self.rid(RTYPE_GROUPED_LIGHT, group["id"])
            resources.append(
                {
                    "id": group_rid,
                    "id_v1": f"/groups/{group['id']}",
                    "children": children,
                    "services": [rref(grouped_light_rid, RTYPE_GROUPED_LIGHT)],
                    "metadata": {
                        "name": group["name"],
                        "archetype": hue_room_archetype(group.get("class")),
                    },
                    "type": group_rtype,
                }
            )
            resources.append(
                await self._async_grouped_light_resource(
                    grouped_light_rid,
                    group["id"],
                    rref(group_rid, group_rtype),
                    group["entities"],
                )
            )

        # bridge home (group 0) contains all rooms and devices without a room
        bridge_home_rid = self.rid(RTYPE_BRIDGE_HOME)
        home_grouped_light_rid = self.rid(RTYPE_GROUPED_LIGHT, "0")
        resources.append(
            {
                "id": bridge_home_rid,
                "id_v1": "/groups/0",
                "children": [
                    rref(r["id"], RTYPE_ROOM)
                    for r in resources
                    if r["type"] == RTYPE_ROOM
                ]
                + [rref(bridge_device_id, RTYPE_DEVICE)]
                + [
                    rref(device_rid, RTYPE_DEVICE)
                    for entity_id, device_rid in light_device_ids.items()
                    if entity_id not in devices_in_rooms
                ],
                "services": [rref(home_grouped_light_rid, RTYPE_GROUPED_LIGHT)],
                "type": RTYPE_BRIDGE_HOME,
            }
        )
        resources.append(
            await self._async_grouped_light_resource(
                home_grouped_light_rid,
                "0",
                rref(bridge_home_rid, RTYPE_BRIDGE_HOME),
                all_entities,
            )
        )

        if rtype:
            return [r for r in resources if r["type"] == rtype]
        return resources

    def _bridge_resource(self, bridge_device_id: str) -> dict:
        config = self.ctl.config_instance
        return {
            "id": self.rid(RTYPE_BRIDGE),
            "id_v1": "",
            "owner": rref(bridge_device_id, RTYPE_DEVICE),
            "bridge_id": config.bridge_id.lower(),
            "time_zone": {
                "time_zone": config.get_storage_value(
                    "bridge_config", "timezone", tzlocal.get_localzone_name()
                )
            },
            "type": RTYPE_BRIDGE,
        }

    def _bridge_device_resource(self, bridge_device_id: str) -> dict:
        config = self.ctl.config_instance
        basic = config.definitions["bridge"]["basic"]
        return {
            "id": bridge_device_id,
            "id_v1": "",
            "product_data": {
                "model_id": basic["modelid"],
                "manufacturer_name": "Signify Netherlands B.V.",
                "product_name": "Hue Bridge",
                "product_archetype": "bridge_v2",
                "certified": True,
                "software_version": swversion_to_v2(basic["swversion"]),
            },
            "metadata": {"name": config.bridge_name, "archetype": "bridge_v2"},
            "identify": {},
            "services": [rref(self.rid(RTYPE_BRIDGE), RTYPE_BRIDGE)],
            "type": RTYPE_DEVICE,
        }

    def _light_definition(self, device: OnOffDevice) -> dict:
        """Return the v1 light definition (model, gamut etc.) for a device."""
        lights = self.ctl.config_instance.definitions["lights"]
        if isinstance(device, RGBWWDevice):
            return lights["Extended color light"]
        if isinstance(device, RGBDevice):
            return lights["Color light"]
        if isinstance(device, CTDevice):
            return lights["Color temperature light"]
        if isinstance(device, BrightnessDevice):
            return lights["Dimmable light"]
        return lights["On/off light"]

    def _light_device_resource(self, device: OnOffDevice, device_rid: str) -> dict:
        definition = self._light_definition(device)
        props = device.device_properties
        return {
            "id": device_rid,
            "id_v1": f"/lights/{device.light_id}",
            "product_data": {
                "model_id": props.model or definition["modelid"],
                "manufacturer_name": props.manufacturer
                or definition["manufacturername"],
                "product_name": props.name or definition["productname"],
                "product_archetype": "sultan_bulb",
                "certified": True,
                "software_version": props.sw_version or definition["swversion"],
            },
            "metadata": {"name": device.name, "archetype": "sultan_bulb"},
            "identify": {},
            "services": [
                rref(self.rid(RTYPE_LIGHT, device.entity_id), RTYPE_LIGHT),
                rref(
                    self.rid(RTYPE_ZIGBEE_CONNECTIVITY, device.entity_id),
                    RTYPE_ZIGBEE_CONNECTIVITY,
                ),
            ],
            "type": RTYPE_DEVICE,
        }

    def _zigbee_resource(self, device: OnOffDevice, device_rid: str) -> dict:
        return {
            "id": self.rid(RTYPE_ZIGBEE_CONNECTIVITY, device.entity_id),
            "id_v1": f"/lights/{device.light_id}",
            "owner": rref(device_rid, RTYPE_DEVICE),
            "status": "connected" if device.reachable else "connectivity_issue",
            "mac_address": device.unique_id.split("-")[0][-23:],
            "type": RTYPE_ZIGBEE_CONNECTIVITY,
        }

    def _light_resource(self, device: OnOffDevice, device_rid: str) -> dict:
        definition = self._light_definition(device)
        light = {
            "id": self.rid(RTYPE_LIGHT, device.entity_id),
            "id_v1": f"/lights/{device.light_id}",
            "owner": rref(device_rid, RTYPE_DEVICE),
            "metadata": {"name": device.name, "archetype": "sultan_bulb"},
            "identify": {},
            "on": {"on": device.power_state},
            "mode": "normal",
            "type": RTYPE_LIGHT,
        }
        light.update(self._light_state(device))
        if isinstance(device, BrightnessDevice):
            min_dim = definition["capabilities"]["control"].get("mindimlevel", 1000)
            light["dimming"]["min_dim_level"] = round(min_dim / 100, 2)
            light["alert"] = {"action_values": ["breathe"]}
            light["dynamics"] = {
                "status": "none",
                "status_values": ["none"],
                "speed": 0.0,
                "speed_valid": False,
            }
        if isinstance(device, CTDevice):
            light["color_temperature"]["mirek_schema"] = {
                "mirek_minimum": device.min_mireds or const.HUE_ATTR_CT_MIN,
                "mirek_maximum": device.max_mireds or const.HUE_ATTR_CT_MAX,
            }
        if isinstance(device, RGBDevice):
            control = definition["capabilities"]["control"]
            gamut = control.get("colorgamut")
            light["color"]["gamut_type"] = control.get("colorgamuttype", "other")
            if gamut:
                light["color"]["gamut"] = {
                    "red": {"x": gamut[0][0], "y": gamut[0][1]},
                    "green": {"x": gamut[1][0], "y": gamut[1][1]},
                    "blue": {"x": gamut[2][0], "y": gamut[2][1]},
                }
        return light

    @staticmethod
    def _light_state(device: OnOffDevice) -> dict:
        """Return the controllable state of a light (used for eventstream diffs)."""
        state: dict[str, Any] = {"on": {"on": device.power_state}}
        if isinstance(device, BrightnessDevice):
            state["dimming"] = {"brightness": hass_to_hue_brightness(device.brightness)}
        if isinstance(device, CTDevice):
            in_ct_mode = device.color_mode == const.HASS_COLOR_MODE_COLOR_TEMP
            mirek = device.color_temp
            state["color_temperature"] = {
                "mirek": mirek if in_ct_mode else None,
                "mirek_valid": in_ct_mode and mirek is not None,
            }
        if isinstance(device, RGBDevice):
            x, y = device.xy_color
            state["color"] = {"xy": {"x": round(x, 4), "y": round(y, 4)}}
        return state

    async def _async_grouped_light_resource(
        self, rid: str, group_id: str, owner: dict, entity_ids: list[str]
    ) -> dict:
        any_on = False
        brightness = []
        for entity_id in entity_ids:
            device = await async_get_device(self.ctl, entity_id)
            if device.power_state:
                any_on = True
                if isinstance(device, BrightnessDevice):
                    brightness.append(hass_to_hue_brightness(device.brightness))
        return {
            "id": rid,
            "id_v1": f"/groups/{group_id}",
            "owner": owner,
            "on": {"on": any_on},
            "dimming": {
                "brightness": round(sum(brightness) / len(brightness), 2)
                if brightness
                else 0.0
            },
            "alert": {"action_values": ["breathe"]},
            "type": RTYPE_GROUPED_LIGHT,
        }

    async def _async_get_enabled_entities(self) -> list[str]:
        """Return all enabled light entities."""
        result = []
        for entity_id in self.ctl.controller_hass.get_entities():
            device = await async_get_device(self.ctl, entity_id)
            if device.enabled:
                result.append(entity_id)
        return result

    async def _async_get_groups(self) -> list[dict]:
        """Return all enabled rooms/zones as {id, name, type, class, entities}."""
        config = self.ctl.config_instance
        enabled_entities = set(await self._async_get_enabled_entities())
        result = []

        # local (non Home Assistant) groups
        groups = await config.async_get_storage_value("groups", default={})
        for group_id, group_conf in groups.items():
            if "area_id" in group_conf or group_conf.get("enabled") is False:
                continue
            if group_conf.get("type") not in ("Room", "Zone"):
                continue
            entities = []
            for light_id in group_conf.get("lights", []):
                with contextlib.suppress(Exception):
                    entities.append(
                        await config.async_entity_id_from_light_id(light_id)
                    )
            result.append(
                {
                    "id": group_id,
                    "name": group_conf.get("name", ""),
                    "type": group_conf["type"],
                    "class": group_conf.get("class"),
                    "entities": [e for e in entities if e in enabled_entities],
                }
            )

        # Home Assistant areas
        areas = await self.ctl.controller_hass.async_get_area_entities()
        for area in areas.values():
            group_id = await config.async_area_id_to_group_id(area["area_id"])
            group_conf = await config.async_get_group_config(group_id)
            if not group_conf["enabled"]:
                continue
            entities = [e for e in area["entities"] if e in enabled_entities]
            if not entities:
                continue
            result.append(
                {
                    "id": group_id,
                    "name": group_conf["name"] or area["name"],
                    "type": "Room",
                    "class": group_conf.get("class"),
                    "entities": entities,
                }
            )
        return result

    # ---------------------------------------------------------------------
    # Control
    # ---------------------------------------------------------------------

    async def _async_light_action(self, entity_id: str, request_data: dict) -> None:
        """Translate a v2 light/grouped_light PUT body to actions on a light entity."""
        device = await async_get_device(self.ctl, entity_id)
        call = device.new_control_state()

        duration = request_data.get("dynamics", {}).get("duration")
        call.set_transition_ms(duration if duration is not None else 400)

        if "on" in request_data:
            call.set_power_state(bool(request_data["on"].get("on")))
        if not call.control_state.power_state:
            # in v2, state changes do not implicitly turn on a light
            await call.async_execute()
            return

        if isinstance(device, BrightnessDevice):
            if (
                brightness := request_data.get("dimming", {}).get("brightness")
            ) is not None:
                call.set_brightness(hue_to_hass_brightness(brightness))
            if delta := request_data.get("dimming_delta"):
                step = delta.get("brightness_delta", 0)
                if delta.get("action") == "down":
                    step = -step
                if delta.get("action") in ("up", "down"):
                    current = hass_to_hue_brightness(device.brightness)
                    call.set_brightness(
                        hue_to_hass_brightness(clamp(current + step, 0, 100))
                    )
            if request_data.get("alert", {}).get("action") == "breathe":
                call.set_flash("short")

        if isinstance(device, CTDevice) and (
            mirek := request_data.get("color_temperature", {}).get("mirek")
        ):
            call.set_color_temperature(int(mirek))

        if isinstance(device, RGBDevice) and (
            xy := request_data.get("color", {}).get("xy")
        ):
            with contextlib.suppress(KeyError, TypeError):
                call.set_xy(xy["x"], xy["y"])

        await call.async_execute()

    async def _async_update_metadata(
        self, rtype: str, rid: str, request_data: dict
    ) -> web.Response:
        """Rename a device, room or zone."""
        name = request_data.get("metadata", {}).get("name")
        config = self.ctl.config_instance
        if rtype == RTYPE_DEVICE:
            for entity_id in await self._async_get_enabled_entities():
                if self.rid(RTYPE_DEVICE, entity_id) == rid:
                    if name:
                        device = await async_get_device(self.ctl, entity_id)
                        device.name = name
                    return send_v2_response([rref(rid, rtype)])
        else:
            for group in await self._async_get_groups():
                if self.rid(rtype, group["id"]) == rid:
                    if name:
                        group_conf = await config.async_get_group_config(group["id"])
                        group_conf["name"] = name
                        await config.async_set_storage_value(
                            "groups", group["id"], group_conf
                        )
                    return send_v2_response([rref(rid, rtype)])
        return send_v2_error("Not Found", 404)

    # ---------------------------------------------------------------------
    # Eventstream
    # ---------------------------------------------------------------------

    async def _async_on_state_changed(self, event: str, event_details: Any) -> None:
        """Push light state changes to connected eventstream clients."""
        if not self._event_queues:
            return
        entity_id = (event_details or {}).get("entity_id", "")
        if not entity_id.startswith("light."):
            return
        device = await async_get_device(self.ctl, entity_id)
        if not device.enabled:
            return
        # make sure the device has processed the new state before we read it
        await device.async_update_state()

        state = self._light_state(device)
        previous = self._last_light_states.get(entity_id, {})
        changed = {k: v for k, v in state.items() if previous.get(k) != v}
        self._last_light_states[entity_id] = state
        if not changed:
            return

        updates = [
            {
                "id": self.rid(RTYPE_LIGHT, entity_id),
                "id_v1": f"/lights/{device.light_id}",
                "owner": rref(self.rid(RTYPE_DEVICE, entity_id), RTYPE_DEVICE),
                "type": RTYPE_LIGHT,
                **changed,
            }
        ]
        # also update the grouped lights this light is part of
        groups = [{"id": "0", "type": "", "entities": None}]
        groups += await self._async_get_groups()
        for group in groups:
            entities = group["entities"]
            if entities is None:
                entities = await self._async_get_enabled_entities()
            if entity_id not in entities:
                continue
            owner = (
                rref(self.rid(RTYPE_BRIDGE_HOME), RTYPE_BRIDGE_HOME)
                if group["id"] == "0"
                else rref(
                    self.rid(
                        RTYPE_ZONE if group["type"] == "Zone" else RTYPE_ROOM,
                        group["id"],
                    ),
                    RTYPE_ZONE if group["type"] == "Zone" else RTYPE_ROOM,
                )
            )
            grouped = await self._async_grouped_light_resource(
                self.rid(RTYPE_GROUPED_LIGHT, group["id"]),
                group["id"],
                owner,
                entities,
            )
            updates.append(
                {
                    k: grouped[k]
                    for k in ("id", "id_v1", "owner", "on", "dimming", "type")
                }
            )
        self.publish_event("update", updates)

    def publish_event(self, event_type: str, data: list[dict]) -> None:
        """Publish an event to all connected eventstream clients."""
        now = datetime.datetime.now(datetime.UTC)
        container = [
            {
                "creationtime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "data": data,
                "id": str(uuid.uuid4()),
                "type": event_type,
            }
        ]
        payload = (
            f"id: {int(now.timestamp())}:0\n"
            f"data: {json.dumps(container, separators=(',', ':'))}\n\n"
        ).encode()
        for queue in self._event_queues:
            queue.put_nowait(payload)


def hass_to_hue_brightness(brightness: int | None) -> float:
    """Convert Home Assistant brightness (0-255) to Hue v2 brightness (0-100)."""
    if not brightness:
        return 0.0
    return round(clamp(brightness / 255 * 100, 0, 100), 2)


def hue_to_hass_brightness(brightness: float) -> int:
    """Convert Hue v2 brightness (0-100) to Home Assistant brightness (1-255)."""
    return int(clamp(round(brightness / 100 * 255), 1, 255))


def swversion_to_v2(swversion: str) -> str:
    """Convert a v1 bridge swversion (e.g. 1959097030) to v2 format (1.59.1959097030)."""
    return f"1.{swversion[2:4]}.{swversion}"


ROOM_ARCHETYPES = {
    "Living room": "living_room",
    "Kitchen": "kitchen",
    "Dining": "dining",
    "Bedroom": "bedroom",
    "Kids bedroom": "kids_bedroom",
    "Bathroom": "bathroom",
    "Nursery": "nursery",
    "Recreation": "recreation",
    "Office": "office",
    "Gym": "gym",
    "Hallway": "hallway",
    "Toilet": "toilet",
    "Front door": "front_door",
    "Garage": "garage",
    "Terrace": "terrace",
    "Garden": "garden",
    "Driveway": "driveway",
    "Carport": "carport",
    "Home": "home",
    "Downstairs": "downstairs",
    "Upstairs": "upstairs",
    "Top floor": "top_floor",
    "Attic": "attic",
    "Guest room": "guest_room",
    "Staircase": "staircase",
    "Lounge": "lounge",
    "Man cave": "man_cave",
    "Computer": "computer",
    "Studio": "studio",
    "Music": "music",
    "TV": "tv",
    "Reading": "reading",
    "Closet": "closet",
    "Storage": "storage",
    "Laundry room": "laundry_room",
    "Balcony": "balcony",
    "Porch": "porch",
    "Barbecue": "barbecue",
    "Pool": "pool",
}


def hue_room_archetype(group_class: str | None) -> str:
    """Convert a v1 group class to a v2 room archetype."""
    return ROOM_ARCHETYPES.get(group_class or "", "other")
