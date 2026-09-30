import sys


if __name__ == "__main__":
    if '--verify-ble-runtime' in sys.argv:
        from bleak.backends.winrt.client import BleakClientWinRT
        from bleak.backends.winrt.scanner import BleakScannerWinRT
        print('Windows BLE runtime import passed')
    else:
        from app.main import run
        run()
