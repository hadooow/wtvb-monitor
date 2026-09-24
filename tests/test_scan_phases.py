import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.database import Database
from app.gateway import GatewayEvent, SerialGateway, parse_gateway_line
from app.scheduler import DeviceRuntime, Scheduler
from test_gateway import FakeSerial, run_commands

MACS = ['02A000000001', '02A000000002', '02A000000003']
REPLAY = json.loads((Path(__file__).parent / 'fixtures/notify-replay.json').read_text())


@pytest.mark.parametrize('reply', [['ERROR'], [REPLAY['valid'], 'OK'], []])
def test_unconfirmed_scan_stop_never_sends_connection(reply):
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = 0.01
    g._running.set()
    g._serial = FakeSerial(lambda _: [g._receive_line(line) for line in reply])
    g.connect(MACS[0])
    run_commands(g)
    assert g._serial.writes == ['AT+SCAN=0'] * (1 if reply == ['ERROR'] else 3)
    assert g._faulted and g.first_fault
    first = g.first_fault.copy()
    g._receive_line('OK')  # A late reply cannot unpause the worker.
    g._receive_line(REPLAY['corrupted'][0])
    assert g._faulted and g.first_fault == first
    assert g.warning_count == 1


def test_scan_start_rejection_is_blocking():
    g = SerialGateway('COM5', 115200)
    g._running.set()
    g._serial = FakeSerial(lambda _: g._receive_line('ERROR'))
    g._execute('AT+SCAN=1', None)
    assert g._faulted and g.scanning is not True


def test_notify_and_unrelated_disconnect_ok_do_not_ack_scan_stop():
    g = SerialGateway('COM5', 115200)
    g._pending_command = 'AT+SCAN=0'
    for line in [REPLAY['valid'], 'OK', f'+DISCON:2,0,{MACS[0]},8', 'OK']:
        g._receive_line(line)
        assert g._terminal is None
    g._receive_line('OK')
    assert g._terminal == 'OK'


def test_disconnect_does_not_resume_scanning():
    g = SerialGateway('COM5', 115200)
    g._running.set()
    g.active_links[MACS[0]] = 0
    def respond(_):
        g._receive_line('OK')  # Some firmware acknowledges before the event.
        assert g._terminal is None
        g._receive_line(f'+DISCON:2,0,{MACS[0]},22')
        g._receive_line('OK')
    g._serial = FakeSerial(respond)
    g.disconnect(MACS[0])
    run_commands(g)
    assert g._serial.writes == [f'AT+DISCON=,{MACS[0]}']
    assert not g.active_links


@pytest.mark.parametrize('line', REPLAY['corrupted'])
def test_field_corruption_is_dropped_as_warning(line):
    e = parse_gateway_line(line)
    assert e.kind == 'warning' and e.mac == MACS[0] and e.payload is None


@pytest.mark.parametrize('replacement', [',1,32,', ',0,31,', ',0,-1,'])
def test_notification_type_and_length_are_checked(replacement):
    assert parse_gateway_line(REPLAY['valid'].replace(',0,32,', replacement)).kind == 'warning'


def test_serial_read_fault_survives_later_notification_warning():
    class BrokenSerial(FakeSerial):
        def read(self, size):
            return b''
        @property
        def in_waiting(self):
            raise OSError('ReadFile failed')
    g = SerialGateway('COM5', 115200)
    g._serial = BrokenSerial()
    g._running.set()
    g._read_loop()
    g._receive_line(REPLAY['corrupted'][0])
    assert not g.online and g.busy
    assert 'ReadFile failed' in g.first_fault['message']
    assert g.warning_count == 1 and len(g.history) == 2


class PhaseGateway:
    busy = False
    scanning = False
    scan_started_at = None
    def __init__(self, clock):
        self.clock = clock
        self.active_links = {}
        self.calls = []
    def scan(self):
        assert not self.active_links
        self.calls.append(('scan',))
        self.scanning = True
        self.scan_started_at = self.clock[0]
    def stop_scan(self):
        self.calls.append(('stop_scan',))
        self.scanning = False
    def connect(self, mac):
        assert not self.scanning
        self.calls.append(('connect', mac))
    def report_no_data(self, mac):