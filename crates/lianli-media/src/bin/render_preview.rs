//! Render a `preview.png` for a template folder.
//!
//! Usage:
//!   cargo run -p lianli-media --bin render-preview -- <template-dir> [--out <file>] [--mock <widget-id>=<value>]...
//!
//! Loads `<template-dir>/template.json`, injects deterministic mock sensor
//! values (seeded per widget id for reproducible PRs), renders one frame at
//! the template's native `base_width × base_height`, and writes the result to
//! `<template-dir>/preview.png` (or `--out` if given).

use anyhow::{anyhow, Context, Result};
use lianli_media::CustomAsset;
use lianli_shared::media::SensorSourceConfig;
use lianli_shared::screen::ScreenInfo;
use lianli_shared::sensors::SensorCategory;
use lianli_shared::systeminfo::SysSensor;
use lianli_shared::template::{LcdTemplate, Widget, WidgetKind};
use std::collections::{hash_map::DefaultHasher, HashMap};
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};

const MOCK_CORE_COUNT: usize = 24;
const MAX_MOCKS: usize = 256;
const MAX_MOCK_ID_BYTES: usize = 256;

struct CliArgs {
    template_dir: PathBuf,
    out_path: Option<PathBuf>,
    mocks: HashMap<String, f32>,
}

fn main() -> Result<()> {
    let raw_args: Vec<String> = std::env::args().skip(1).collect();
    if raw_args.iter().any(|arg| arg == "-h" || arg == "--help") {
        print_usage();
        return Ok(());
    }
    let args = parse_cli(raw_args)?;
    let template_dir = args.template_dir;
    let out_path = args.out_path;

    let template_path = template_dir.join("template.json");
    let raw = std::fs::read_to_string(&template_path)
        .with_context(|| format!("reading {}", template_path.display()))?;
    let mut template: LcdTemplate = serde_json::from_str(&raw)
        .with_context(|| format!("parsing {}", template_path.display()))?;

    validate_mock_ids(&template, &args.mocks)?;
    stub_sensor_sources(&mut template, &args.mocks);
    SysSensor::set_mock_cores(mock_core_values(MOCK_CORE_COUNT));

    let abs_template_dir = std::fs::canonicalize(&template_dir)
        .with_context(|| format!("canonicalizing {}", template_dir.display()))?;
    std::env::set_current_dir(&abs_template_dir)
        .with_context(|| format!("chdir {}", abs_template_dir.display()))?;

    let screen = ScreenInfo {
        width: template.base_width,
        height: template.base_height,
        max_fps: 30,
        jpeg_quality: 100,
        max_payload: usize::MAX,
        h264: false,
        needs_keepalive: false,
        png: false,
        play_count: 0,
    };

    let asset = CustomAsset::new(&template, 0.0, &screen, &[], false, 30.0, false)
        .map_err(|e| anyhow!("building custom asset: {e}"))?;
    asset.seed_preview_history();
    let frame = asset
        .render_frame_rgba_with(true, |bytes| bytes.to_vec())
        .map_err(|e| anyhow!("rendering frame: {e}"))?
        .ok_or_else(|| anyhow!("render_frame_rgba_with returned no frame"))?;

    let out = out_path.unwrap_or_else(|| abs_template_dir.join("preview.png"));
    write_rgba_png(&out, asset.canvas_width(), asset.canvas_height(), &frame)?;
    println!("wrote {}", out.display());
    Ok(())
}

