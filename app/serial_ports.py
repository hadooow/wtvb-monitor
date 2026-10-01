"""Enumerate ports without opening unrelated serial or Bluetooth devices."""
from serial.tools import list_ports
import serial


def available_ports():
    ports = []
    for port in list_ports.comports():
        hwid = port.hwid or ''
        bluetooth = hwid.upper().startswith(('BTH', 'BLUETOOTH'))
        usb = port.vid is not None and not bluetooth
        ports.append({'device': port.device, 'description': port.description,
                      'hwid': hwid, 'bluetooth': bluetooth, 'usb': usb})
    return sorted(ports, key=lambda p: (p['bluetooth'], not p['usb'], p['device']))


def resolve_port(requested):
    ports = available_ports()
    if requested.strip().lower() == 'auto':
        candidates = [p for p in ports if p['usb']]
        if len(candidates) != 1:
            raise serial.SerialException('SERIAL_PORT_SELECTION: 无法唯一确定 USB 网关，请在采集设置中选择实际串口')
        return candidates[0]['device']
    selected = next((p for p in ports if p['device'].upper() == requested.upper()), None)
    if selected and selected['bluetooth']:
        raise serial.SerialException('SERIAL_BLUETOOTH_PORT: 所选串口是 Windows 蓝牙虚拟串口，请选择 USB 网关端口或切换电脑蓝牙直连')
    return requested
