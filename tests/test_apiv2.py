"""Tests for the Hue CLIP API v2."""
import asyncio
import json

from emulated_hue.apiv2 import hass_to_hue_brightness, hue_to_hass_brightness

from .conftest import APP_KEY

HEADERS = {"hue-application-key": APP_KEY}


async def get_resources(client, path="/clip/v2/resource"):
    resp = await client.get(path, headers=HEADERS)
    assert resp.status == 200
    body = await resp.json()
    assert body["errors"] == []
    return body["data"]


def by_type(resources, rtype):
    return [r for r in resources if r["type"] == rtype]


async def test_unauthorized(client):
    resp = await client.get("/clip/v2/resource")
    assert resp.status == 403
    resp = await client.get("/clip/v2/resource", headers={"hue-application-key": "x"})
    assert resp.status == 403
    assert (await resp.json())["errors"][0]["description"] == "unauthorized user"


async def test_resource_overview(client):
    resources = await get_resources(client)
    assert len(by_type(resources, "bridge")) == 1
    assert len(by_type(resources, "bridge_home")) == 1
    assert len(by_type(resources, "light")) == 3
    # 3 lights + bridge
    assert len(by_type(resources, "device")) == 4
    assert len(by_type(resources, "room")) == 2
    # 2 rooms + bridge home
    assert len(by_type(resources, "grouped_light")) == 3

    # ids are unique and references resolve
    ids = {r["id"] for r in resources}
    assert len(ids) == len(resources)
    for resource in resources:
        refs = [resource.get("owner")] + resource.get("children", [])
        refs += resource.get("services", [])
        for ref in filter(None, refs):
            assert ref["rid"] in ids, (resource, ref)


async def test_ids_are_stable(client):
    first = await get_resources(client)
    second = await get_resources(client)
    assert [r["id"] for r in first] == [r["id"] for r in second]


async def test_bridge(client, bridge):
    (res,) = await get_resources(client, "/clip/v2/resource/bridge")
    assert res["bridge_id"] == bridge.config_instance.bridge_id.lower()
    device = next(
        d
        for d in await get_resources(client, "/clip/v2/resource/device")
        if d["id"] == res["owner"]["rid"]
    )
    assert device["product_data"]["product_archetype"] == "bridge_v2"
    assert device["product_data"]["software_version"].startswith("1.59.")


async def test_light_representation(client):
    lights = {
        r["metadata"]["name"]: r
        for r in await get_resources(client, "/clip/v2/resource/light")
    }
    color = lights["Living color"]
    assert color["on"] == {"on": True}
    assert color["dimming"]["brightness"] == hass_to_hue_brightness(128)
    assert color["color"]["xy"] == {"x": 0.3, "y": 0.4}
    assert color["color"]["gamut_type"] == "C"
    assert color["color_temperature"]["mirek_schema"] == {
        "mirek_minimum": 153,
        "mirek_maximum": 500,
    }
    assert color["color_temperature"]["mirek_valid"] is False

    dimmer = lights["Kitchen dimmer"]
    assert dimmer["on"] == {"on": False}
    assert "color" not in dimmer
    assert "color_temperature" not in dimmer

    switch = lights["Hall switch"]
    assert "dimming" not in switch


async def test_get_single_resource(client):
    (light, *_) = await get_resources(client, "/clip/v2/resource/light")
    data = await get_resources(client, f"/clip/v2/resource/light/{light['id']}")
    assert data == [light]
    resp = await client.get(
        "/clip/v2/resource/light/00000000-0000-0000-0000-000000000000",
        headers=HEADERS,
    )
    assert resp.status == 404


async def test_rooms(client):
    rooms = {
        r["metadata"]["name"]: r
        for r in await get_resources(client, "/clip/v2/resource/room")
    }
    assert set(rooms) == {"Living Room", "Kitchen"}
    devices = {
        d["id"]: d for d in await get_resources(client, "/clip/v2/resource/device")
    }
    living_children = [devices[c["rid"]] for c in rooms["Living Room"]["children"]]
    assert [d["metadata"]["name"] for d in living_children] == ["Living color"]

    # the hall switch has no room, so it is a direct child of bridge_home
    (home,) = await get_resources(client, "/clip/v2/resource/bridge_home")
    home_device_names = {
        devices[c["rid"]]["metadata"]["name"]
        for c in home["children"]
        if c["rtype"] == "device"
    }
    assert "Hall switch" in home_device_names
    assert "Living color" not in home_device_names


