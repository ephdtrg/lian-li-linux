use super::{DiskDirection, NetDirection, NvidiaMetric, RateState, ResolvedSensor, SensorSource};
use crate::systeminfo::SysSensor;
use std::collections::HashMap;
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::process::Command;
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug)]
pub struct SensorReading {
    pub value: f32,
    pub observed_at: Instant,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MemorySnapshot {
    total_kib: f32,
    available_kib: f32,
}

impl MemorySnapshot {
    pub fn parse(content: &str) -> anyhow::Result<Self> {
        let total_kib = parse_meminfo_kib(content, "MemTotal:")?;
        let available_kib = parse_meminfo_kib(content, "MemAvailable:")?;
        anyhow::ensure!(total_kib.is_finite() && total_kib > 0.0, "invalid MemTotal");
        anyhow::ensure!(
            available_kib.is_finite() && available_kib >= 0.0 && available_kib <= total_kib,
            "invalid MemAvailable"
        );
        Ok(Self {
            total_kib,
            available_kib,
        })
    }

    pub fn usage_percent(self) -> f32 {
        self.used_kib() * 100.0 / self.total_kib
    }

    pub fn used_gib(self) -> f32 {
        self.used_kib() / 1024.0 / 1024.0
    }

    pub fn available_gib(self) -> f32 {
        self.available_kib / 1024.0 / 1024.0
    }

    pub fn total_gib(self) -> f32 {
        self.total_kib / 1024.0 / 1024.0
    }

    fn used_kib(self) -> f32 {
        self.total_kib - self.available_kib
    }
}

fn parse_meminfo_kib(content: &str, field: &str) -> anyhow::Result<f32> {
    let line = content
        .lines()
        .find(|line| line.starts_with(field))
        .ok_or_else(|| anyhow::anyhow!("missing {field}"))?;
    line.split_whitespace()
        .nth(1)
        .ok_or_else(|| anyhow::anyhow!("missing value for {field}"))?
        .parse()
        .map_err(|error| anyhow::anyhow!("invalid value for {field}: {error}"))
}

#[derive(Default)]
struct MemoryCacheState {
    sample: Option<(Instant, CachedMemorySnapshot)>,
    loading: bool,
}

#[derive(Clone)]
enum CachedMemorySnapshot {
    Available(MemorySnapshot),
    Failed(Arc<str>),
}

impl CachedMemorySnapshot {
    fn into_result(self) -> anyhow::Result<MemorySnapshot> {
        match self {
            Self::Available(snapshot) => Ok(snapshot),
            Self::Failed(message) => Err(anyhow::anyhow!(message.to_string())),
        }
    }
}

#[derive(Default)]
struct MemoryCache {
    state: Mutex<MemoryCacheState>,
    ready: Condvar,
}

static MEMORY_CACHE: OnceLock<MemoryCache> = OnceLock::new();
const MEMORY_CACHE_TTL: Duration = Duration::from_millis(100);

fn memory_snapshot() -> anyhow::Result<MemorySnapshot> {
    cached_memory_snapshot_at(
        MEMORY_CACHE.get_or_init(MemoryCache::default),
        Instant::now(),
        || {
            let content = std::fs::read_to_string("/proc/meminfo")
                .map_err(|error| anyhow::anyhow!("reading /proc/meminfo: {error}"))?;
            MemorySnapshot::parse(&content)
        },
    )
}

fn cached_memory_snapshot_at(
    cache: &MemoryCache,
    now: Instant,
    load: impl FnOnce() -> anyhow::Result<MemorySnapshot>,
) -> anyhow::Result<MemorySnapshot> {
    let mut state = cache
        .state
        .lock()
        .map_err(|_| anyhow::anyhow!("memory snapshot cache lock poisoned"))?;
    loop {
        if let Some((sampled_at, cached)) = &state.sample {
            if now.saturating_duration_since(*sampled_at) < MEMORY_CACHE_TTL {
                return cached.clone().into_result();
            }
        }
        if !state.loading {
            state.loading = true;
            break;
        }
        state = cache
            .ready
            .wait(state)
            .map_err(|_| anyhow::anyhow!("memory snapshot cache lock poisoned"))?;
    }
    drop(state);

    let loaded = load();
    let mut state = cache
        .state
        .lock()
        .map_err(|_| anyhow::anyhow!("memory snapshot cache lock poisoned"))?;
    state.loading = false;
    let cached = match loaded {
        Ok(snapshot) => CachedMemorySnapshot::Available(snapshot),
        Err(error) => CachedMemorySnapshot::Failed(Arc::from(error.to_string())),
    };
    state.sample = Some((now, cached.clone()));
    cache.ready.notify_all();
    cached.into_result()
}

pub fn read_sensor_reading(resolved: &ResolvedSensor) -> anyhow::Result<SensorReading> {
    if let ResolvedSensor::NvidiaGpu { index, metric } = resolved {
        return nvidia_cache_get(*index, *metric);
    }
    if let ResolvedSensor::RuntimeFile(path) = resolved {
        return read_runtime_reading(path);
    }
    let observed_at = Instant::now();
    Ok(SensorReading {
        value: read_sensor_value(resolved)?,
        observed_at,
    })
}

fn read_runtime_reading(path: &std::path::Path) -> anyhow::Result<SensorReading> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW)
        .open(path)?;
    let metadata = file.metadata()?;
    anyhow::ensure!(
        metadata.is_file() && metadata.len() <= 64,
        "Invalid coolant runtime file"
    );
    let modified = metadata.modified()?;
    let age = std::time::SystemTime::now().duration_since(modified)?;
    let observed_at = Instant::now()
        .checked_sub(age)
        .ok_or_else(|| anyhow::anyhow!("Coolant reading predates the monotonic clock"))?;
    let mut content = String::new();
    file.take(64).read_to_string(&mut content)?;
    Ok(SensorReading {
        value: content.trim().parse()?,
        observed_at,
    })
}

