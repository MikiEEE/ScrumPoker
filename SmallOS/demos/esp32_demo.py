"""
ESP32-oriented demo entry point.

This can run as a desktop example for structure review, but the optional Wi-Fi
connection path is intended for MicroPython boards that expose ``network.WLAN``.
"""

from common import build_runtime, default_tasks

from SmallPackage.Kernel import ESP32


WIFI_SSID = None
WIFI_PASSWORD = None
WIFI_HOSTNAME = "smallos-esp32"


def maybe_connect_wifi(kernel):
    """Connect only when credentials are filled in for the target board."""
    if WIFI_SSID and WIFI_PASSWORD:
        kernel.connect_wifi(WIFI_SSID, WIFI_PASSWORD)


def main():
    kernel = ESP32(hostname=WIFI_HOSTNAME)
    maybe_connect_wifi(kernel)

    runtime = build_runtime(kernel)
    runtime.fork(default_tasks(kernel.board_name))
    runtime.startOS()


if __name__ == "__main__":
    main()
