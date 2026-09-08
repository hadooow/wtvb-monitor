from app.gateway import parse_gateway_line


def test_connection_failure_keeps_ble_reason_code():
    event = parse_gateway_line("+CONN:2,0,FE6DF407B3E4,34,DISSCONNECT")
    assert event is not None
    assert event.kind == "error"
    assert event.mac == "FE6DF407B3E4"
    assert event.message == "DISSCONNECT (BLE code 34)"
