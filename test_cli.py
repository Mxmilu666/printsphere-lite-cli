import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import cli
import profiles
from platform_support import _split_nmcli, render_alias_bitmap


class ProfileBehavior(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "config.json")
        self.patch = patch.object(profiles, "CONFIG_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def status(self, identity, **extra):
        return {"ok": True, "device_id": identity, "ip": "", "mqtt_connected": False, **extra}

    def test_legacy_migration_then_new_device_inherits_only_wifi(self):
        legacy = copy.deepcopy(profiles.DEFAULT_CONFIG)
        legacy.pop("esp_devices")
        legacy.pop("active_esp_id")
        legacy["wifi"] = {"ssid": "old", "password": "secret"}
        legacy["printer"]["serial"] = "printer-a"
        legacy["display"].update(brightness=37, layout="dashboard", dimmed=True)
        profiles.write_json(self.path, legacy)
        first = profiles.apply_status_to_config(self.status("esp-a"), "/dev/a")
        self.assertEqual(first["printer"]["serial"], "printer-a")
        self.assertEqual(first["display"]["brightness"], 37)
        self.assertNotIn("dimmed", first["display"])
        second = profiles.apply_status_to_config(self.status("esp-b"), "/dev/b")
        self.assertEqual(second["wifi"], legacy["wifi"])
        self.assertEqual(second["printer"]["serial"], "")
        self.assertEqual(second["display"]["layout"], "classic")
        restored = profiles.activate_esp_profile("esp-a")
        self.assertEqual(restored["display"]["layout"], "dashboard")
        self.assertEqual(restored["printer"]["serial"], "printer-a")

    def test_switching_profiles_preserves_independent_display_and_wifi(self):
        for identity, brightness, ssid in (("a", 0, "wifi-a"), ("b", 63, "wifi-b")):
            cfg = profiles.apply_status_to_config(self.status(identity), "/dev/" + identity)
            cfg["display"].update(brightness=brightness, layout="dashboard" if identity == "a" else "classic")
            cfg["wifi"] = {"ssid": ssid, "password": identity}
            profiles.save_config(cfg)
        selected = profiles.activate_esp_profile("a")
        self.assertEqual(selected["display"]["brightness"], 0)
        self.assertEqual(selected["display"]["layout"], "dashboard")
        self.assertEqual(selected["wifi"]["ssid"], "wifi-a")
        self.assertEqual(profiles.activate_esp_profile("b")["display"]["brightness"], 63)

    def test_explicit_empty_ip_invalidates_previous_address(self):
        profiles.apply_status_to_config(self.status("a", ip="192.168.1.22"), "/dev/a")
        cfg = profiles.apply_status_to_config(self.status("a"), "/dev/a")
        self.assertEqual((cfg["esp"]["host"], cfg["esp"]["last_ip"], cfg["esp"]["wifi_status"]), ("", "", "unknown"))

    def test_failed_atomic_replace_does_not_destroy_saved_credentials(self):
        profiles.write_json(self.path, {"password": "original"})
        with patch.object(profiles.os, "replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                profiles.write_json(self.path, {"password": "replacement"})
        self.assertEqual(profiles.read_json(self.path, {}), {"password": "original"})

    def test_brightness_boundaries_rounding_and_nonfinite(self):
        for value, expected in ((0, 0), (37, 37), (37.5, 38), (-2, 0), (101, 100), (float("nan"), 100)):
            with self.subTest(value=value):
                self.assertEqual(profiles.normalize_brightness(value), expected)

    def test_device_swap_blocks_write_without_changing_target_profile(self):
        cfg = profiles.apply_status_to_config(self.status("a"), "/dev/a")
        with patch.object(cli, "list_serial_ports", return_value=[{"port": "/dev/a"}]), \
             patch.object(cli, "serial_json_command", return_value={"ok": True, "json": self.status("b")}), \
             patch.object(cli, "write_esp_payload") as write:
            with self.assertRaisesRegex(RuntimeError, "身份"):
                cli.push_display_to_esp(cfg)
            write.assert_not_called()
        self.assertEqual(profiles.read_config()["active_esp_id"], "a")

    def test_multiple_connected_devices_block_write(self):
        cfg = profiles.apply_status_to_config(self.status("a"), "/dev/a")
        with patch.object(cli, "list_serial_ports", return_value=[{"port": "/dev/a"}, {"port": "/dev/b"}]), \
             patch.object(cli, "serial_json_command", side_effect=[{"ok": True, "json": self.status("a")}, {"ok": True, "json": self.status("b")}]), \
             patch.object(cli, "write_esp_payload") as write:
            with self.assertRaisesRegex(RuntimeError, "多台"):
                cli.push_display_to_esp(cfg)
            write.assert_not_called()

    def test_http_wrong_identity_blocks_fallback(self):
        cfg = profiles.apply_status_to_config(self.status("a", ip="192.168.1.22"), "/dev/a")
        with patch.object(cli, "list_serial_ports", return_value=[]), \
             patch.object(cli, "local_request", return_value={"status": 200, "json": self.status("b")}), \
             patch.object(cli, "write_esp_payload") as write:
            with self.assertRaisesRegex(RuntimeError, "身份"):
                cli.push_display_to_esp(cfg)
            write.assert_not_called()

    def test_screen_input_and_alias_clear_reach_http_query(self):
        from urllib.parse import parse_qs, urlsplit
        cfg = profiles.apply_status_to_config(self.status("a", ip="192.168.1.22"), "/dev/a")
        cfg["display"].update(brightness=0, alias="", alias_bitmap_hex="", alias_bitmap_w=0)
        query = parse_qs(urlsplit(cli.esp_config_url(cli.display_payload(cfg), cfg)).query, keep_blank_values=True)
        self.assertEqual(query["brightness"], ["0"])
        self.assertEqual(query["alias"], [""])
        self.assertEqual(query["alias_bitmap_hex"], [""])

    def test_nmcli_preserves_escaped_ssid(self):
        self.assertEqual(_split_nmcli(r"Lab\:Printer\\WiFi:82"), ["Lab:Printer\\WiFi", "82"])


if __name__ == "__main__":
    unittest.main()
