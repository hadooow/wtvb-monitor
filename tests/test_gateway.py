from app.gateway import SerialGateway, parse_gateway_line


class FakeSerial:
    is_open = True

    def __init__(self):
        self.writes = []

    def write(self, payload):
        self.writes.append(payload.decode("ascii").strip())


def test_parse_scan_notification():
    event = parse_gateway_line("+SC_NTF:FE6DF407B3E4,0,1,0,-83,127,39,0,16,020105,0,")
    assert event is not None
    assert event.kind == "scan"
    assert event.mac == "FE6DF407B3E4"
    assert event.rssi == -83
    assert event.addr_id == 0
    assert event.addr_type == 1


def test_parse_notify():
    event = parse_gateway_line("+NOTIFY:0,FE6DF407B3E4,16,FFE4,1,4,55610000")
    assert event is not None
    assert event.kind == "notify"
    assert event.payload == b"\x55\x61\x00\x00"


def test_parse_connection_failure():
    event = parse_gateway_line("+CONN:2,0,FE6DF407B3E4,22,DISSCONNECT")
    assert event is not None
    assert event.kind == "error"
    assert event.message == "DISSCONNECT (BLE code 22)"


def test_connection_profiles_fall_back_to_auto_address_and_plain_mode():
    gateway = SerialGateway("COM3", 115200)
    fake = FakeSerial()
    gateway._serial = fake
    mac = "F8C5C0B8917E"
    gateway.addresses[mac] = (0, 3)

    gateway.connect(mac)
    gateway._profile_cursor[mac] = 1
    gateway.connect(mac)
    gateway._profile_cursor[mac] = 2
    gateway.connect(mac)

    assert fake.writes[0].startswith(f"AT+CONN={mac},0,3,247,")
    assert fake.writes[0].endswith(",1,1,0")
    assert fake.writes[1].startswith(f"AT+CONN={mac},,,247,")
    assert fake.writes[1].endswith(",1,1,0")
    assert fake.writes[2].startswith(f"AT+CONN={mac},,,247,")
    assert fake.writes[2].endswith(",1,0,0")
