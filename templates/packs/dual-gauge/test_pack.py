import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import pack


class FixtureTree:
    def __init__(self, root: Path):
        self.root = root

    def hwmon(self, index: int, name: str, device: str, temperatures: list[tuple[str, str]]):
        directory = self.root / "class" / "hwmon" / f"hwmon{index}"
        directory.mkdir(parents=True)
        (directory / "name").write_text(f"{name}\n", encoding="utf-8")
        target = self.root / "devices" / device
        target.mkdir(parents=True, exist_ok=True)
        (directory / "device").symlink_to(target)
        for sensor, label in temperatures:
            (directory / f"{sensor}_input").write_text("42000\n", encoding="utf-8")
            (directory / f"{sensor}_label").write_text(f"{label}\n", encoding="utf-8")

    def amd_card(self, index: int, device: str):
        directory = self.root / "class" / "drm" / f"card{index}"
        directory.mkdir(parents=True)
        target = self.root / "devices" / device
        target.mkdir(parents=True, exist_ok=True)
        (target / "vendor").write_text("0x1002\n", encoding="utf-8")
        (target / "gpu_busy_percent").write_text("50\n", encoding="utf-8")
        (directory / "device").symlink_to(target)


class PackTests(unittest.TestCase):
    def complete_machine(self, root: Path):
        sysfs = root / "sys"
        tree = FixtureTree(sysfs)
        tree.hwmon(0, "k10temp", "platform:k10temp", [("temp1", "Tctl")])
        tree.hwmon(1, "amdgpu", "0000:03:00.0", [("temp1", "edge")])
        tree.amd_card(0, "0000:03:00.0")
        meminfo = root / "proc" / "meminfo"
        meminfo.parent.mkdir()
        meminfo.write_text("MemTotal: 33554432 kB\nMemAvailable: 10485760 kB\n", encoding="utf-8")
        config = root / "config"
        config.mkdir()
        return config, sysfs, meminfo

    @property
    def asset_root(self):
        return Path(__file__).resolve().parents[2] / "assets"

    def test_config_directory_resolution(self):
        self.assertEqual(
            pack.resolve_config_dir("user", None, {"HOME": "/home/test", "XDG_CONFIG_HOME": "/state"}),
            Path("/state/lianli"),
        )
        self.assertEqual(
            pack.resolve_config_dir("user", None, {"HOME": "/home/test"}),
            Path("/home/test/.config/lianli"),
        )
        self.assertEqual(
            pack.resolve_config_dir("system", None, {}),
            Path("/var/lib/lianli"),
        )
        self.assertEqual(
            pack.resolve_config_dir("system", Path("/box/state"), {}),
            Path("/box/state"),
        )
        with self.assertRaises(pack.PackError):
            pack.resolve_config_dir("user", None, {"HOME": "/home/test", "XDG_CONFIG_HOME": "relative"})

    def test_sensor_detection_prefers_cpu_control_temperature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = FixtureTree(root)
            tree.hwmon(0, "k10temp", "platform:k10temp", [("temp1", "Tdie"), ("temp2", "Tctl")])
            tree.hwmon(1, "amdgpu", "0000:03:00.0", [("temp1", "edge")])
            tree.amd_card(0, "0000:03:00.0")

            bindings = pack.detect_bindings(root)

        self.assertEqual(
            bindings["cpu_temp"],
            {"type": "hwmon", "name": "k10temp", "label": "temp2", "device_path": "platform:k10temp"},
        )
        self.assertEqual(bindings["gpu_usage"], {"type": "amd_gpu_usage", "card_index": 0})
        self.assertEqual(bindings["gpu_temp"]["device_path"], "0000:03:00.0")

    def test_multiple_amd_cards_bind_temperature_to_usage_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = FixtureTree(root)
            tree.hwmon(0, "k10temp", "platform:k10temp", [("temp1", "Tctl")])
            tree.hwmon(1, "amdgpu", "0000:03:00.0", [("temp1", "edge")])
            tree.hwmon(2, "amdgpu", "0000:04:00.0", [("temp2", "edge")])
            tree.amd_card(0, "0000:04:00.0")
            tree.amd_card(1, "0000:03:00.0")

            bindings = pack.detect_bindings(root)

        self.assertEqual(bindings["gpu_usage"], {"type": "amd_gpu_usage", "card_index": 0})
        self.assertEqual(bindings["gpu_temp"]["label"], "temp2")
        self.assertEqual(bindings["gpu_temp"]["device_path"], "0000:04:00.0")

    def test_missing_required_sensor_categories_are_reported_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "class" / "hwmon").mkdir(parents=True)
            (root / "class" / "drm").mkdir(parents=True)

            with self.assertRaisesRegex(pack.PackError, "cpu_temp.*gpu_temp.*gpu_usage"):
                pack.detect_bindings(root)

    def test_total_memory_is_read_as_gibibytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "meminfo"
            path.write_text("MemTotal:       33554432 kB\nMemAvailable:   10485760 kB\n", encoding="utf-8")
            self.assertEqual(pack.read_total_memory_gib(path), 32.0)
            path.write_text("MemTotal:       31953748 kB\n", encoding="utf-8")
            self.assertEqual(pack.read_total_memory_gib(path), 32.0)
            path.write_text("MemAvailable: 1 kB\n", encoding="utf-8")
            with self.assertRaises(pack.PackError):
                pack.read_total_memory_gib(path)

    def test_templates_are_transformed_for_the_official_schema(self):
        font_path = Path("/state/template-packs/dual-gauge/NotoSans-SemiBold.ttf")
        bindings = {
            "cpu_temp": {
                "type": "hwmon",
                "name": "k10temp",
                "label": "temp2",
                "device_path": "platform:k10temp",
            },
            "gpu_temp": {
                "type": "hwmon",
                "name": "amdgpu",
                "label": "temp1",
                "device_path": "0000:03:00.0",
            },
            "gpu_usage": {"type": "amd_gpu_usage", "card_index": 1},
        }

        templates = pack.prepare_templates(self.asset_root, font_path, bindings, 32.0)

        self.assertEqual([template["id"] for template in templates], [
            "dual-gauge-cpu",
            "dual-gauge-gpu",
            "dual-gauge-ram",
        ])
        for template in templates:
            self.assertNotIn("smooth_edges", template)
            for widget in template["widgets"]:
                self.assertNotIn("sensor_category", widget)
                font = widget["kind"].get("font")
                if font is not None:
                    self.assertEqual(font["path"], str(font_path))

        cpu, gpu, ram = templates
        self.assertEqual(cpu["widgets"][0]["width"], 380)
        self.assertEqual(cpu["widgets"][1]["width"], 260)
        self.assertEqual(cpu["widgets"][0]["kind"]["ranges"][0]["color"], [32, 214, 176])
        self.assertEqual(cpu["widgets"][0]["kind"]["source"], bindings["cpu_temp"])
        self.assertEqual(cpu["widgets"][3]["kind"]["source"], bindings["cpu_temp"])
        self.assertEqual(gpu["widgets"][0]["kind"]["source"], bindings["gpu_temp"])
        self.assertEqual(gpu["widgets"][1]["kind"]["source"], bindings["gpu_usage"])
        self.assertEqual(ram["widgets"][0]["kind"]["source"], {"type": "mem_usage"})
        total = next(widget for widget in ram["widgets"] if widget["id"] == "memory-total-value")
        self.assertEqual(total["kind"]["source"], {"type": "constant", "value": 32.0})
        self.assertEqual(total["update_interval_ms"], 10_000)
        self.assertNotIn('"mem_total"', json.dumps(templates))

    def test_reinstall_accepts_defaults_materialized_by_official_serialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)
            state_path = config / "lcd_templates.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))

            for template in state["templates"]:
                template.setdefault("rotated", False)
                template.setdefault("target_device", None)
                for widget in template["widgets"]:
                    widget.setdefault("rotation", 0.0)
                    widget.setdefault("visible", True)
                    widget.setdefault("update_interval_ms", None)
                    widget.setdefault("fps", None)
                    kind = widget["kind"]
                    if kind["type"] == "label":
                        kind.setdefault("align", "center")
                        kind.setdefault("letter_spacing", 0.0)
                    elif kind["type"] == "value_text":
                        kind.setdefault("format", "{:.1}")
                        kind.setdefault("unit", "")
                        kind.setdefault("align", "center")
                        kind.setdefault("value_min", 0.0)
                        kind.setdefault("value_max", 100.0)
                        kind.setdefault("ranges", [])
                        kind.setdefault("letter_spacing", 0.0)
                    elif kind["type"] == "radial_gauge":
                        kind.setdefault("inner_radius_pct", 0.75)
                        kind.setdefault("ranges", [])
                        kind.setdefault("bg_corner_radius", 0.0)
                        kind.setdefault("value_corner_radius", 0.0)
                        kind.setdefault("gradient", False)
                        kind.setdefault("gradient_stops", [
                            {"position": 0.0, "color": [45, 110, 255], "alpha": 255},
                            {"position": 50.0, "color": [170, 80, 255], "alpha": 255},
                            {"position": 100.0, "color": [255, 80, 190], "alpha": 255},
                        ])
            state_path.write_text(json.dumps(state), encoding="utf-8")

            result = pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

            self.assertFalse(result.changed)

    def test_final_template_count_is_checked_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            state_path = config / "lcd_templates.json"
            previous = {
                "templates": [{"id": f"existing-{index}"} for index in range(1022)]
            }
            original = json.dumps(previous).encode()
            state_path.write_bytes(original)

            with self.assertRaisesRegex(pack.PackError, "more than 1024"):
                pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

            self.assertEqual(state_path.read_bytes(), original)
            self.assertFalse((config / "template-packs").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_nonregular_state_file_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "lcd_templates.json"
            os.mkfifo(state_path)
            script = (
                "from pathlib import Path; import sys; "
                f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r}); "
                "import pack; "
                f"path = Path({str(state_path)!r}); "
                "\ntry:\n pack._load_state(path)\n"
                "except pack.PackError as error:\n"
                " assert 'regular file' in str(error)\n"
                "else:\n raise SystemExit('FIFO was accepted')\n"
            )
            try:
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail("opening a FIFO blocked instead of rejecting it")

            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_temporary_path_replacement_cannot_chmod_an_outside_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            outside = root / "outside"
            outside.write_bytes(b"private")
            outside.chmod(0o600)
            real_chmod = os.chmod
            replaced = False

            def replace_before_chmod(path, mode, *args, **kwargs):
                nonlocal replaced
                if not replaced and Path(path).name.startswith(".state.json."):
                    replaced = True
                    Path(path).unlink()
                    Path(path).symlink_to(outside)
                return real_chmod(path, mode, *args, **kwargs)

            with mock.patch.object(pack.os, "chmod", side_effect=replace_before_chmod):
                pack._atomic_write(target, b"new", 0o644)

            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o600)
            self.assertTrue(target.is_file())
            self.assertFalse(target.is_symlink())

    def test_directory_swap_cannot_publish_assets_outside_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            outside = root / "outside"
            outside.mkdir()
            held = config / "template-packs-held"
            real_mkdir = os.mkdir
            swapped = False

            def swap_after_mkdir(path, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                result = real_mkdir(path, mode, dir_fd=dir_fd)
                candidate = os.fspath(path)
                if not swapped and (
                    candidate == "template-packs"
                    or Path(candidate) == config / "template-packs"
                ):
                    swapped = True
                    (config / "template-packs").rename(held)
                    (config / "template-packs").symlink_to(outside, target_is_directory=True)
                return result

            with mock.patch.object(pack.os, "mkdir", side_effect=swap_after_mkdir):
                with self.assertRaises(pack.PackError):
                    pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

            self.assertEqual(list(outside.iterdir()), [])

    def test_directory_sync_failure_after_state_rename_restores_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            state_path = config / "lcd_templates.json"
            original = b'{"templates": [], "keep": true}'
            state_path.write_bytes(original)
            config_stat = config.stat()
            real_fsync = os.fsync
            failed = False

            def fail_config_sync(descriptor):
                nonlocal failed
                metadata = os.fstat(descriptor)
                is_config = (metadata.st_dev, metadata.st_ino) == (
                    config_stat.st_dev,
                    config_stat.st_ino,
                )
                if is_config and stat.S_ISDIR(metadata.st_mode) and not failed:
                    failed = True
                    raise OSError("fixture directory sync failure")
                return real_fsync(descriptor)

            with mock.patch.object(pack.os, "fsync", side_effect=fail_config_sync):
                with self.assertRaisesRegex(pack.PackError, "directory sync failure"):
                    pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

            self.assertTrue(failed)
            self.assertEqual(state_path.read_bytes(), original)

    def test_install_creates_state_and_preserves_unrelated_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            state_path = config / "lcd_templates.json"
            previous = {
                "future": {"keep": True},
                "templates": [{
                    "id": "unrelated",
                    "name": "Keep",
                    "base_width": 1,
                    "base_height": 1,
                    "background": {"type": "color", "rgb": [0, 0, 0]},
                    "widgets": [],
                }],
            }
            state_path.write_text(json.dumps(previous), encoding="utf-8")

            result = pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)
            installed = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertTrue(result.changed)
            self.assertEqual(result.installed_ids, ("dual-gauge-cpu", "dual-gauge-gpu", "dual-gauge-ram"))
            self.assertIsNotNone(result.backup_path)
            self.assertEqual(json.loads(result.backup_path.read_text(encoding="utf-8")), previous)
            self.assertEqual(installed["future"], {"keep": True})
            self.assertEqual([item["id"] for item in installed["templates"]], [
                "unrelated",
                "dual-gauge-cpu",
                "dual-gauge-gpu",
                "dual-gauge-ram",
            ])
            font = config / "template-packs" / "dual-gauge" / "NotoSans-SemiBold.ttf"
            self.assertTrue(font.is_file())
            self.assertEqual(installed["templates"][1]["widgets"][2]["kind"]["font"]["path"], str(font))

    def test_install_is_idempotent_and_missing_state_needs_no_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)

            first = pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)
            before = (config / "lcd_templates.json").read_bytes()
            second = pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

            self.assertTrue(first.changed)
            self.assertIsNone(first.backup_path)
            self.assertFalse(second.changed)
            self.assertIsNone(second.backup_path)
            self.assertEqual((config / "lcd_templates.json").read_bytes(), before)

    def test_conflicting_pack_id_requires_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            state_path = config / "lcd_templates.json"
            previous = {"templates": [{"id": "dual-gauge-cpu", "name": "User edit"}]}
            original = json.dumps(previous).encode()
            state_path.write_bytes(original)

            with self.assertRaisesRegex(pack.PackError, "dual-gauge-cpu.*--replace"):
                pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

            self.assertEqual(state_path.read_bytes(), original)
            self.assertFalse((config / "template-packs").exists())
            result = pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=True)
            self.assertTrue(result.changed)
            installed = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(installed["templates"][0]["name"], "Dual Gauge CPU")

    def test_invalid_and_oversized_state_are_never_replaced(self):
        for content in (b"{", json.dumps({"templates": "invalid"}).encode(), b" " * (16 * 1024 * 1024 + 1)):
            with self.subTest(size=len(content)):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    config, sysfs, meminfo = self.complete_machine(root)
                    state_path = config / "lcd_templates.json"
                    state_path.write_bytes(content)

                    with self.assertRaises(pack.PackError):
                        pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

                    self.assertEqual(state_path.read_bytes(), content)

    def test_symlinked_config_root_and_state_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            link = root / "config-link"
            link.symlink_to(config, target_is_directory=True)

            with self.assertRaisesRegex(pack.PackError, "symlink"):
                pack.install_pack(link, self.asset_root, sysfs, meminfo, replace=False)

            self.assertFalse((config / "lcd_templates.json").exists())
            outside = root / "outside.json"
            outside.write_text('{"templates": []}', encoding="utf-8")
            (config / "lcd_templates.json").symlink_to(outside)
            with self.assertRaisesRegex(pack.PackError, "symlink"):
                pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)
            self.assertEqual(outside.read_text(encoding="utf-8"), '{"templates": []}')

            (config / "lcd_templates.json").unlink()
            outside_assets = root / "outside-assets"
            outside_assets.mkdir()
            (config / "template-packs").symlink_to(outside_assets, target_is_directory=True)
            with self.assertRaisesRegex(pack.PackError, "symlink"):
                pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)
            self.assertEqual(list(outside_assets.iterdir()), [])

    def test_existing_state_mode_and_configuration_owner_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            state = config / "lcd_templates.json"
            state.write_text('{"templates": []}', encoding="utf-8")
            state.chmod(0o640)

            pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

            config_stat = config.stat()
            state_stat = state.stat()
            font_stat = (config / "template-packs" / "dual-gauge" / "NotoSans-SemiBold.ttf").stat()
            self.assertEqual(state_stat.st_mode & 0o777, 0o640)
            self.assertEqual((state_stat.st_uid, state_stat.st_gid), (config_stat.st_uid, config_stat.st_gid))
            self.assertEqual((font_stat.st_uid, font_stat.st_gid), (config_stat.st_uid, config_stat.st_gid))

    def test_state_publication_failure_preserves_original_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, sysfs, meminfo = self.complete_machine(root)
            state = config / "lcd_templates.json"
            original = b'{"templates": [], "keep": true}'
            state.write_bytes(original)
            real_atomic_write_at = pack._atomic_write_at

            def fail_state(directory, name, data, mode, owner=None):
                if name == state.name:
                    raise pack._AtomicWriteFailure(OSError("fixture publication failure"), False)
                return real_atomic_write_at(directory, name, data, mode, owner)

            with mock.patch.object(pack, "_atomic_write_at", side_effect=fail_state):
                with self.assertRaisesRegex(pack.PackError, "fixture publication failure"):
                    pack.install_pack(config, self.asset_root, sysfs, meminfo, replace=False)

            self.assertEqual(state.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
