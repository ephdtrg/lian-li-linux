use lianli_media::CustomAsset;
use lianli_shared::media::SensorSourceConfig;
use lianli_shared::screen::ScreenInfo;
use lianli_shared::template::catalog::{is_supported, CatalogManifest};
use lianli_shared::template::{LcdTemplate, TemplateBackground, WidgetKind};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

#[derive(Clone, Copy)]
struct TemplateCase {
    id: &'static str,
    name: &'static str,
    ranges: [(f32, f32); 2],
}

const TEMPLATE_CASES: [TemplateCase; 3] = [
    TemplateCase {
        id: "dual-gauge-cpu",
        name: "Dual Gauge CPU",
        ranges: [(0.0, 90.0), (0.0, 100.0)],
    },
    TemplateCase {
        id: "dual-gauge-gpu",
        name: "Dual Gauge GPU",
        ranges: [(0.0, 90.0), (0.0, 100.0)],
    },
    TemplateCase {
        id: "dual-gauge-ram",
        name: "Dual Gauge RAM",
        ranges: [(0.0, 100.0), (0.0, 100.0)],
    },
];

fn asset_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../templates/assets")
}

fn load_template(id: &str) -> LcdTemplate {
    let path = asset_root().join(id).join("template.json");
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("reading {}: {error}", path.display()));
    serde_json::from_str(&raw).unwrap_or_else(|error| panic!("parsing {}: {error}", path.display()))
}

fn approved_values(id: &str) -> HashMap<String, f32> {
    let values: &[(&str, f32)] = match id {
        "dual-gauge-cpu" => &[
            ("temperature-ring", 72.0),
            ("usage-ring", 42.0),
            ("temperature-value", 72.0),
            ("usage-value", 42.0),
        ],
        "dual-gauge-gpu" => &[
            ("temperature-ring", 58.0),
            ("usage-ring", 96.0),
            ("temperature-value", 58.0),
            ("usage-value", 96.0),
        ],
        "dual-gauge-ram" => &[
            ("memory-outer-ring", 69.0),
            ("memory-inner-ring", 69.0),
            ("memory-percent-value", 69.0),
            ("memory-used-value", 22.0),
            ("memory-total-value", 32.0),
        ],
        _ => unreachable!(),
    };
    values
        .iter()
        .map(|(widget, value)| ((*widget).to_string(), *value))
        .collect()
}

fn build_asset(id: &str, values: &HashMap<String, f32>) -> std::sync::Arc<CustomAsset> {
    let directory = asset_root().join(id);
    let mut template = load_template(id);
    for widget in &mut template.widgets {
        if let Some(font) = widget.kind.font_ref_mut() {
            if let Some(path) = &mut font.path {
                *path = directory.join(&*path);
            }
        }
        if let Some(source) = widget.kind.source_config_mut() {
            let value = values
                .get(&widget.id)
                .unwrap_or_else(|| panic!("missing mock for {id}: {}", widget.id));
            *source = SensorSourceConfig::Constant { value: *value };
            widget.sensor_category = None;
        }
    }
    CustomAsset::new(
        &template,
        0.0,
        &ScreenInfo {
            width: 400,
            height: 400,
            max_fps: 30,
            jpeg_quality: 100,
            max_payload: usize::MAX,
            h264: false,
            needs_keepalive: false,
            png: false,
            play_count: 0,
        },
        &[],
        false,
        30.0,
        false,
    )
    .unwrap()
}

fn render(asset: &CustomAsset) -> Vec<u8> {
    asset
        .render_frame_rgba_with(true, |bytes| bytes.to_vec())
        .unwrap()
        .unwrap()
}

#[test]
fn ram_catalog_entry_requires_the_mem_total_release() {
    let manifest_path = asset_root().join("../default_templates.json");
    let raw = std::fs::read_to_string(&manifest_path)
        .unwrap_or_else(|error| panic!("reading {}: {error}", manifest_path.display()));
    let manifest: CatalogManifest = serde_json::from_str(&raw)
        .unwrap_or_else(|error| panic!("parsing {}: {error}", manifest_path.display()));
    let ram = manifest
        .templates
        .iter()
        .find(|template| template.id == "dual-gauge-ram")
        .expect("RAM template catalog entry");

    assert!(!is_supported(ram, "1.2.6"));
    assert!(is_supported(ram, "1.2.7"));
}

