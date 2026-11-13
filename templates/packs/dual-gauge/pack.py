from __future__ import annotations

import copy
from dataclasses import dataclass
import errno
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Mapping


class PackError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallResult:
    installed_ids: tuple[str, ...]
    backup_path: Path | None
    changed: bool


PACK_IDS = ("dual-gauge-cpu", "dual-gauge-gpu", "dual-gauge-ram")
MAX_STATE_BYTES = 16 * 1024 * 1024
STANDARD_MEMORY_GIB = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048)


def resolve_config_dir(mode: str, explicit: Path | None, env: Mapping[str, str]) -> Path:
    if explicit is not None:
        path = Path(explicit)
    elif mode == "system":
        path = Path("/var/lib/lianli")
    elif mode == "user":
        xdg = env.get("XDG_CONFIG_HOME")
        if xdg:
            path = Path(xdg) / "lianli"
        else:
            home = env.get("HOME")
            if not home:
                raise PackError("HOME is unavailable; pass --config-dir")
            path = Path(home) / ".config" / "lianli"
    else:
        raise PackError(f"unsupported install mode: {mode}")
    if not path.is_absolute():
        raise PackError(f"configuration directory must be absolute: {path}")
    return path


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _device_key(directory: Path) -> str | None:
    try:
        target = os.readlink(directory / "device")
    except OSError:
        return None
    return Path(target).name or None


def _hwmon_temperatures(sysfs_root: Path) -> list[dict]:
    root = sysfs_root / "class" / "hwmon"
    sensors = []
    try:
        directories = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return sensors
    for directory in directories:
        name = _read_text(directory / "name")
        device_path = _device_key(directory)
        if not name or not device_path:
            continue
        for input_path in sorted(directory.glob("temp*_input")):
            label_key = input_path.name.removesuffix("_input")
            label = _read_text(directory / f"{label_key}_label") or ""
            sensors.append(
                {
                    "name": name,
                    "label": label_key,
                    "human_label": label,
                    "device_path": device_path,
                }
            )
    return sensors


def _cpu_temperature(temperatures: list[dict]) -> dict | None:
    supported = {"k10temp": 0, "coretemp": 0, "zenpower": 1, "zenpower3": 1, "k8temp": 2}
    candidates = [sensor for sensor in temperatures if sensor["name"] in supported]
    if not candidates:
        return None

    def priority(sensor: dict) -> tuple[int, int, str, str]:
        label = sensor["human_label"].lower()
        preferred = 0 if "tctl" in label or "package id 0" in label else 1
        return supported[sensor["name"]], preferred, sensor["device_path"], sensor["label"]

    sensor = min(candidates, key=priority)
    return {
        "type": "hwmon",
        "name": sensor["name"],
        "label": sensor["label"],
        "device_path": sensor["device_path"],
    }


def _amd_cards(sysfs_root: Path) -> list[tuple[int, str]]:
    root = sysfs_root / "class" / "drm"
    cards = []
    try:
        directories = root.iterdir()
    except OSError:
        return cards
    for directory in directories:
        match = re.fullmatch(r"card(\d+)", directory.name)
        if not match:
            continue
        if _read_text(directory / "device" / "vendor") != "0x1002":
            continue
        if not (directory / "device" / "gpu_busy_percent").is_file():
            continue
        device_path = _device_key(directory)
        if device_path:
            cards.append((int(match.group(1)), device_path))
    return sorted(cards)


def _gpu_temperature_for(device_path: str, temperatures: list[dict]) -> dict | None:
    candidates = [
        sensor
        for sensor in temperatures
        if sensor["name"] in {"amdgpu", "radeon"} and sensor["device_path"] == device_path
    ]
    if not candidates:
        return None
    sensor = min(
        candidates,
        key=lambda item: (0 if "edge" in item["human_label"].lower() else 1, item["label"]),
    )
    return {
        "type": "hwmon",
        "name": sensor["name"],
        "label": sensor["label"],
        "device_path": sensor["device_path"],
    }


def detect_bindings(sysfs_root: Path) -> dict[str, dict]:
    temperatures = _hwmon_temperatures(sysfs_root)
    bindings: dict[str, dict] = {}
    cpu = _cpu_temperature(temperatures)
    if cpu is not None:
        bindings["cpu_temp"] = cpu

    cards = _amd_cards(sysfs_root)
    for card_index, device_path in cards:
        gpu_temp = _gpu_temperature_for(device_path, temperatures)
        if gpu_temp is not None:
            bindings["gpu_temp"] = gpu_temp
            bindings["gpu_usage"] = {"type": "amd_gpu_usage", "card_index": card_index}
            break

    missing = [category for category in ("cpu_temp", "gpu_temp", "gpu_usage") if category not in bindings]
    if missing:
        raise PackError("required sensors unavailable: " + ", ".join(missing))
    return bindings