fn parse_cli<I, S>(args: I) -> Result<CliArgs>
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let mut args = args.into_iter().map(Into::into);
    let mut template_dir: Option<PathBuf> = None;
    let mut out_path: Option<PathBuf> = None;
    let mut mocks = HashMap::new();
    let mut mock_count = 0;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--out" | "-o" => {
                out_path = Some(PathBuf::from(
                    args.next()
                        .ok_or_else(|| anyhow!("--out requires a value"))?,
                ));
            }
            "--mock" => {
                mock_count += 1;
                if mock_count > MAX_MOCKS {
                    return Err(anyhow!("at most {MAX_MOCKS} --mock values are allowed"));
                }
                let pair = args
                    .next()
                    .ok_or_else(|| anyhow!("--mock requires <widget-id>=<value>"))?;
                let (id, value) = pair.split_once('=').ok_or_else(|| {
                    anyhow!("invalid --mock '{pair}': expected <widget-id>=<value>")
                })?;
                if id.is_empty() || id.len() > MAX_MOCK_ID_BYTES {
                    return Err(anyhow!(
                        "mock widget id must contain 1 to {MAX_MOCK_ID_BYTES} bytes"
                    ));
                }
                let value: f32 = value
                    .parse()
                    .with_context(|| format!("invalid mock value in '{pair}'"))?;
                if !value.is_finite() {
                    return Err(anyhow!("mock value for '{id}' must be finite"));
                }
                mocks.insert(id.to_string(), value);
            }
            other if other.starts_with('-') => {
                return Err(anyhow!("unexpected argument '{other}'"))
            }
            other if template_dir.is_none() => template_dir = Some(PathBuf::from(other)),
            other => return Err(anyhow!("unexpected argument '{other}'")),
        }
    }

    let template_dir = template_dir.ok_or_else(|| {
        print_usage();
        anyhow!("missing template directory")
    })?;
    Ok(CliArgs {
        template_dir,
        out_path,
        mocks,
    })
}

fn print_usage() {
    eprintln!(
        "usage: render-preview <template-dir> [--out <file>] [--mock <widget-id>=<value>]..."
    );
}

fn validate_mock_ids(template: &LcdTemplate, mocks: &HashMap<String, f32>) -> Result<()> {
    let mut ids: Vec<_> = mocks.keys().collect();
    ids.sort();
    for id in ids {
        let widget = template
            .widgets
            .iter()
            .find(|widget| widget.id == id.as_str())
            .ok_or_else(|| anyhow!("unknown mock widget id '{id}'"))?;
        if widget.kind.source_config().is_none() {
            return Err(anyhow!("mock widget id '{id}' has no sensor source"));
        }
    }
    Ok(())
}

fn stub_sensor_sources(template: &mut LcdTemplate, mocks: &HashMap<String, f32>) {
    for widget in template.widgets.iter_mut() {
        let value = mocks
            .get(&widget.id)
            .copied()
            .unwrap_or_else(|| mock_value_for_widget(widget));
        if let Some(source) = widget.kind.source_config_mut() {
            *source = SensorSourceConfig::Constant { value };
            widget.sensor_category = None;
        }
    }
}

fn write_rgba_png(path: &Path, width: u32, height: u32, bytes: &[u8]) -> Result<()> {
    let image = image::RgbaImage::from_raw(width, height, bytes.to_vec())
        .ok_or_else(|| anyhow!("RGBA frame size does not match {width}x{height}"))?;
    image
        .save(path)
        .with_context(|| format!("writing {}", path.display()))
}

const MOCK_TEMP_C: f32 = 48.0;
const MOCK_USAGE_PCT: f32 = 28.0;

fn mock_value_for_widget(widget: &Widget) -> f32 {
    if let Some(cat) = widget.sensor_category {
        return match cat {
            SensorCategory::CpuTemp | SensorCategory::GpuTemp => MOCK_TEMP_C,
            SensorCategory::CpuUsage | SensorCategory::GpuUsage | SensorCategory::MemUsage => {
                MOCK_USAGE_PCT
            }
            SensorCategory::MemUsed => 16.0,
            SensorCategory::MemFree => 16.0,
            SensorCategory::NetworkRx
            | SensorCategory::NetworkTx
            | SensorCategory::DiskRead
            | SensorCategory::DiskWrite => 25.0,
        };
    }
    if let WidgetKind::ValueText { unit, .. } = &widget.kind {
        let u = unit.to_lowercase();
        if u.contains("°c") || u == "c" {
            return MOCK_TEMP_C;
        }
        if u.contains('%') {
            return MOCK_USAGE_PCT;
        }
        if u.contains("rpm") {
            return 1400.0;
        }
    }
    MOCK_USAGE_PCT
}

