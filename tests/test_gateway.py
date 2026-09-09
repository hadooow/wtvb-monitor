import threading

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


def test_parse_scan_notification():
    event = parse_gateway_line('+SC_NTF:FE6DF407B3E4,0,1,0,-83,127,39,0,16,020105,0,')
    assert event.kind == 'scan'
    assert (event.mac, event.rssi, event.addr_id, event.addr_type) == ('FE6DF407B3E4', -83, 0, 1)


def test_parse_notify():
    event = parse_gateway_line('+NOTIFY:0,FE6DF407B3E4,16,FFE4,1,4,55610000')
    assert event.kind == 'notify'
    assert event.payload == b'\x55\x61\x00\x00'


def test_parse_connection_failure():
    event = parse_gateway_line('+CONN:2,0,FE6DF407B3E4,22,DISSCONNECT')
    assert event.kind == 'error'
    assert event.message == 'DISSCONNECT (BLE code 22)'
    timeout = parse_gateway_line('+CONN:2,65535,FE6DF407B3E4,0,TIMEOUT')
    assert timeout.message == 'TIMEOUT'
    unknown = parse_gateway_line('+CONN:2,65535,FE6DF407B3E4,0,NEW_ERROR')
    assert unknown.kind == 'error'


def test_profiles_start_with_manufacturer_multilink_example():
    gateway = SerialGateway('COM3', 115200)
    gateway._serial = FakeSerial()
    mac = 'F8C5C0B8917E'
    gateway.addresses[mac] = (0, 3)
    commands = []
    for index in range(5):
        gateway._profile_cursor[mac] = index
        gateway.connect(mac)
        commands.append(gateway._commands.get_nowait()[0])
    assert commands[0] == f'AT+CONN={mac},,,247,40000,1,40,20,0,600'
    assert commands[1] == f'AT+CONN={mac},0,3,247,40000,1,40,20,0,600'
    assert commands[2] == f'AT+CONN={mac},,,23,40000,1,40,20,0,600'
    assert commands[3].endswith(',1,1,0')
    assert commands[4].endswith(',1,1,0')


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
    finally:
        gateway.stop()
        thread.join(1)


def test_plain_error_is_associated_with_attempt_and_advances_profile():
    gateway = SerialGateway('COM3', 115200)
    gateway._serial = FakeSerial(lambda _: gateway._receive_line('ERROR'))
    gateway._running.set()
    mac = 'FE6DF407B3E4'
    gateway._active_profile[mac] = 0
    gateway._execute('AT+CONN=test', mac)
    event = gateway.poll()[0]
    assert event.mac == mac and 'AT_ERROR' in event.message
    assert gateway._profile_cursor[mac] == 1


def test_scan_notification_ok_does_not_ack_pending_command():
    gateway = SerialGateway('COM3', 115200)
    gateway._pending_command = 'AT+OP?'
    gateway._receive_line('+SC_NTF:FE6DF407B3E4,0,1,0,-83,127,39,0,16,020105,0,')
    gateway._receive_line('OK')
    assert gateway._terminal is None
    gateway._receive_line('+OP:V1.53,EW-DTU02,EC0FFC9664F3')
    gateway._receive_line('OK')
    assert gateway._terminal == 'OK'


def test_terminator_missing_faults_gateway_instead_of_sending_another_connection(monkeypatch):
    gateway = SerialGateway('COM3', 115200)
    gateway._serial = FakeSerial()
    gateway._running.set()
    ticks = iter([0, 100])
    monkeypatch.setattr('app.gateway.time.monotonic', lambda: next(ticks))
    gateway._execute('AT+CONN=test', 'FE6DF407B3E4')
    assert gateway._faulted and gateway.busy
    assert any('AT_RESPONSE_TIMEOUT' in (e.message or '') for e in gateway.poll())


