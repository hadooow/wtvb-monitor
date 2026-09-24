import threading
import time

from app.gateway import LineBuffer, SerialGateway, parse_gateway_line


class FakeSerial:
    is_open = True

    def __init__(self, callback=None):
        self.writes = []
        self.callback = callback

    def write(self, payload):
        command = payload.decode('ascii').strip()
        self.writes.append(command)
        if self.callback:
            self.callback(command)

    def close(self):
        self.is_open = False


def run_commands(gateway):
    gateway._worker = threading.Thread(target=gateway._command_loop, daemon=True)
    gateway._worker.start()
    deadline = time.monotonic() + 2
    while gateway._commands.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.001)
    gateway.stop()
    assert gateway._commands.unfinished_tasks == 0


def test_parse_scan_notification():
    event = parse_gateway_line('+SC_NTF:FE6DF407B3E4,0,1,0,-83,127,39,0,16,020105,0,')
    assert event.kind == 'scan'
    assert (event.mac, event.rssi, event.addr_id, event.addr_type) == ('FE6DF407B3E4', -83, 0, 1)


def test_parse_notify():
    event = parse_gateway_line('+NOTIFY:0,FE6DF407B3E4,16,FFE4,0,4,55610000')
    assert event.kind == 'notify'
    assert event.payload == b'\x55\x61\x00\x00'


def test_parse_notify_rejects_declared_length_mismatch():
    event = parse_gateway_line('+NOTIFY:0,FE6DF407B3E4,16,FFE4,0,5,55610000')
    assert event.kind == 'warning'
    assert 'Malformed NOTIFY' in event.message


def test_parse_connection_failure():
    event = parse_gateway_line('+CONN:2,0,FE6DF407B3E4,22,DISSCONNECT')
    assert event.kind == 'error'
    assert event.message == 'DISSCONNECT (BLE code 22)'
    timeout = parse_gateway_line('+CONN:2,65535,FE6DF407B3E4,0,TIMEOUT')
    assert timeout.message == 'TIMEOUT'
    unknown = parse_gateway_line('+CONN:2,65535,FE6DF407B3E4,0,NEW_ERROR')
    assert unknown.kind == 'error'


def test_profiles_start_with_verified_v04_command():
    gateway = SerialGateway('COM3', 115200)
    gateway._serial = FakeSerial()
    mac = 'F8C5C0B8917E'
    gateway.addresses[mac] = (0, 3)
    commands = []
    for index in range(len(gateway.CONNECTION_PROFILES)):
        gateway._profile_cursor[mac] = index
        gateway.connect(mac)
        commands.append(gateway._commands.get_nowait()[0])
    assert commands[0] == f'AT+CONN={mac},0,3,247,40000,1,40,20,0,600,1,1,0'
    assert commands[1] == f'AT+CONN={mac},,,247,40000,1,40,20,0,600'
    assert commands[2] == f'AT+CONN={mac},0,3,247,40000,1,40,20,0,600'
    assert commands[3] == f'AT+CONN={mac},,,247,40000,1,40,20,0,600,1,1,0'
    assert all(',247,40000,' in command for command in commands)


def test_fragmented_serial_reads_preserve_multiple_messages():
    buffer = LineBuffer()
    assert buffer.feed(b'\r\n+CONN:2,0,FE6D') == []
    assert buffer.feed(b'') == []
    assert buffer.feed(b'F407B3E4,4,247\r') == []
    assert buffer.feed(b'\n+SERV:1,FFE5,1\r\nOK\r\n') == [
        '+CONN:2,0,FE6DF407B3E4,4,247', '+SERV:1,FFE5,1', 'OK',
    ]


def test_connection_waits_for_matching_result_and_discovery_terminator():
    gateway = SerialGateway('COM3', 115200)
    written = threading.Event()
    gateway._serial = FakeSerial(lambda _: written.set())
    gateway._running.set()
    mac = 'FE6DF407B3E4'
    thread = threading.Thread(target=gateway._execute, args=('AT+CONN=test', mac))
    thread.start()
    assert written.wait(1)
    try:
        gateway._receive_line('OK')
        assert gateway.poll() == []
        gateway._receive_line(f'+CONN:2,0,{mac},4,247')
        assert gateway.poll() == []
        gateway._receive_line('+SERV:1,FFE5,1')
        gateway._receive_line('+CHAR:2,FFE4,0,0,0,0,1,0,0')
        assert gateway.poll() == []
        gateway._receive_line('OK')
        thread.join(1)
        assert not thread.is_alive()
        assert [e.kind for e in gateway.poll()] == ['connected']
    f