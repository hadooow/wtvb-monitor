import asyncio
from types import SimpleNamespace

import pytest

from app.ble_gateway import BleGateway, SERVICE_UUID, NOTIFY_UUID
from app.gateway import SerialGateway
from app.config import Settings
from app.gateway import advertisement_name, parse_gateway_line, GatewayEvent
from test_field_v0612 import FRAME
from test_scan_phases import MACS, phase_scheduler

MAC = MACS[0]
DEVICE = SimpleNamespace(address=':'.join(MAC[i:i+2] for i in range(0,12,2)), name='WT-CBF0-W31')


class Services(list):
    def get_service(self, uuid):
        return next((s for s in self if s.uuid == uuid), None)


class Client:
    def __init__(self, device, **options):
        self.device = device
        self.options = options
        self.is_connected = False
        self.mtu_size = 247
        self.services = Services([SimpleNamespace(uuid=SERVICE_UUID, characteristics=[
            SimpleNamespace(uuid=NOTIFY_UUID, properties=['notify'])])])
        self.disconnects = 0
        self.notify = None

    async def connect(self):
        self.is_connected = True

    async def start_notify(self, char, callback):
        self.notify = callback
        callback(char, b'early')

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False
        self.options['disconnected_callback'](self)


def gateway(factory=Client):
    g = BleGateway(client_factory=factory, scanner_factory=lambda **kwargs: None)
    g._started = True
    g._detection(DEVICE, SimpleNamespace(rssi=-60, local_name=DEVICE.name))
    g.poll()
    return g


def test_connect_uses_scanned_object_uncached_services_no_pair_and_commits_after_notify():
    async def run():
        g = gateway()
        g.connect(MAC)
        assert g.busy
        await asyncio.gather(*tuple(g._tasks))
        client = g._clients[MAC]
        assert client.device is DEVICE
        assert client.options['pair'] is False
        assert client.options['winrt']['use_cached_services'] is False
        events = g.poll()
        assert [e.kind for e in events] == ['notify','connected']
        assert events[0].handle == events[1].handle
        assert events[1].mtu == 247 and events[1].service_count == 1
        assert not g.busy
        assert g.diagnostics()['gatt_services'][MAC][0]['uuid'] == SERVICE_UUID
        await g.aclose()
        assert client.disconnects == 1 and not g._clients
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['connect', 'service', 'properties', 'subscribe'])
def test_failed_stage_cleans_up_without_success(failure):
    class Failing(Client):
        async def connect(self):
            if failure == 'connect': raise RuntimeError('radio unavailable')
            await super().connect()
            if failure == 'service': self.services.clear()
            if failure == 'properties': self.services[0].characteristics[0].properties = ['read']
        async def start_notify(self, char, callback):
            if failure == 'subscribe': raise RuntimeError('subscription denied')
            await super().start_notify(char, callback)
    async def run():
        g = gateway(Failing)
        g.connect(MAC)
        await asyncio.gather(*tuple(g._tasks))
        events = g.poll()
        assert [e.kind for e in events] == ['error']
        assert 'stage=' in events[0].message
        assert not g._clients and not g._handles and not g.busy
    asyncio.run(run())


def test_session_handle_blocks_old_callbacks_and_disconnect_emits_once():
    async def run():
        g = gateway()
        g.connect(MAC)
        await asyncio.gather(*tuple(g._tasks))
        old = g._clients[MAC]
        old_handle = g._handles[MAC]
        g.poll()
        g.disconnect(MAC)
        await asyncio.gather(*tuple(g._tasks))
        assert [e.kind for e in g.poll()] == ['disconnected']
        g.connect(MAC)
        await asyncio.gather(*tuple(g._tasks))
        assert g._handles[MAC] != old_handle
        g.poll()
        old.notify(None,b'late')
        old.options['disconnected_callback'](old)
        assert not g.poll() and MAC in g._clients
        current = g._clients[MAC]
        current.is_connected = False
        current.options['disconnected_callback'](current)
        assert [e.kind for e in g.poll()] == ['disconnected']
        assert MAC not in g._clients
        await g.aclose()
    asyncio.run(run())


def test_missing_or_expired_discovery_never_calls_client(monkeypatch):
    g = gateway(lambda *a,**k: pytest.fail('must not connect'))
    g._seen[MAC] -= 61
    g.connect(MAC)
    assert 'BLE_NOT_DISCOVERED' in g.poll()[0].message