def read_total_memory_gib(meminfo_path: Path) -> float:
    try:
        lines = meminfo_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise PackError(f"cannot read {meminfo_path}: {error}") from error
    for line in lines:
        if line.startswith("MemTotal:"):
            fields = line.split()
            if len(fields) < 2:
                break
            try:
                total_kib = float(fields[1])
            except ValueError:
                break
            if math.isfinite(total_kib) and total_kib > 0:
                visible_gib = total_kib / 1024 / 1024
                for nominal_gib in STANDARD_MEMORY_GIB:
                    if nominal_gib >= visible_gib:
                        return float(nominal_gib)
                return float(math.ceil(visible_gib / 16) * 16)
            break
    raise PackError(f"invalid or missing MemTotal in {meminfo_path}")


def prepare_templates(
    asset_root: Path,
    font_path: Path,
    bindings: Mapping[str, dict],
    total_gib: float,
) -> list[dict]:
    if not font_path.is_absolute():
        raise PackError("installed font path must be absolute")
    if not math.isfinite(total_gib) or total_gib <= 0:
        raise PackError("total memory must be positive")

    templates = []
    for template_id in PACK_IDS:
        path = asset_root / template_id / "template.json"
        try:
            template = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PackError(f"cannot load {path}: {error}") from error
        if not isinstance(template, dict) or template.get("id") != template_id:
            raise PackError(f"unexpected template identity in {path}")
        template.pop("smooth_edges", None)
        for widget in template.get("widgets", []):
            kind = widget.get("kind", {})
            font = kind.get("font")
            if isinstance(font, dict):
                font["path"] = str(font_path)
            category = widget.get("sensor_category")
            if category in bindings and "source" in kind:
                kind["source"] = copy.deepcopy(bindings[category])
            if template_id == "dual-gauge-ram" and widget.get("id") == "memory-total-value":
                kind["source"] = {"type": "constant", "value": total_gib}
                widget["update_interval_ms"] = 10_000
            if "source" in kind:
                widget.pop("sensor_category", None)
        templates.append(template)
    return templates


class _AtomicWriteFailure(OSError):
    def __init__(self, error: OSError, published: bool):
        super().__init__(error.errno, str(error))
        self.published = published


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, _directory_flags())
    except OSError as error:
        try:
            is_symlink = stat.S_ISLNK(os.lstat(path).st_mode)
        except OSError:
            is_symlink = False
        if is_symlink:
            raise PackError(f"configuration directory must not be a symlink: {path}") from error
        raise PackError(f"cannot open configuration directory {path}: {error}") from error


def _open_child_directory(parent: int, name: str, owner: tuple[int, int]) -> int:
    try:
        return os.open(name, _directory_flags(), dir_fd=parent)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o755, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError as error:
            raise PackError(f"cannot create installation directory {name}: {error}") from error
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent)
        except OSError as error:
            raise PackError(f"installation directory must not be a symlink: {name}") from error
        try:
            os.fchown(descriptor, *owner)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    except OSError as error:
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            is_symlink = stat.S_ISLNK(metadata.st_mode)
        except OSError:
            is_symlink = False
        if is_symlink:
            raise PackError(f"installation directory must not be a symlink: {name}") from error
        raise PackError(f"cannot open installation directory {name}: {error}") from error


def _read_regular_file_at(directory: int, name: str, limit: int) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise PackError(f"file must not be a symlink: {name}") from error
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PackError(f"file must be a regular file: {name}")
        if metadata.st_size > limit:
            raise PackError(f"file exceeds the {limit}-byte limit: {name}")
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise PackError(f"file exceeds the {limit}-byte limit: {name}")
        return raw, metadata
    finally:
        os.close(descriptor)