async def test_put_light(client, fake_hass):
    lights = {
        r["metadata"]["name"]: r
        for r in await get_resources(client, "/clip/v2/resource/light")
    }
    rid = lights["Living color"]["id"]
    resp = await client.put(
        f"/clip/v2/resource/light/{rid}",
        headers=HEADERS,
        json={
            "on": {"on": True},
            "dimming": {"brightness": 100},
            "color_temperature": {"mirek": 250},
            "dynamics": {"duration": 1000},
        },
    )
    assert resp.status == 200
    assert (await resp.json())["data"] == [{"rid": rid, "rtype": "light"}]
    assert fake_hass.calls[-1] == (
        "turn_on",
        "light.living_color",
        {"brightness": 255, "color_temp": 250, "transition": 1.0},
    )


async def test_put_light_xy_and_off(client, fake_hass):
    lights = {
        r["metadata"]["name"]: r
        for r in await get_resources(client, "/clip/v2/resource/light")
    }
    rid = lights["Living color"]["id"]
    await client.put(
        f"/clip/v2/resource/light/{rid}",
        headers=HEADERS,
        json={"color": {"xy": {"x": 0.6, "y": 0.3}}},
    )
    assert fake_hass.calls[-1][2]["xy_color"] == (0.6, 0.3)

    await client.put(
        f"/clip/v2/resource/light/{rid}", headers=HEADERS, json={"on": {"on": False}}
    )
    assert fake_hass.calls[-1] == ("turn_off", "light.living_color", {})


async def test_put_does_not_implicitly_turn_on(client, fake_hass):
    lights = {
        r["metadata"]["name"]: r
        for r in await get_resources(client, "/clip/v2/resource/light")
    }
    rid = lights["Kitchen dimmer"]["id"]
    await client.put(
        f"/clip/v2/resource/light/{rid}",
        headers=HEADERS,
        json={"dimming": {"brightness": 50}},
    )
    assert not [c for c in fake_hass.calls if c[0] == "turn_on"]


async def test_put_grouped_light(client, fake_hass):
    (home,) = await get_resources(client, "/clip/v2/resource/bridge_home")
    rid = home["services"][0]["rid"]
    resp = await client.put(
        f"/clip/v2/resource/grouped_light/{rid}",
        headers=HEADERS,
        json={"on": {"on": True}},
    )
    assert resp.status == 200
    assert {c[1] for c in fake_hass.calls if c[0] == "turn_on"} == {
        "light.living_color",
        "light.kitchen_dimmer",
    }


async def test_rename_device(client):
    devices = await get_resources(client, "/clip/v2/resource/device")
    device = next(d for d in devices if d["metadata"]["name"] == "Hall switch")
    resp = await client.put(
        f"/clip/v2/resource/device/{device['id']}",
        headers=HEADERS,
        json={"metadata": {"name": "Hallway"}},
    )
    assert resp.status == 200
    names = {
        r["metadata"]["name"]
        for r in await get_resources(client, "/clip/v2/resource/light")
    }
    assert "Hallway" in names


async def test_eventstream(client, fake_hass):
    lights = {
        r["metadata"]["name"]: r
        for r in await get_resources(client, "/clip/v2/resource/light")
    }
    resp = await client.get("/eventstream/clip/v2", headers=HEADERS)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"
    assert await resp.content.readline() == b": hi\n"
    await resp.content.readline()

    # first event after connecting contains the full light state
    await fake_hass.change_state("light.kitchen_dimmer", "on", brightness=255)
    lines = [await asyncio.wait_for(resp.content.readline(), 2) for _ in range(3)]
    assert lines[0].startswith(b"id: ")
    container = json.loads(lines[1].removeprefix(b"data: "))
    assert container[0]["type"] == "update"
    updates = {u["id"]: u for u in container[0]["data"]}
    light_update = updates[lights["Kitchen dimmer"]["id"]]
    assert light_update["on"] == {"on": True}
    assert light_update["dimming"] == {"brightness": 100.0}
    assert any(u["type"] == "grouped_light" for u in updates.values())
    resp.close()


def test_brightness_conversion():
    assert hass_to_hue_brightness(255) == 100.0
    assert hass_to_hue_brightness(None) == 0.0
    assert hue_to_hass_brightness(100) == 255
    assert hue_to_hass_brightness(0) == 1
    assert hue_to_hass_brightness(hass_to_hue_brightness(128)) == 128
