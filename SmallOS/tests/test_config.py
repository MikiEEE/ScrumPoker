import json
import os
import sys
import tempfile
import unittest

sys.path.append("..")

from SmallPackage.SmallConfig import SmallOSConfig
from SmallPackage.SmallOS import SmallOS


class TestSmallOSConfig(unittest.TestCase):
    def test_from_dict_accepts_project_aliases(self):
        config = SmallOSConfig.from_dict(
            {
                "oslist_length": 64,
                "num_categories": 7,
                "io_buffer_length": 12,
                "eternal_watchers": True,
            }
        )

        self.assertEqual(64, config.task_capacity)
        self.assertEqual(7, config.priority_levels)
        self.assertEqual(12, config.io_buffer_length)
        self.assertTrue(config.eternal_watchers)

    def test_from_json_file_loads_canonical_keys(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            json.dump(
                {
                    "task_capacity": 128,
                    "priority_levels": 9,
                    "io_buffer_length": 32,
                    "eternal_watchers": False,
                    "client_defaults": {
                        "stream": {"max_buffer_size": 4096},
                        "mqtt": {"keepalive": 45},
                        "sse": {"max_event_size": 2048},
                        "websocket": {"max_frame_size": 8192},
                    },
                },
                handle,
            )
            path = handle.name

        try:
            config = SmallOSConfig.from_json_file(path)
        finally:
            os.unlink(path)

        self.assertEqual(128, config.task_capacity)
        self.assertEqual(9, config.priority_levels)
        self.assertEqual(32, config.io_buffer_length)
        self.assertFalse(config.eternal_watchers)
        self.assertEqual(4096, config.client_defaults["stream"]["max_buffer_size"])
        self.assertEqual(45, config.client_defaults["mqtt"]["keepalive"])
        self.assertEqual(2048, config.client_defaults["sse"]["max_event_size"])
        self.assertEqual(8192, config.client_defaults["websocket"]["max_frame_size"])

    def test_smallos_uses_config_for_runtime_sizing(self):
        config = SmallOSConfig(
            task_capacity=48,
            priority_levels=6,
            io_buffer_length=20,
            eternal_watchers=True,
        )

        runtime = SmallOS(config=config)

        self.assertEqual(48, runtime.tasks.maxPID)
        self.assertEqual(6, runtime.tasks.num_priorities)
        self.assertEqual(20, runtime.buffer_length)
        self.assertTrue(runtime.eternalWatchers)

    def test_client_defaults_merge_stream_and_protocol_sections(self):
        config = SmallOSConfig.from_dict(
            {
                "client_defaults": {
                    "stream": {"max_buffer_size": 2048},
                    "http": {"max_response_size": 512},
                    "mqtt": {"keepalive": 15},
                }
            }
        )

        http_defaults = config.client_defaults_for("http")
        mqtt_defaults = config.client_defaults_for("mqtt")

        self.assertEqual(2048, http_defaults["max_buffer_size"])
        self.assertEqual(512, http_defaults["max_response_size"])
        self.assertEqual(2048, mqtt_defaults["max_buffer_size"])
        self.assertEqual(15, mqtt_defaults["keepalive"])

    def test_copy_preserves_nested_client_defaults(self):
        config = SmallOSConfig.from_dict(
            {
                "client_defaults": {
                    "stream": {"max_buffer_size": 8192},
                    "redis": {"max_nesting_depth": 12},
                }
            }
        )

        cloned = config.copy(priority_levels=8)

        self.assertEqual(8, cloned.priority_levels)
        self.assertEqual(8192, cloned.client_defaults["stream"]["max_buffer_size"])
        self.assertEqual(12, cloned.client_defaults["redis"]["max_nesting_depth"])


if __name__ == "__main__":
    unittest.main()
