from app.gateway import parse_gateway_line


def test_connection_failure_keeps_ble_reason_code():
    event = parse_gateway_line("+CONN:2,0,FE6DF407B3E4,34,DISSCONNECT")
    assert event is not None
    assert event.kind == "error"
    assert event.mac == "FE6DF407B3E4"
    assert event.message == "DISSCONNECT (BLE code 34)"


def test_logs_persist_and_download_contains_configuration_but_no_database(tmp_path):
    import io
    import json
    import logging
    import zipfile
    from app.diagnostics import configure_logging, diagnostic_archive

    path = configure_logging(tmp_path)
    logger = logging.getLogger('app.gateway')
    logger.info('TX AT+CONN=C2372102DEEF,,,247,40000,1,40,20,0,600')
    logger.error('RX ERROR')
    archive = diagnostic_archive({'gateway': {'connected': 0}}, {'serial_port': 'COM3'}, tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        assert 'TX AT+CONN=C2372102DEEF' in z.read('logs/monitor.log').decode('utf-8')
        assert 'RX ERROR' in z.read('logs/monitor.log').decode('utf-8')
        assert json.loads(z.read('diagnostics.json'))['settings']['serial_port'] == 'COM3'
        assert not any('.db' in name for name in z.namelist())
    # Close test handler, including on Windows where an open file cannot be removed.
    for h in list(logging.getLogger().handlers):
        if getattr(h, 'baseFilename', None) == str(path):
            logging.getLogger().removeHandler(h)
            h.close()
