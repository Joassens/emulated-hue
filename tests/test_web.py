"""Tests for the webserver helpers."""
import logging

from aiohttp import web

from emulated_hue.web import log_not_found


async def test_not_found_is_logged_with_source(aiohttp_client, caplog):
    app = web.Application(middlewares=[log_not_found])
    app.router.add_get("/ok", lambda _request: web.Response(text="ok"))
    client = await aiohttp_client(app)

    with caplog.at_level(logging.DEBUG, logger="emulated_hue.web"):
        assert (await client.get("/ok")).status == 200
        assert (await client.get("/vendor/phpunit/eval-stdin.php")).status == 404

    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["[127.0.0.1] Not found: GET /vendor/phpunit/eval-stdin.php"]