pub fn read_sensor_value(resolved: &ResolvedSensor) -> anyhow::Result<f32> {
    match resolved {
        ResolvedSensor::SysfsFile { path, divider, .. } => {
            let content = std::fs::read_to_string(path)
                .map_err(|e| anyhow::anyhow!("reading {}: {e}", path.display()))?;
            let raw_value: f32 = content
                .trim()
                .parse()
                .map_err(|e| anyhow::anyhow!("parsing {}: {e}", path.display()))?;
            Ok(raw_value / (*divider as f32))
        }
        ResolvedSensor::Virtual { source, divider } => match source {
            SensorSource::CpuUsage => Ok(SysSensor::get_cpu_usage() as f32 / *divider as f32),
            SensorSource::MemUsage => Ok(memory_snapshot()?.usage_percent()),
            SensorSource::MemUsed => Ok(memory_snapshot()?.used_kib() / *divider as f32),
            SensorSource::MemFree => Ok(memory_snapshot()?.available_kib / *divider as f32),
            SensorSource::MemTotal => Ok(memory_snapshot()?.total_kib / *divider as f32),
            _ => anyhow::bail!("unexpected virtual sensor source"),
        },
        ResolvedSensor::NvidiaGpu { index, metric } => {
            let reading = nvidia_cache_get(*index, *metric)?;
            anyhow::ensure!(
                reading.observed_at.elapsed() < Duration::from_secs(5),
                "NVIDIA sensor reading expired"
            );
            Ok(reading.value)
        }
        ResolvedSensor::RuntimeFile(path) => {
            let reading = read_runtime_reading(path)?;
            anyhow::ensure!(
                reading.observed_at.elapsed() < Duration::from_secs(5),
                "Coolant sensor reading expired"
            );
            Ok(reading.value)
        }
        ResolvedSensor::ShellCommand(cmd) => {
            super::command::reading(cmd, &std::sync::atomic::AtomicBool::new(false))
                .map(|reading| reading.value)
        }
        ResolvedSensor::Constant(value) => Ok(*value),
        ResolvedSensor::NetworkRate {
            iface,
            direction,
            divider,
            state,
        } => {
            let counter = read_network_counter(iface, *direction)?;
            Ok(compute_rate(state, counter, *divider))
        }
        ResolvedSensor::DiskRate {
            device,
            direction,
            divider,
            state,
        } => {
            let counter = read_disk_counter(device, *direction)?;
            Ok(compute_rate(state, counter, *divider))
        }
    }
}

type NvidiaCache = Arc<Mutex<HashMap<(u32, NvidiaMetric), SensorReading>>>;

static NVIDIA_CACHE: OnceLock<NvidiaCache> = OnceLock::new();

