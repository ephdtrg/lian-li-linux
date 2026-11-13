import argparse
from pathlib import Path
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import install
import pack


class FakeService:
    def __init__(self, active=False, stop_error=None, start_error=None, events=None):
        self.active = active
        self.stop_error = stop_error
        self.start_error = start_error
        self.events = events if events is not None else []

    def is_active(self):
        self.events.append("is_active")
        return self.active

    def stop(self):
        self.events.append("stop")
        if self.stop_error:
            raise self.stop_error

    def start(self):
        self.events.append("start")
        if self.start_error:
            raise self.start_error


def arguments(**overrides):
    values = {
        "mode": "user",
        "config_dir": Path("/state"),
        "replace": False,
        "no_service": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class InstallTests(unittest.TestCase):
    def result(self):
        return pack.InstallResult(pack.PACK_IDS, None, True)

    def test_inactive_service_is_not_stopped_or_started(self):
        events = []
        service = FakeService(events=events)

        def installer(*args, **kwargs):
            events.append("install")
            return self.result()

        with mock.patch.object(install.pack, "install_pack", side_effect=installer):
            result = install.run_install(arguments(), service)

        self.assertTrue(result.changed)
        self.assertEqual(events, ["is_active", "install"])

    def test_active_service_is_stopped_then_restarted(self):
        events = []
        service = FakeService(active=True, events=events)

        def installer(*args, **kwargs):
            events.append("install")
            return self.result()

        with mock.patch.object(install.pack, "install_pack", side_effect=installer):
            install.run_install(arguments(), service)

        self.assertEqual(events, ["is_active", "stop", "install", "start"])

    def test_stop_failure_aborts_before_install(self):
        events = []
        service = FakeService(active=True, stop_error=pack.PackError("stop failed"), events=events)
        with mock.patch.object(install.pack, "install_pack") as installer:
            with self.assertRaisesRegex(pack.PackError, "stop failed"):
                install.run_install(arguments(), service)

        installer.assert_not_called()
        self.assertEqual(events, ["is_active", "stop"])

    def test_install_failure_still_restarts_previously_active_service(self):
        events = []
        service = FakeService(active=True, events=events)

        def installer(*args, **kwargs):
            events.append("install")
            raise pack.PackError("install failed")

        with mock.patch.object(install.pack, "install_pack", side_effect=installer):
            with self.assertRaisesRegex(pack.PackError, "install failed"):
                install.run_install(arguments(), service)

        self.assertEqual(events, ["is_active", "stop", "install", "start"])

    def test_restart_failure_is_reported_without_hiding_install_failure(self):
        events = []
        service = FakeService(
            active=True,
            start_error=pack.PackError("restart failed"),
            events=events,
        )

        def installer(*args, **kwargs):
            events.append("install")
            raise pack.PackError("install failed")

        with mock.patch.object(install.pack, "install_pack", side_effect=installer):
            with self.assertRaisesRegex(pack.PackError, "install failed.*restart failed"):
                install.run_install(arguments(), service)

        self.assertEqual(events, ["is_active", "stop", "install", "start"])

    def test_system_install_requires_root_before_service_or_filesystem_work(self):
        service = FakeService(active=True)
        with mock.patch.object(install.os, "geteuid", return_value=1000):
            with mock.patch.object(install.pack, "install_pack") as installer:
                with self.assertRaisesRegex(pack.PackError, "root"):
                    install.run_install(arguments(mode="system"), service)

        installer.assert_not_called()
        self.assertEqual(service.events, [])

    def test_no_service_mode_skips_systemd(self):
        service = FakeService(active=True)
        with mock.patch.object(install.pack, "install_pack", return_value=self.result()):
            install.run_install(arguments(no_service=True), service)
        self.assertEqual(service.events, [])


if __name__ == "__main__":
    unittest.main()
