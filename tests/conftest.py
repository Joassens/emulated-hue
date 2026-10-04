"""Shared fixtures: an emulated bridge backed by a fake Home Assistant."""
import asyncio
from typing import Any

import pytest
from aiohttp import web

from emulated_hue.apiv2 import HueApiV2Endpoints
from emulated_hue.controllers import Controller, devices
from emulated_hue.controllers import config as config_module
from emulated_hue.controllers.config import Config

APP_KEY = "test-application-key-0000000000000000000"

LIGHTS = {
    "light.living_color": {
        "state": "on",
        "attributes": {
            "friendly_name": "Living color",
            "supported_color_modes": ["color_temp", "xy"],
            "color_mode": "xy",
            "brightness": 128,
            "xy_color": [0.3, 0.4],
            # recent Home Assistant: decimal hs_color, kelvin only
            "hs_color": [28.439, 66.366],
            "color_temp_kelvin": 3333,
            "min_color_temp_kelvin": 2000,
            "max_color_temp_kelvin": 6535,
        },
    },
    "light.kitchen_dimmer": {
        "state": "off",
        "attributes": {
            "friendly_name": "Kitchen dimmer",
            "supported_color_modes": ["brightness"],
        },
    },
    "light.hall_switch": {
        "state": "on",
        "attributes": {
            "friendly_name": "Hall switch",
            "supported_color_modes": ["onoff"],
        },
    },
}

AREAS = {
    "living_room": {"area_id": "living_room", "name": "Living Room"},
    "kitchen": {"area_id": "kitchen", "name": "Kitchen"},
}
AREA_ENTITIES = {
    "living_room": ["light.living_color"],
    "kitchen": ["light.kitchen_dimmer"],
}


class FakeHass:
    """Minimal stand-in for HomeAssistantController."""

    def __init__(self):
        """Initialize."""
        self.states = {k: {"entity_id": k, **v} for k, v in LIGHTS.items()}
        self.calls: list[tuple[str, str, dict]] = []
        self.listeners: list = []

    def get_entities(self, domain: str = "light") -> list[str]:
        return list(self.states)

    def get_entity_state(self, entity_id: str) -> dict:
        return self.states[entity_id]

    def get_device_id_from_entity_id(self, entity_id: str) -> None:
        return None

    def get_device_attributes(self, device_id: str) -> dict:
        return {}

    def register_event_callback(self, cb, event_filter=None, entity_filter=None):
        listener = (cb, entity_filter)
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def register_state_changed_callback(self, cb, entity_id):
        return self.register_event_callback(cb, "state_changed", entity_id)

    async def async_get_area_entities(self, domain_filter=None) -> dict:
        return {
            area_id: {**area, "entities": list(AREA_ENTITIES[area_id])}
            for area_id, area in AREAS.items()
        }

    async def async_turn_on(self, entity_id: str, data: dict) -> None:
        self.calls.append(("turn_on", entity_id, data))

    async def async_turn_off(self, entity_id: str) -> None:
        self.calls.append(("turn_off", entity_id, {}))

    async def set_state(self, *args, **kwargs) -> None:
        pass

    async def change_state(self, entity_id: str, state: str, **attributes: Any):
        """Simulate a state_changed event from Home Assistant."""
        self.states[entity_id]["state"] = state
        self.states[entity_id]["attributes"].update(attributes)
        details = {"entity_id": entity_id, "new_state": self.states[entity_id]}
        for cb, entity_filter in list(self.listeners):
            if entity_filter is None or entity_id in entity_filter:
                await cb("state_changed", details)


@pytest.fixture
def fake_hass():
    return FakeHass()


@pytest.fixture
async def bridge(tmp_path, fake_hass, monkeypatch):
    """Return the controller of an emulated bridge with a registered user."""
    # throttling would drop commands sent right after device creation
    monkeypatch.setattr(config_module, "DEFAULT_THROTTLE_MS", 0)
    getattr(devices, "__device_cache").clear()
    ctl = Controller(controller_hass=fake_hass, loop=asyncio.get_running_loop())
    ctl.config_instance = Config(ctl, str(tmp_path), 80, 443, False)
    await ctl.config_instance.async_set_storage_value(
        "users",
        APP_KEY,
        {"name": "test#pytest", "username": APP_KEY, "clientkey": "00" * 16},
    )
    yield ctl
    getattr(devices, "__device_cache").clear()


@pytest.fixture
async def client(aiohttp_client, bridge):
    """Return a test client for the v2 API."""
    app = web.Application()
    v2_api = HueApiV2Endpoints(bridge)
    app.add_routes(v2_api.get_routes())
    await v2_api.async_setup()
    test_client = await aiohttp_client(app)
    test_client.v2_api = v2_api
    yield test_client
    await v2_api.async_stop()
