"""MicroPython demo that picks a built-in board profile from the machine string."""

from common import build_runtime, default_tasks

from SmallPackage.Kernel import build_micropython_kernel


WIFI_SSID = None
WIFI_PASSWORD = None


def maybe_connect_wifi(kernel):
    """Use Wi-Fi only when the kernel supports it and credentials are provided."""
    if WIFI_SSID and WIFI_PASSWORD and hasattr(kernel, "connect_wifi"):
        kernel.connect_wifi(WIFI_SSID, WIFI_PASSWORD)


def main():
    kernel = build_micropython_kernel()
    maybe_connect_wifi(kernel)

    runtime = build_runtime(kernel)
    runtime.fork(default_tasks(getattr(kernel, "board_name", "MicroPython")))
    runtime.startOS()


if __name__ == "__main__":
    main()
