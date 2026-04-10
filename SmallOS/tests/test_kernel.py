import sys
import unittest

sys.path.append("..")

from SmallPackage.Kernel import ESP32, ESP8266, MicroPythonKernel, PicoW, RaspberryPiPicoW, Unix, build_micropython_kernel


class FakeNIC:
    def __init__(self):
        self.active_calls = []
        self.connect_calls = []
        self.config_calls = []
        self.connected = False

    def active(self, flag):
        self.active_calls.append(flag)

    def isconnected(self):
        return self.connected

    def connect(self, ssid, password):
        self.connect_calls.append((ssid, password))
        self.connected = True

    def config(self, **kwargs):
        self.config_calls.append(kwargs)


class FakeNetworkModule:
    STA_IF = object()

    def __init__(self, nic):
        self.nic = nic
        self.hostname_calls = []
        self.wlan_modes = []

    def WLAN(self, mode):
        self.wlan_modes.append(mode)
        return self.nic

    def hostname(self, value):
        self.hostname_calls.append(value)


class FakeRP2Module:
    def __init__(self):
        self.country_calls = []

    def country(self, value):
        self.country_calls.append(value)


class FakePollObject:
    def __init__(self, fd):
        self.fd = fd

    def fileno(self):
        return self.fd


class FakePoller:
    def __init__(self, events):
        self.events = events
        self.registrations = []

    def register(self, obj, mask):
        self.registrations.append((obj, mask))

    def poll(self, timeout):
        return list(self.events)


class TestKernelProfiles(unittest.TestCase):
    def test_build_micropython_kernel_detects_esp32_profile(self):
        kernel = build_micropython_kernel(machine_name="ESP32 module with ESP32")

        self.assertIsInstance(kernel, ESP32)
        self.assertEqual("ESP32", kernel.board_name)

    def test_build_micropython_kernel_detects_pico_w_profile(self):
        kernel = build_micropython_kernel(machine_name="Raspberry Pi Pico W with RP2040")

        self.assertIsInstance(kernel, PicoW)
        self.assertEqual("Raspberry Pi Pico W", kernel.board_name)

    def test_build_micropython_kernel_falls_back_to_generic_profile(self):
        kernel = build_micropython_kernel(machine_name="PYBD-SF2")

        self.assertIsInstance(kernel, MicroPythonKernel)
        self.assertNotIsInstance(kernel, ESP32)
        self.assertNotIsInstance(kernel, PicoW)

    def test_esp8266_profile_is_still_available(self):
        kernel = build_micropython_kernel(machine_name="ESP8266 board")

        self.assertIsInstance(kernel, ESP8266)
        self.assertEqual("ESP8266", kernel.board_name)

    def test_pico_w_alias_points_to_same_profile(self):
        self.assertIs(RaspberryPiPicoW, PicoW)

    def test_esp32_connect_wifi_applies_hostname(self):
        nic = FakeNIC()
        network_mod = FakeNetworkModule(nic)
        kernel = ESP32(
            hostname="smallos-esp32",
            modules={"network": network_mod},
        )

        connected_nic = kernel.connect_wifi("ssid", "password")

        self.assertIs(connected_nic, nic)
        self.assertEqual([True], nic.active_calls)
        self.assertEqual([("ssid", "password")], nic.connect_calls)
        self.assertEqual(["smallos-esp32"], network_mod.hostname_calls)

    def test_pico_w_connect_wifi_applies_country_hostname_and_power_mode(self):
        nic = FakeNIC()
        network_mod = FakeNetworkModule(nic)
        rp2_mod = FakeRP2Module()
        kernel = PicoW(
            country="US",
            hostname="smallos-pico",
            power_management=0xA11140,
            modules={"network": network_mod, "rp2": rp2_mod},
        )

        connected_nic = kernel.connect_wifi("ssid", "password")

        self.assertIs(connected_nic, nic)
        self.assertEqual([True], nic.active_calls)
        self.assertEqual([("ssid", "password")], nic.connect_calls)
        self.assertEqual(["US"], rp2_mod.country_calls)
        self.assertEqual(["smallos-pico"], network_mod.hostname_calls)
        self.assertIn({"pm": 0xA11140}, nic.config_calls)

    def test_unix_io_wait_maps_poll_file_descriptors_back_to_objects(self):
        readable_obj = FakePollObject(11)
        writable_obj = FakePollObject(22)
        poller = FakePoller([(11, 0x001), (22, 0x004)])

        kernel = Unix()
        kernel._poll_factory = lambda: poller

        readable, writable = kernel.io_wait([readable_obj], [writable_obj], timeout_ms=5)

        self.assertEqual([readable_obj], readable)
        self.assertEqual([writable_obj], writable)


if __name__ == "__main__":
    unittest.main()
