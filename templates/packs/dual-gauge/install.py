#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

import pack


class ServiceController:
    def __init__(self, mode: str):
        self.mode = mode
        self.unit = "lianli-daemon-system.service" if mode == "system" else "lianli-daemon.service"

    def _command(self, action: str, check: bool = True) -> subprocess.CompletedProcess:
        command = ["systemctl"]
        if self.mode == "user":
            command.append("--user")
        command.extend([action, self.unit])
        try:
            return subprocess.run(command, check=check, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            detail = getattr(error, "stderr", "") or str(error)
            raise pack.PackError(f"systemctl {action} {self.unit} failed: {detail.strip()}") from error

    def is_active(self) -> bool:
        result = self._command("is-active", check=False)
        if result.returncode == 0:
            return True
        if result.returncode in (3, 4):
            return False
        raise pack.PackError(
            f"cannot determine whether {self.unit} is active: {result.stderr.strip()}"
        )

    def stop(self) -> None:
        self._command("stop")

    def start(self) -> None:
        self._command("start")


def run_install(args: argparse.Namespace, service: ServiceController) -> pack.InstallResult:
    if args.mode == "system" and os.geteuid() != 0:
        raise pack.PackError("system installation must run as root")
    config_dir = pack.resolve_config_dir(args.mode, args.config_dir, os.environ)
    asset_root = Path(__file__).resolve().parents[2] / "assets"
    stopped = False
    if not args.no_service:
        active = service.is_active()
        if active:
            service.stop()
            stopped = True

    result = None
    failure: BaseException | None = None
    try:
        result = pack.install_pack(
            config_dir,
            asset_root,
            Path("/sys"),
            Path("/proc/meminfo"),
            replace=args.replace,
        )
    except BaseException as error:
        failure = error

    if stopped:
        try:
            service.start()
        except Exception as restart_error:
            if failure is not None:
                unit = getattr(service, "unit", "daemon service")
                raise pack.PackError(
                    f"{failure}; additionally failed to restart {unit}: {restart_error}"
                ) from failure
            raise
    if failure is not None:
        raise failure
    assert result is not None
    return result


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Install Dual Gauge CPU, GPU and RAM into an official Lian Li Linux configuration.",
    )
    modes = argument_parser.add_mutually_exclusive_group()
    modes.add_argument("--user", dest="mode", action="store_const", const="user", help="target the user daemon (default)")
    modes.add_argument("--system", dest="mode", action="store_const", const="system", help="target the system daemon; run as root")
    argument_parser.set_defaults(mode="user")
    argument_parser.add_argument("--config-dir", type=Path, help="override the Lian Li Linux configuration directory")
    argument_parser.add_argument("--replace", action="store_true", help="replace conflicting Dual Gauge template IDs")
    argument_parser.add_argument("--no-service", action="store_true", help="do not inspect or stop systemd; the daemon must already be stopped")
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run_install(args, ServiceController(args.mode))
    except pack.PackError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if result.changed:
        print("Installed: " + ", ".join(result.installed_ids))
    else:
        print("Dual Gauge templates are already installed and unchanged.")
    if result.backup_path is not None:
        print(f"Backup: {result.backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