def _load_state_at(directory: int, name: str) -> tuple[dict, bytes | None, int]:
    try:
        raw, metadata = _read_regular_file_at(directory, name, MAX_STATE_BYTES)
    except FileNotFoundError:
        return {"templates": []}, None, 0o600
    except OSError as error:
        raise PackError(f"cannot read {name}: {error}") from error
    state_mode = stat.S_IMODE(metadata.st_mode)
    if len(raw) > MAX_STATE_BYTES:
        raise PackError(f"{name} exceeds the 16 MiB state-file limit")
    try:
        state = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PackError(f"invalid JSON in {name}: {error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("templates"), list):
        raise PackError(f"invalid template collection in {name}")
    templates = state["templates"]
    if len(templates) > 1024:
        raise PackError(f"{name} contains more than 1024 templates")
    ids = []
    for template in templates:
        if not isinstance(template, dict) or not isinstance(template.get("id"), str):
            raise PackError(f"invalid template entry in {name}")
        ids.append(template["id"])
    if len(ids) != len(set(ids)):
        raise PackError(f"duplicate template IDs in {name}")
    return state, raw, state_mode


def _load_state(path: Path) -> tuple[dict, bytes | None]:
    path = Path(path)
    parent = _open_directory(path.parent)
    try:
        state, raw, _ = _load_state_at(parent, path.name)
        return state, raw
    finally:
        os.close(parent)


def _temporary_name(target: str) -> str:
    return f".{target}.{secrets.token_hex(8)}"


def _write_file_descriptor(
    descriptor: int,
    data: bytes,
    mode: int,
    owner: tuple[int, int] | None,
) -> None:
    with os.fdopen(descriptor, "wb", closefd=False) as file:
        file.write(data)
        file.flush()
    os.fchmod(descriptor, mode)
    if owner is not None:
        os.fchown(descriptor, *owner)
    os.fsync(descriptor)


def _atomic_write_at(
    directory: int,
    name: str,
    data: bytes,
    mode: int,
    owner: tuple[int, int] | None = None,
) -> None:
    temporary = _temporary_name(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = -1
    published = False
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
        _write_file_descriptor(descriptor, data, mode, owner)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        published = True
        os.fsync(directory)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        raise _AtomicWriteFailure(error, published) from error


def _atomic_write(
    path: Path,
    data: bytes,
    mode: int,
    owner: tuple[int, int] | None = None,
) -> None:
    path = Path(path)
    directory = _open_directory(path.parent)
    try:
        _atomic_write_at(directory, path.name, data, mode, owner)
    finally:
        os.close(directory)


def _publish_asset_at(source: Path, directory: int, name: str, owner: tuple[int, int]) -> bool:
    try:
        data = source.read_bytes()
    except OSError as error:
        raise PackError(f"cannot read bundled asset {source}: {error}") from error
    if not data:
        raise PackError(f"bundled asset is empty: {source}")
    try:
        current, metadata = _read_regular_file_at(directory, name, MAX_STATE_BYTES)
    except FileNotFoundError:
        current = None
        metadata = None
    except OSError as error:
        raise PackError(f"cannot inspect installed asset {name}: {error}") from error
    if current == data and metadata is not None:
        changed = stat.S_IMODE(metadata.st_mode) != 0o644
        changed |= (metadata.st_uid, metadata.st_gid) != owner
        if changed:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                os.fchmod(descriptor, 0o644)
                os.fchown(descriptor, *owner)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory)
        return changed
    try:
        _atomic_write_at(directory, name, data, 0o644, owner)
    except _AtomicWriteFailure as error:
        raise PackError(f"cannot publish installed asset {name}: {error}") from error
    return True


def _backup_state_at(
    directory: int,
    config_dir: Path,
    raw: bytes,
    owner: tuple[int, int],
) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    name = f"lcd_templates.json.dual-gauge-backup-{secrets.token_hex(8)}.json"
    descriptor = -1
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory)
        _write_file_descriptor(descriptor, raw, 0o600, owner)
        os.close(descriptor)
        descriptor = -1
        os.fsync(directory)
        return config_dir / name
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise PackError(f"cannot create state backup: {error}") from error


