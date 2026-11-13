# Dual Gauge template pack for the official build

This one-time installer adds **Dual Gauge CPU**, **Dual Gauge GPU** and
**Dual Gauge RAM** to the saved custom templates used by the official Lian Li
Linux daemon. It does not replace the daemon or GUI, contact GitHub, install a
background process, or change fan, RGB and LCD assignment settings.

Python 3 is the only installer dependency. Run it from this source checkout.

## User daemon

```bash
python3 templates/packs/dual-gauge/install.py --user
```

The default destination is `${XDG_CONFIG_HOME:-$HOME/.config}/lianli`. If the
user daemon is active, the installer stops it while publishing the templates and
starts it again afterward.

## System daemon

```bash
sudo python3 templates/packs/dual-gauge/install.py --system
```

The destination is `/var/lib/lianli`. Root is required so the installer can
stop `lianli-daemon-system.service` and retain the configuration directory's
owner on new files.

For a custom native or Distrobox state directory, pass an absolute path:

```bash
python3 templates/packs/dual-gauge/install.py --user --config-dir /absolute/path/to/lianli
```

Use `--no-service` only after stopping the daemon that owns that configuration.
Editing `lcd_templates.json` while the daemon is running can lose changes.

## Existing templates and backups

Unrelated templates and unknown top-level fields in `lcd_templates.json` are
preserved. An existing Dual Gauge ID with different contents is treated as a
user edit and is not overwritten. To replace those three IDs intentionally:

```bash
python3 templates/packs/dual-gauge/install.py --user --replace
```

Before changing an existing state file, the installer creates a sibling backup
named `lcd_templates.json.dual-gauge-backup-*.json`. Writes use a same-directory
temporary file, fsync and atomic rename. Reinstalling an unchanged pack is a
no-op.

## Sensor detection

The installer binds CPU temperature, AMD GPU edge temperature and AMD GPU usage
from the current `/sys` topology. RAM usage uses the official virtual sensors.
The official 1.2.6 schema has no `mem_total` source, so the installed RAM total
is captured once from `/proc/meminfo`. Rerun with `--replace` after changing the
installed memory.

If a required CPU or AMD GPU sensor is unavailable, installation stops before
changing `lcd_templates.json`. Confirm that `k10temp` or `coretemp`, `amdgpu`
edge temperature and `gpu_busy_percent` are exposed, then retry.

## Select in the GUI

Restart or reconnect the official GUI, open **LCD**, choose an LCD, select
**Custom**, then choose one of the three Dual Gauge templates. Assign templates
independently to different fans. Enable the existing **Smooth Edges** option for
each LCD; the official release does not yet honor that option from template JSON.

The templates are normal saved user data and remain in place when the official
Lian Li Linux package is updated. They do not appear under **Browse Online**,
because that window is tied to the upstream catalog.