fn mock_core_values(count: usize) -> Vec<u32> {
    (0..count)
        .map(|i| {
            let mut hasher = DefaultHasher::new();
            ("lianli-mock-core", i).hash(&mut hasher);
            let r = (hasher.finish() % 10_000) as f32 / 10_000.0;
            let pct = 5.0 + r * 35.0;
            (pct * 100.0) as u32
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[test]
    fn repeated_mocks_parse_and_the_last_duplicate_wins() {
        let args = parse_cli([
            "fixture",
            "--mock",
            "temperature=58",
            "--mock",
            "load=42",
            "--mock",
            "load=96",
        ])
        .unwrap();

        assert_eq!(args.template_dir, PathBuf::from("fixture"));
        assert_eq!(args.mocks.get("temperature"), Some(&58.0));
        assert_eq!(args.mocks.get("load"), Some(&96.0));
    }

    #[test]
    fn invalid_mock_arguments_are_rejected() {
        for args in [
            vec!["fixture", "--mock", "missing-value"],
            vec!["fixture", "--mock", "=42"],
            vec!["fixture", "--mock", "load=NaN"],
            vec!["fixture", "--mock", "load=inf"],
            vec!["fixture", "--unknown"],
        ] {
            assert!(parse_cli(args).is_err());
        }
        let oversized = format!("{}=1", "x".repeat(257));
        assert!(parse_cli(["fixture", "--mock", oversized.as_str()]).is_err());
    }

    #[test]
    fn explicit_widget_mock_overrides_the_category_default() {
        let mut template: LcdTemplate = serde_json::from_value(serde_json::json!({
            "id": "mock", "name": "Mock", "base_width": 400, "base_height": 400,
            "background": {"type": "color", "rgb": [0, 0, 0]},
            "widgets": [{
                "id": "temperature", "x": 200, "y": 200, "width": 100, "height": 50,
                "sensor_category": "cpu_temp",
                "kind": {"type": "value_text", "source": {"type": "cpu_usage"},
                    "font_size": 20, "color": [255, 255, 255]}
            }]
        }))
        .unwrap();
        let mocks = HashMap::from([("temperature".to_string(), 72.0)]);

        stub_sensor_sources(&mut template, &mocks);

        let source = template.widgets[0].kind.source_config().unwrap();
        assert!(matches!(source, SensorSourceConfig::Constant { value } if *value == 72.0));
    }

    #[test]
    fn unknown_widget_mock_is_rejected() {
        let template: LcdTemplate = serde_json::from_value(serde_json::json!({
            "id": "mock", "name": "Mock", "base_width": 400, "base_height": 400,
            "background": {"type": "color", "rgb": [0, 0, 0]},
            "widgets": [{
                "id": "temperature", "x": 200, "y": 200, "width": 100, "height": 50,
                "kind": {"type": "value_text", "source": {"type": "cpu_usage"},
                    "font_size": 20, "color": [255, 255, 255]}
            }]
        }))
        .unwrap();
        let mocks = HashMap::from([("temperatuer".to_string(), 72.0)]);

        let error = validate_mock_ids(&template, &mocks).unwrap_err();

        assert!(error.to_string().contains("temperatuer"));
    }

    #[test]
    fn mock_for_widget_without_sensor_source_is_rejected() {
        let template: LcdTemplate = serde_json::from_value(serde_json::json!({
            "id": "mock", "name": "Mock", "base_width": 400, "base_height": 400,
            "background": {"type": "color", "rgb": [0, 0, 0]},
            "widgets": [{
                "id": "title", "x": 200, "y": 200, "width": 100, "height": 50,
                "kind": {"type": "label", "text": "CPU", "font_size": 20,
                    "color": [255, 255, 255]}
            }]
        }))
        .unwrap();
        let mocks = HashMap::from([("title".to_string(), 72.0)]);

        let error = validate_mock_ids(&template, &mocks).unwrap_err();

        assert!(error.to_string().contains("title"));
    }

    #[test]
    fn raw_rgba_png_preserves_exact_color() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("pixel.png");
        let rgba = [0x20, 0xD6, 0xB0, 0x7F];

        write_rgba_png(&path, 1, 1, &rgba).unwrap();

        let decoded = image::open(path).unwrap().to_rgba8();
        assert_eq!(decoded.as_raw(), &rgba);
    }
}