def _normalize_template(template: dict) -> dict:
    normalized = copy.deepcopy(template)
    if normalized.get("smooth_edges") is False:
        normalized.pop("smooth_edges")
    normalized.setdefault("rotated", False)
    normalized.setdefault("target_device", None)
    for widget in normalized.get("widgets", []):
        widget.setdefault("rotation", 0.0)
        widget.setdefault("visible", True)
        widget.setdefault("update_interval_ms", None)
        widget.setdefault("fps", None)
        if widget.get("sensor_category") is None:
            widget.pop("sensor_category", None)
        kind = widget.get("kind", {})
        kind_type = kind.get("type")
        if kind_type == "label":
            kind.setdefault("font", {"path": None})
            kind.setdefault("align", "center")
            kind.setdefault("letter_spacing", 0.0)
        elif kind_type == "value_text":
            kind.setdefault("format", "{:.1}")
            kind.setdefault("unit", "")
            kind.setdefault("font", {"path": None})
            kind.setdefault("align", "center")
            kind.setdefault("value_min", 0.0)
            kind.setdefault("value_max", 100.0)
            kind.setdefault("ranges", [])
            kind.setdefault("letter_spacing", 0.0)
        elif kind_type == "radial_gauge":
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
    return normalized


def _rollback_state(
    directory: int,
    name: str,
    previous_raw: bytes | None,
    mode: int,
    owner: tuple[int, int],
) -> None:
    if previous_raw is None:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.fsync(directory)
        return
    _atomic_write_at(directory, name, previous_raw, mode, owner)


def install_pack(
    config_dir: Path,
    asset_root: Path,
    sysfs_root: Path,
    meminfo_path: Path,
    replace: bool,
) -> InstallResult:
    config_dir = Path(config_dir)
    if not config_dir.is_absolute():
        raise PackError(f"configuration directory must be absolute: {config_dir}")
    config_descriptor = _open_directory(config_dir)
    try:
        config_stat = os.fstat(config_descriptor)
        owner = (config_stat.st_uid, config_stat.st_gid)
        state_name = "lcd_templates.json"
        state, previous_raw, state_mode = _load_state_at(config_descriptor, state_name)
        bindings = detect_bindings(sysfs_root)
        total_gib = read_total_memory_gib(meminfo_path)
        install_dir = config_dir / "template-packs" / "dual-gauge"
        font_path = install_dir / "NotoSans-SemiBold.ttf"
        prepared = prepare_templates(asset_root, font_path, bindings, total_gib)

        merged = copy.deepcopy(state)
        templates = merged["templates"]
        index_by_id = {template["id"]: index for index, template in enumerate(templates)}
        state_changed = False
        for template in prepared:
            index = index_by_id.get(template["id"])
            if index is None:
                index_by_id[template["id"]] = len(templates)
                templates.append(template)
                state_changed = True
            elif _normalize_template(templates[index]) != _normalize_template(template):
                if not replace:
                    raise PackError(
                        f"template '{template['id']}' already exists with local changes; use --replace"
                    )
                templates[index] = template
                state_changed = True
        if len(templates) > 1024:
            raise PackError("installed template state would contain more than 1024 templates")

        encoded = None
        if state_changed:
            encoded = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(encoded) > MAX_STATE_BYTES:
                raise PackError("installed template state would exceed 16 MiB")

        pack_descriptor = _open_child_directory(config_descriptor, "template-packs", owner)
        try:
            install_descriptor = _open_child_directory(pack_descriptor, "dual-gauge", owner)
            try:
                source_dir = asset_root / "dual-gauge-cpu"
                assets_changed = _publish_asset_at(
                    source_dir / "NotoSans-SemiBold.ttf",
                    install_descriptor,
                    "NotoSans-SemiBold.ttf",
                    owner,
                )
                assets_changed |= _publish_asset_at(
                    source_dir / "OFL.txt",
                    install_descriptor,
                    "OFL.txt",
                    owner,
                )
            finally:
                os.close(install_descriptor)
        finally:
            os.close(pack_descriptor)

        backup_path = None
        if state_changed and encoded is not None:
            if previous_raw is not None:
                backup_path = _backup_state_at(config_descriptor, config_dir, previous_raw, owner)
            try:
                _atomic_write_at(config_descriptor, state_name, encoded, state_mode, owner)
            except _AtomicWriteFailure as error:
                rollback_error = None
                if error.published:
                    try:
                        _rollback_state(
                            config_descriptor,
                            state_name,
                            previous_raw,
                            state_mode,
                            owner,
                        )
                    except OSError as failure:
                        rollback_error = failure
                message = f"cannot publish {config_dir / state_name}: {error}"
                if rollback_error is not None:
                    message += f"; rollback failed: {rollback_error}"
                raise PackError(message) from error
            except OSError as error:
                raise PackError(f"cannot publish {config_dir / state_name}: {error}") from error

        return InstallResult(PACK_IDS, backup_path, state_changed or assets_changed)
    finally:
        os.close(config_descriptor)
