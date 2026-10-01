import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ble_gateway import SERVICE_UUID, NOTIFY_UUID
from scripts.release_notes import current_notes
from test_ble_gateway import Client, DEVICE, MAC, gateway
from test_field_v0614 import finish


def test_disconnect_unsubscribes_before_release_and_requires_new_post_settle_ad(monkeypatch):
    clock = [100.]
    monkeypatch.setattr('app.ble_gateway.time.monotonic', lambda: clock[0])
    calls = []
    class Ordered(Client):
        async def stop_notify(self, characteristic):
            calls.append(('unsubscribe', characteristic))
            await super().stop_notify(characteristic)
        async def disconnect(self):
            calls.append(('disconnect', None))
            await super().disconnect()
    async def run():
        g = gateway(Ordered)
        g.connect(MAC)
        await finish(g)
        g.poll()
        g.disconnect(MAC)
        await finish(g)
        assert calls == [('unsubscribe', NOTIFY_UUID), ('disconnect', None)]
        assert not g.discovered(MAC)
        clock[0] += 1
        g._detection(DEVICE, SimpleNamespace(rssi=-60, local_name=DEVICE.name))
        assert not g.discovered(MAC)
        clock[0] += 2
        assert not g.discovered(MAC)  # old ad remains unusable after time passes
        g._detection(DEVICE, SimpleNamespace(rssi=-60, local_name=DEVICE.name))
        assert g.discovered(MAC)
        g.connect(MAC)
        await finish(g)
        assert MAC in g.active_links
        await g.aclose()
    asyncio.run(run())


def test_remote_link_loss_closes_retired_native_services_without_closing_new_peer():
    async def run():
        g = gateway()
        g.connect(MAC)
        await finish(g)
        old = g._clients[MAC]
        g.poll()
        old.is_connected = False
        old.options['disconnected_callback'](old)
        assert g.busy
        assert not g.discovered(MAC)
        await finish(g)
        assert old.disconnects == 1
        assert [e.kind for e in g.poll()] == ['disconnected']
        assert not g.busy
        await g.aclose()
    asyncio.run(run())


def test_missing_service_invalidates_discovery_and_targeted_retry_can_succeed(monkeypatch):
    clock = [100.]
    monkeypatch.setattr('app.ble_gateway.time.monotonic', lambda: clock[0])
    clients = []
    class Incomplete(Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)
            if len(clients) == 1: self.services.clear()
    async def run():
        g = gateway(Incomplete)
        g.connect(MAC)
        await finish(g)
        assert g.poll()[0].kind == 'error'
        assert clients[0].disconnects == 1
        assert not g.discovered(MAC)
        clock[0] += 3
        g._detection(DEVICE, SimpleNamespace(rssi=-60, local_name=DEVICE.name))
        g.poll()
        g.connect(MAC)
        await finish(g)
        assert clients[1].options['services'] == [SERVICE_UUID]
        assert g.poll()[-1].kind == 'connected'
        await g.aclose()
    asyncio.run(run())


def test_unsubscribe_failure_still_releases_connection():
    class Broken(Client):
        async def stop_notify(self, *args): raise RuntimeError('CCCD unavailable')
    async def run():
        g = gateway(Broken)
        g.connect(MAC)
        await finish(g)
        client = g._clients[MAC]
        g.poll()
        g.disconnect(MAC)
        await finish(g)
        assert client.disconnects == 1 and not g.active_links
        assert g.poll()[-1].kind == 'disconnected'
        await g.aclose()
    asyncio.run(run())


def test_shutdown_retries_a_cancelled_retired_native_cleanup():
    async def run():
        entered = asyncio.Event()
        class Slow(Client):
            async def disconnect(self):
                self.disconnects += 1
                if self.disconnects == 1:
                    entered.set()
                    await asyncio.Event().wait()
                self.is_connected = False
        g = gateway(Slow)
        g.connect(MAC)
        await finish(g)
        client = g._clients[MAC]
        client.is_connected = False
        client.options['disconnected_callback'](client)
        await entered.wait()
        await g.aclose()
        assert client.disconnects == 2
        assert not g._retired_clients and not g.active_links
    asyncio.run(run())


def test_release_body_contains_only_requested_version():
    changelog = '## v0.6.15（2026-10-01）\n\ncurrent\n\n---\n\n# v0.6.14 — previous\n\nold\n'
    assert current_notes(changelog, '0.6.15') == '## v0.6.15（2026-10-01）\n\ncurrent\n'
    assert 'old' not in current_notes(changelog, '0.6.15')
    assert 'current' not in current_notes(changelog, '0.6.14')
    with pytest.raises(ValueError): current_notes(changelog, '0.6.16')


def test_actual_changelog_matches_app_version():
    from app.diagnostics import VERSION
    changelog = (Path(__file__).resolve().parents[1] / 'RELEASE_NOTES.md').read_text(encoding='utf-8')
    body = current_notes(changelog, VERSION)
    assert f'v{VERSION}' in body
    assert 'v0.6.14' not in body