#[test]
fn dual_gauge_templates_have_the_approved_geometry_and_ranges() {
    for TemplateCase {
        id,
        name,
        ranges: expected_ranges,
    } in TEMPLATE_CASES
    {
        let template = load_template(id);
        assert_eq!(template.id, id);
        assert_eq!(template.name, name);
        assert_eq!((template.base_width, template.base_height), (400, 400));
        assert!(template.smooth_edges);
        assert!(matches!(
            template.background,
            TemplateBackground::Color {
                rgb: [0, 0, 0, 255]
            }
        ));
        template.validate().unwrap();

        let gauges: Vec<_> = template
            .widgets
            .iter()
            .filter(|widget| matches!(widget.kind, WidgetKind::RadialGauge { .. }))
            .collect();
        assert_eq!(gauges.len(), 2, "{id}");
        for (widget, expected_range) in gauges.iter().zip(expected_ranges) {
            assert!(widget.x - widget.width / 2.0 >= 8.0, "{id}: {}", widget.id);
            assert!(widget.y - widget.height / 2.0 >= 8.0, "{id}: {}", widget.id);
            assert!(
                widget.x + widget.width / 2.0 <= 392.0,
                "{id}: {}",
                widget.id
            );
            assert!(
                widget.y + widget.height / 2.0 <= 392.0,
                "{id}: {}",
                widget.id
            );
            let WidgetKind::RadialGauge {
                value_min,
                value_max,
                start_angle,
                sweep_angle,
                ..
            } = &widget.kind
            else {
                unreachable!()
            };
            assert_eq!(
                (*value_min, *value_max),
                expected_range,
                "{id}: {}",
                widget.id
            );
            assert_eq!(*start_angle, 270.0, "{id}: {}", widget.id);
            assert_eq!(*sweep_angle, 360.0, "{id}: {}", widget.id);
        }
    }
}

#[test]
fn rendered_templates_stay_inside_the_circle_and_respond_to_every_value() {
    for TemplateCase { id, .. } in TEMPLATE_CASES {
        let values = approved_values(id);
        let asset = build_asset(id, &values);
        let baseline = render(&asset);
        let pixel = |x: usize, y: usize| &baseline[(y * 400 + x) * 4..][..4];

        for y in 0..400 {
            for x in 0..400 {
                if x < 8 || y < 8 || x >= 392 || y >= 392 {
                    assert_eq!(pixel(x, y), [0, 0, 0, 255], "{id}: ({x}, {y})");
                }
            }
        }

        let outer_color = if id == "dual-gauge-ram" {
            [167, 139, 250]
        } else {
            [32, 214, 176]
        };
        let (pixels, remainder) = baseline.as_chunks::<4>();
        assert!(remainder.is_empty());
        let has_color_in_band = |color: [u8; 3], min_radius: f32, max_radius: f32| {
            pixels.iter().enumerate().any(|(index, rgba)| {
                let x = (index % 400) as f32 - 200.0;
                let y = (index / 400) as f32 - 200.0;
                let radius = (x * x + y * y).sqrt();
                radius >= min_radius && radius <= max_radius && rgba[..3] == color && rgba[3] == 255
            })
        };
        assert!(has_color_in_band(outer_color, 169.0, 190.0), "{id}");
        assert!(has_color_in_band([77, 163, 255], 111.0, 130.0), "{id}");

        assert!(asset.render_frame(false).unwrap().is_none(), "{id}");
        for widget_id in values.keys() {
            let mut changed_values = values.clone();
            *changed_values.get_mut(widget_id).unwrap() += 7.0;
            let changed = render(&build_asset(id, &changed_values));
            assert_ne!(baseline, changed, "{id}: {widget_id}");
        }
    }
}