def test_cancellation_during_subscribe_disconnects_and_suppresses_late_events():
    entered = None
    clients=[]
    class Slow(Client):
        def __init__(self,*a,**k): super().__init__(*a,**k); clients.append(self)
        async def start_notify(self,char,callback):
            self.notify=callback
            entered.set()
            await asyncio.Event().wait()
    async def run():
        nonlocal entered
        entered=asyncio.Event()
        g=gateway(Slow)
        g.connect(MAC)
        await entered.wait()
        await g.aclose()
        clients[0].notify(None,b'late')
        assert clients[0].disconnects == 1
        assert not g._clients and not g.busy and not g.poll()
    asyncio.run(run())


def test_scanner_failure_is_visible_and_releases_busy():
    class Scanner:
        def __init__(self,**kwargs): pass
        async def start(self): raise RuntimeError('Bluetooth is off')
        async def stop(self): pass
    async def run():
        g=BleGateway(scanner_factory=Scanner,client_factory=Client)
        g.start()
        await asyncio.gather(*tuple(g._tasks))
        assert not g.busy and not g.scanning
        assert g.diagnostics()['ble_scan_error']=='Bluetooth is off'
        assert g.poll()[0].kind=='warning'
        await g.aclose()
    asyncio.run(run())


def test_cancel_scan_stops_scanner():
    entered=None
    scanners=[]
    class Scanner:
        def __init__(self,**kwargs): self.stopped=False; scanners.append(self)
        async def start(self): entered.set()
        async def stop(self): self.stopped=True
    async def run():
        nonlocal entered
        entered=asyncio.Event()
        g=BleGateway(scanner_factory=Scanner,client_factory=Client)
        g.start()
        await entered.wait()
        await g.aclose()
        assert scanners[0].stopped and not g.busy
    asyncio.run(run())


def test_disconnect_failure_retains_live_client():
    class Failing(Client):
        async def disconnect(self): raise RuntimeError('disconnect rejected')
    async def run():
        g=gateway(Failing)
        g.connect(MAC)
        await asyncio.gather(*tuple(g._tasks))
        g.poll()
        g.disconnect(MAC)
        await asyncio.gather(*tuple(g._tasks))
        assert MAC in g._clients
        assert [e.kind for e in g.poll()]==['info','connected']
        g._clients.clear()
        await g.aclose()
    asyncio.run(run())


def test_native_mode_settings_and_driver(phase_scheduler):
    s,_=phase_scheduler
    s.settings.update({'gateway_driver':'ble'})
    s.gateway=s._make_gateway()
    assert isinstance(s.gateway,BleGateway)
    assert not s._ready_to_connect(s.states[MAC], 100)
    assert Settings(gateway_driver='ble').validate() is None


def test_new_serial_profile_uses_firmware_default_intervals():
    g=SerialGateway('COM3',115200,connection_profile=4)
    g.addresses[MAC]=(0,1)
    g.connect(MAC)
    command,mac=g._commands.get_nowait()
    assert command==f'AT+CONN={MAC},0,1,247,40000,1'
    assert mac==MAC


def test_field_broadcast_name_is_decoded_without_affecting_address():
    line='+SC_NTF:F80D11C2A52E,0,1,0,-63,127,38,0,16,0201050C0957542D434246352D573331,0,'
    e=parse_gateway_line(line)
    assert e.message=='WT-CBF5-W31' and e.addr_type==1 and e.rssi==-63
    assert advertisement_name('FF09AA') is None
    assert advertisement_name('not hex') is None


def test_frequent_advertisements_periodically_refresh_scheduler(monkeypatch):
    clock=[100.]
    monkeypatch.setattr('app.ble_gateway.time.monotonic',lambda:clock[0])
    g=gateway()
    for i in range(1,11):
        clock[0]=100+i*0.1
        g._detection(DEVICE,SimpleNamespace(rssi=-60,local_name=DEVICE.name))
    assert len(g.poll())==2


def test_native_scheduler_buffers_early_data_and_rejects_old_handle(phase_scheduler):
    s,clock=phase_scheduler
    s.settings.gateway_driver='ble'
    state=s.states[MAC]
    state.status='connecting'
    asyncio.run(s._handle_event(GatewayEvent('notify',MAC,handle=12,payload=FRAME)))
    assert not state.latest
    asyncio.run(s._handle_event(GatewayEvent('connected',MAC,handle=12)))
    assert state.latest and state.last_sample_at==clock[0]
    before=state.last_sample_at
    clock[0]+=2
    asyncio.run(s._handle_event(GatewayEvent('notify',MAC,handle=11,payload=FRAME)))
    assert state.last_sample_at==before