def test_field_empty_connection_list_resumes_startup_scan():
    """Replay the field firmware's CNB:0 reply without an additional OK."""
    gateway = SerialGateway('COM3', 115200)
    gateway.COMMAND_TIMEOUT_SECONDS = 0.03
    gateway._running.set()
    replies = {
        'AT+SCAN=0': ['OK'],
        'AT+OP?': ['+OP:V1.5(2507081320),EW-DTU02-M,000000000000', 'OK'],
        'AT+CNNI=': ['+CNB:0'],
        'AT+SCAN=1': ['OK'],
    }

    def respond(command):
        for line in replies[command]:
            gateway._receive_line(line)
        if command == 'AT+SCAN=1':
            gateway._running.clear()

    gateway._serial = FakeSerial(respond)
    for command in replies:
        gateway.send(command)
    gateway._command_loop()
    assert gateway._serial.writes == list(replies)
    assert not gateway._faulted
    assert not any(e.kind == 'error' for e in gateway.poll())


def test_empty_list_accepts_optional_ok_and_does_not_leak_to_next_command():
    gateway = SerialGateway('COM3', 115200)
    gateway.COMMAND_TIMEOUT_SECONDS = 0.03
    gateway._running.set()
    gateway._serial = FakeSerial(lambda _: [gateway._receive_line(l) for l in ['+CNB:0', 'OK']])
    gateway._execute('AT+CNNI=', None)
    assert not gateway._faulted
    gateway._serial.callback = None
    gateway._execute('AT+OP?', None)
    assert gateway._faulted  # Query's OK cannot acknowledge the silent OP query.


def test_empty_list_compatibility_does_not_hide_other_failures():
    cases = [
        ('AT+CNNI=', []),
        ('AT+CNNI=', ['+CNB:1']),
        ('AT+CNNI=', ['+CNB:0', '+CONN:0,FE6DF407B3E4,4,247']),
        ('AT+CNNI=', ['+CNB:0', 'ERROR']),
        ('AT+OP?', ['+CNB:0']),
    ]
    for command, lines in cases:
        gateway = SerialGateway('COM3', 115200)
        gateway.COMMAND_TIMEOUT_SECONDS = 0.03
        gateway._running.set()
        gateway._serial = FakeSerial(lambda _: [gateway._receive_line(l) for l in lines])
        gateway._execute(command, None)
        assert any(e.kind == 'error' for e in gateway.poll()), (command, lines)


def test_empty_list_cannot_finish_bluetooth_connection():
    gateway = SerialGateway('COM3', 115200)
    gateway._pending_command = 'AT+CONN=FE6DF407B3E4,,,247,40000,1,40,20,0,600'
    gateway._pending_mac = 'FE6DF407B3E4'
    gateway._receive_line('+CNB:0')
    assert gateway._terminal is None
    assert gateway._connection_result is None


def test_scan_traffic_does_not_gate_original_sensor_connection():
    """SCAN=0 stalled in the field; connect must reach the wire directly."""
    gateway = SerialGateway('COM3', 115200)
    gateway.COMMAND_TIMEOUT_SECONDS = 0.03
    gateway._running.set()
    mac = 'FE6DF407B3E4'

    def respond(command):
        if command.startswith('AT+CONN='):
            gateway._receive_line(f'+SC_NTF:{mac},0,1,0,-46,127,37,0,16,020105,0,')
            gateway._receive_line('OK')
            assert gateway._terminal is None
            gateway._receive_line(f'+CONN:2,0,{mac},4,247')
            gateway._receive_line('+CHAR:2,FFE4,0,0,0,0,1,0,0')
            gateway._receive_line('OK')
        elif command == 'AT+SCAN=1':
            gateway._receive_line('OK')
            gateway._running.clear()
        else:
            raise AssertionError(command)

    gateway._serial = FakeSerial(respond)
    gateway.connect(mac)
    gateway._command_loop()
    assert gateway._serial.writes == [f'AT+CONN={mac},,,247,40000,1,40,20,0,600', 'AT+SCAN=1']
    assert not gateway._faulted
    events = gateway.poll()
    assert any(e.kind == 'connected' and e.mac == mac for e in events)
    assert not any(e.kind == 'error' for e in events)