fn nvidia_cache_get(index: u32, metric: NvidiaMetric) -> anyhow::Result<SensorReading> {
    let cache = NVIDIA_CACHE.get_or_init(|| {
        let cache: NvidiaCache = Arc::new(Mutex::new(HashMap::new()));
        let cache_clone = Arc::clone(&cache);
        std::thread::spawn(move || loop {
            let observed_at = Instant::now();
            if let Ok(values) = query_nvidia_smi_all() {
                *cache_clone.lock().unwrap() = values
                    .into_iter()
                    .map(|(key, value)| (key, SensorReading { value, observed_at }))
                    .collect();
            }
            std::thread::sleep(Duration::from_secs(1));
        });
        cache
    });
    cache
        .lock()
        .unwrap()
        .get(&(index, metric))
        .copied()
        .ok_or_else(|| anyhow::anyhow!("NVIDIA sensor reading unavailable"))
}

fn query_nvidia_smi_all() -> anyhow::Result<HashMap<(u32, NvidiaMetric), f32>> {
    let mut command = Command::new("nvidia-smi");
    command.args([
        "--query-gpu=index,temperature.gpu,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]);
    let output = super::command::output(command)?;
    let stdout = String::from_utf8_lossy(&output);
    let mut map = HashMap::new();
    for line in stdout.lines() {
        let parts: Vec<&str> = line.split(',').map(|s| s.trim()).collect();
        if parts.len() < 3 {
            continue;
        }
        let Ok(idx) = parts[0].parse::<u32>() else {
            continue;
        };
        if let Ok(temp) = parts[1].parse::<f32>() {
            map.insert((idx, NvidiaMetric::Temp), temp);
        }
        if let Ok(usage) = parts[2].parse::<f32>() {
            map.insert((idx, NvidiaMetric::Usage), usage);
        }
    }
    Ok(map)
}

fn compute_rate(state: &Arc<Mutex<RateState>>, counter: u64, divider: usize) -> f32 {
    let now = Instant::now();
    let mut s = state.lock().unwrap();
    let rate = match (s.prev_counter, s.prev_at) {
        (Some(prev), Some(prev_at)) => {
            let dt = now.saturating_duration_since(prev_at).as_secs_f32();
            if counter < prev || dt <= 0.0 {
                0.0
            } else {
                (counter - prev) as f32 / dt / divider.max(1) as f32
            }
        }
        _ => 0.0,
    };
    s.prev_counter = Some(counter);
    s.prev_at = Some(now);
    rate
}

fn read_network_counter(iface: &str, direction: NetDirection) -> anyhow::Result<u64> {
    let content = std::fs::read_to_string("/proc/net/dev")
        .map_err(|e| anyhow::anyhow!("reading /proc/net/dev: {e}"))?;
    for line in content.lines() {
        let Some((name, rest)) = line.split_once(':') else {
            continue;
        };
        if name.trim() != iface {
            continue;
        }
        let fields: Vec<&str> = rest.split_whitespace().collect();
        let idx = match direction {
            NetDirection::Rx => 0,
            NetDirection::Tx => 8,
        };
        return fields
            .get(idx)
            .and_then(|f| f.parse::<u64>().ok())
            .ok_or_else(|| anyhow::anyhow!("malformed /proc/net/dev for {iface}"));
    }
    anyhow::bail!("interface '{iface}' not found in /proc/net/dev")
}

fn read_disk_counter(device: &str, direction: DiskDirection) -> anyhow::Result<u64> {
    let content = std::fs::read_to_string("/proc/diskstats")
        .map_err(|e| anyhow::anyhow!("reading /proc/diskstats: {e}"))?;
    for line in content.lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.get(2).copied() != Some(device) {
            continue;
        }
        // sector = 512 bytes; idx 5 = sectors read, idx 9 = sectors written
        let idx = match direction {
            DiskDirection::Read => 5,
            DiskDirection::Write => 9,
        };
        let sectors: u64 = fields
            .get(idx)
            .and_then(|f| f.parse().ok())
            .ok_or_else(|| anyhow::anyhow!("malformed /proc/diskstats for {device}"))?;
        return Ok(sectors.saturating_mul(512));
    }
    anyhow::bail!("device '{device}' not found in /proc/diskstats")
}

pub fn get_mem_usage(content: &str) -> f32 {
    MemorySnapshot::parse(content)
        .map(MemorySnapshot::usage_percent)
        .unwrap_or(0.0)
}

#[cfg(test)]
mod memory_tests {
    use super::{cached_memory_snapshot_at, MemoryCache, MemorySnapshot};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    #[test]
    fn memory_snapshot_reports_coherent_values() {
        let snapshot =
            MemorySnapshot::parse("MemTotal:       33554432 kB\nMemAvailable:   10485760 kB\n")
                .unwrap();

        assert_eq!(snapshot.total_gib(), 32.0);
        assert_eq!(snapshot.used_gib(), 22.0);
        assert_eq!(snapshot.available_gib(), 10.0);
        assert_eq!(snapshot.usage_percent(), 68.75);
    }

    #[test]
    fn memory_snapshot_rejects_invalid_meminfo() {
        for content in [
            "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
            "MemTotal: 1024 kB\n",
            "MemAvailable: 512 kB\n",
            "MemTotal: nope kB\nMemAvailable: 512 kB\n",
            "MemTotal: 512 kB\nMemAvailable: 1024 kB\n",
        ] {
            assert!(MemorySnapshot::parse(content).is_err(), "{content:?}");
        }
    }

    #[test]
    fn memory_snapshot_cache_reuses_then_refreshes_the_sample() {
        let cache = MemoryCache::default();
        let loads = AtomicUsize::new(0);
        let started = Instant::now();
        let load = || {
            let value = loads.fetch_add(1, Ordering::Relaxed) + 1;
            MemorySnapshot::parse(&format!(
                "MemTotal: 1048576 kB\nMemAvailable: {} kB\n",
                1048576 - value
            ))
        };

        let first = cached_memory_snapshot_at(&cache, started, load).unwrap();
        let cached =
            cached_memory_snapshot_at(&cache, started + Duration::from_millis(99), load).unwrap();
        let refreshed =
            cached_memory_snapshot_at(&cache, started + Duration::from_millis(100), load).unwrap();

        assert_eq!(loads.load(Ordering::Relaxed), 2);
        assert_eq!(first, cached);
        assert_ne!(cached, refreshed);
    }

    #[test]
    fn concurrent_memory_snapshot_requests_share_one_load() {
        let cache = MemoryCache::default();
        let loads = AtomicUsize::new(0);
        let started = Instant::now();
        let (first_loading_tx, first_loading_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let (second_loading_tx, second_loading_rx) = mpsc::channel();

        std::thread::scope(|scope| {
            let cache_ref = &cache;
            let loads_ref = &loads;
            let first = scope.spawn(move || {
                cached_memory_snapshot_at(cache_ref, started, || {
                    loads_ref.fetch_add(1, Ordering::Relaxed);
                    first_loading_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                    MemorySnapshot::parse("MemTotal: 1048576 kB\nMemAvailable: 524288 kB\n")
                })
                .unwrap()
            });
            first_loading_rx.recv().unwrap();

            let cache_ref = &cache;
            let loads_ref = &loads;
            let second = scope.spawn(move || {
                cached_memory_snapshot_at(cache_ref, started, || {
                    loads_ref.fetch_add(1, Ordering::Relaxed);
                    second_loading_tx.send(()).unwrap();
                    MemorySnapshot::parse("MemTotal: 1048576 kB\nMemAvailable: 262144 kB\n")
                })
                .unwrap()
            });

            let duplicate_load = second_loading_rx
                .recv_timeout(Duration::from_millis(200))
                .is_ok();
            release_tx.send(()).unwrap();

            assert_eq!(first.join().unwrap(), second.join().unwrap());
            assert!(
                !duplicate_load,
                "concurrent request started a duplicate load"
            );
        });
        assert_eq!(loads.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn concurrent_memory_snapshot_failures_share_one_load() {
        let cache = MemoryCache::default();
        let loads = AtomicUsize::new(0);
        let started = Instant::now();
        let (first_loading_tx, first_loading_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();

        std::thread::scope(|scope| {
            let cache_ref = &cache;
            let loads_ref = &loads;
            let first = scope.spawn(move || {
                cached_memory_snapshot_at(cache_ref, started, || {
                    loads_ref.fetch_add(1, Ordering::Relaxed);
                    first_loading_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                    anyhow::bail!("unavailable")
                })
            });
            first_loading_rx.recv().unwrap();

            let second = scope.spawn(|| {
                cached_memory_snapshot_at(&cache, started, || {
                    loads.fetch_add(1, Ordering::Relaxed);
                    anyhow::bail!("duplicate load")
                })
            });
            release_tx.send(()).unwrap();

            assert!(first.join().unwrap().is_err());
            assert!(second.join().unwrap().is_err());
        });
        assert_eq!(loads.load(Ordering::Relaxed), 1);
    }
}
