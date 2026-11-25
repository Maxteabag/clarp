#![allow(clippy::unwrap_used)]

use std::sync::atomic::{AtomicI64, AtomicUsize, Ordering};

use super::*;

/// A clock a test moves by hand.
#[derive(Default)]
struct Hand(AtomicI64);

impl Clock for Hand {
    fn now_ms(&self) -> i64 {
        self.0.load(Ordering::SeqCst)
    }
}

/// A folder of pressure files a test writes, as the kernel's are.
struct Fake(PathBuf);

impl Fake {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("pressure-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let fake = Self(dir);
        fake.set(0.0, 0.0, 0.0);
        fake
    }

    /// cpu `some` avg60, io `some` avg60 and memory `full` avg10, in percent.
    fn set(&self, cpu: f64, io: f64, memory: f64) {
        let file = |some10: f64, some60: f64, full: Option<(f64, f64)>| {
            let mut text =
                format!("some avg10={some10:.2} avg60={some60:.2} avg300=0.00 total=12345\n");
            if let Some((full10, full60)) = full {
                text.push_str(&format!(
                    "full avg10={full10:.2} avg60={full60:.2} avg300=0.00 total=678\n"
                ));
            }
            text
        };
        std::fs::write(self.0.join("cpu"), file(0.0, cpu, Some((0.0, 0.0)))).unwrap();
        std::fs::write(self.0.join("io"), file(0.0, io, Some((0.0, io / 2.0)))).unwrap();
        std::fs::write(self.0.join("memory"), file(0.0, 0.0, Some((memory, 0.0)))).unwrap();
    }
}

impl Drop for Fake {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// A gate on `dir` with the default thresholds, ticked by hand.
fn gate(dir: Option<PathBuf>, clock: Arc<Hand>) -> Arc<Gate> {
    Arc::new(Gate {
        host_cpu: dir.as_ref().map(|dir| dir.join("host-cpu")),
        dir,
        clock,
        thresholds: || Thresholds::DEFAULT,
        ticks_itself: false,
        state: Mutex::default(),
        cold: Mutex::default(),
    })
}

/// A way to tell a waiting turn new words that tells nobody.
fn quiet() -> Box<dyn Fn(&str) + Send> {
    Box::new(|_| {})
}

/// A way to let a turn go that counts how often it was let go into `count`, and in which order
/// into `order`.
fn counted(
    count: &Arc<AtomicUsize>,
    order: &Arc<Mutex<Vec<u64>>>,
    turn: u64,
) -> Box<dyn FnOnce() + Send> {
    let (count, order) = (count.clone(), order.clone());
    Box::new(move || {
        count.fetch_add(1, Ordering::SeqCst);
        order.lock().unwrap().push(turn);
    })
}

#[test]
fn a_pressure_file_is_read_as_the_kernel_writes_it() {
    let io = Resource::of(
        "some avg10=12.50 avg60=31.07 avg300=8.00 total=99\n\
         full avg10=4.00 avg60=20.25 avg300=1.00 total=42\n",
    )
    .unwrap();
    assert_eq!(
        io,
        Resource {
            some: Stall {
                avg10: 12.5,
                avg60: 31.07
            },
            full: Some(Stall {
                avg10: 4.0,
                avg60: 20.25
            }),
        }
    );
    // A kernel that writes no full line for the processors.
    let cpu = Resource::of("some avg10=1.00 avg60=2.00 avg300=3.00 total=4\n").unwrap();
    assert_eq!(cpu.full, None);
    for text in [
        "",
        "some avg10=x avg60=2.00",
        "full avg10=1.00 avg60=2.00\n",
    ] {
        assert_eq!(Resource::of(text), None, "{text:?}");
    }
}

#[test]
fn a_machine_without_pressure_files_is_never_under_pressure() {
    let missing = std::env::temp_dir().join(format!("pressure-none-{}", std::process::id()));
    let reading = Reading::read(&missing, None);
    assert_eq!(reading, Reading::default());
    assert!(!Thresholds::DEFAULT.high_after(false, &reading));
    let clock = Arc::new(Hand::default());
    for dir in [None, Some(missing)] {
        let gate = gate(dir, clock.clone());
        assert!(!gate.high());
        assert!(!gate.hold("a", 1, Box::new(|| {}), quiet()));
        assert_eq!(gate.waiting(), 0);
    }
}

#[test]
fn each_threshold_puts_the_machine_under_pressure_by_its_own_figure() {
    let fake = Fake::new("figures");
    let gate = gate(Some(fake.0.clone()), Arc::new(Hand::default()));
    for (cpu, io, memory, high) in [
        (59.0, 29.0, 9.0, false),
        (60.0, 0.0, 0.0, true),
        (0.0, 0.0, 0.0, false),
        (0.0, 30.0, 0.0, true),
        (0.0, 0.0, 0.0, false),
        (0.0, 0.0, 10.0, true),
        (0.0, 0.0, 0.0, false),
    ] {
        fake.set(cpu, io, memory);
        assert_eq!(gate.high(), high, "cpu {cpu} io {io} memory {memory}");
    }
    let look = gate.look();
    assert_eq!(look.thresholds, Thresholds::DEFAULT);
    assert_eq!(look.reading.io.unwrap().full.unwrap().avg60, 0.0);
}

#[test]
fn a_reading_that_wavers_about_a_threshold_does_not_flap() {
    let fake = Fake::new("hysteresis");
    let gate = gate(Some(fake.0.clone()), Arc::new(Hand::default()));
    // io some avg60 against its threshold of 30, calm again only below 24 (80%).
    for (io, high) in [
        (29.0, false),
        (31.0, true),
        (29.0, true),
        (31.0, true),
        (29.0, true),
        (24.0, true),
        (23.9, false),
        (29.0, false),
        (29.9, false),
        (30.0, true),
    ] {
        fake.set(0.0, io, 0.0);
        assert_eq!(gate.high(), high, "io {io}");
    }
    // Calm again needs every figure below 80% of its own.
    fake.set(50.0, 0.0, 0.0);
    assert!(gate.high(), "cpu 50 is not below 48");
    fake.set(47.0, 0.0, 8.5);
    assert!(gate.high(), "memory 8.5 is not below 8");
    fake.set(47.0, 0.0, 7.9);
    assert!(!gate.high());
}

#[test]
fn turns_wait_under_pressure_and_go_one_at_a_time_when_it_falls() {
    let fake = Fake::new("calm");
    let clock = Arc::new(Hand::default());
    let gate = gate(Some(fake.0.clone()), clock.clone());
    let (count, order) = (Arc::default(), Arc::default());
    // Calm, with nothing waiting: a turn starts at once.
    assert!(!gate.hold("a", 1, counted(&count, &order, 1), quiet()));
    fake.set(0.0, 45.0, 0.0);
    assert!(gate.hold("a", 2, counted(&count, &order, 2), quiet()));
    assert!(gate.hold("b", 3, counted(&count, &order, 3), quiet()));
    assert_eq!(gate.waiting(), 2);
    // Under pressure, a look lets nothing go.
    assert_eq!(gate.tick(), Some(EVERY));
    assert_eq!(count.load(Ordering::SeqCst), 0);
    // Calm: the first goes, and the next only at the next look.
    fake.set(0.0, 10.0, 0.0);
    assert_eq!(gate.tick(), Some(BETWEEN));
    assert_eq!(*order.lock().unwrap(), [2]);
    // A turn that comes while others still wait waits behind them, calm or not.
    assert!(gate.hold("c", 4, counted(&count, &order, 4), quiet()));
    assert_eq!(gate.tick(), Some(BETWEEN));
    assert_eq!(gate.tick(), Some(BETWEEN));
    assert_eq!(*order.lock().unwrap(), [2, 3, 4]);
    // Nothing waits: the ticking ends, and the pressure is not read on a timer.
    assert_eq!(gate.tick(), None);
    assert!(!gate.hold("d", 5, counted(&count, &order, 5), quiet()));
    assert_eq!(count.load(Ordering::SeqCst), 3);
}

#[test]
fn a_turn_goes_at_the_bound_however_high_the_pressure() {
    let fake = Fake::new("bound");
    fake.set(90.0, 90.0, 50.0);
    let clock = Arc::new(Hand::default());
    let gate = gate(Some(fake.0.clone()), clock.clone());
    let (count, order) = (Arc::default(), Arc::default());
    assert!(gate.hold("a", 1, counted(&count, &order, 1), quiet()));
    clock.0.store(60_000, Ordering::SeqCst);
    assert!(gate.hold("b", 2, counted(&count, &order, 2), quiet()));
    let bound = i64::try_from(BOUND.as_millis()).unwrap();
    clock.0.store(bound - 1, Ordering::SeqCst);
    assert_eq!(gate.tick(), Some(EVERY));
    clock.0.store(bound, Ordering::SeqCst);
    assert_eq!(gate.tick(), Some(BETWEEN));
    assert_eq!(*order.lock().unwrap(), [1]);
    // The second has not waited its ten minutes yet.
    assert_eq!(gate.tick(), Some(EVERY));
    clock.0.store(bound + 60_000, Ordering::SeqCst);
    assert_eq!(gate.tick(), Some(BETWEEN));
    assert_eq!(*order.lock().unwrap(), [1, 2]);
    assert_eq!(gate.tick(), None);
}

#[test]
fn a_turn_that_is_forgotten_is_never_let_go() {
    let fake = Fake::new("forget");
    fake.set(0.0, 40.0, 0.0);
    let gate = gate(Some(fake.0.clone()), Arc::new(Hand::default()));
    let (count, order) = (Arc::default(), Arc::default());
    assert!(gate.hold("a", 1, counted(&count, &order, 1), quiet()));
    assert!(gate.hold("b", 1, counted(&count, &order, 11), quiet()));
    gate.forget("a", 1);
    assert_eq!(gate.waiting(), 1);
    fake.set(0.0, 0.0, 0.0);
    assert_eq!(gate.tick(), Some(BETWEEN));
    assert_eq!(gate.tick(), None);
    assert_eq!(*order.lock().unwrap(), [11]);
}

#[test]
fn the_thresholds_are_the_owners_settings_within_their_range() {
    assert_eq!(Thresholds::of(None), Thresholds::DEFAULT);
    let file: toml::Table = "pressure_cpu_percent = 70\npressure_io_percent = 1\n\
                             pressure_memory_percent = 200\n"
        .parse()
        .unwrap();
    assert_eq!(
        Thresholds::of(Some(&file)),
        Thresholds {
            cpu: 70,
            io: LEAST,
            memory: MOST
        }
    );
    // They are read with the lanes' settings, anew at each use.
    assert_eq!(
        crate::lane::Settings::of(Some(&file)).pressure,
        Thresholds::of(Some(&file))
    );
}

#[test]
fn the_cold_starts_of_the_last_hour_are_counted_by_tool() {
    let clock = Arc::new(Hand::default());
    let gate = gate(None, clock.clone());
    assert_eq!(gate.cold_starts(), ColdStarts::default());
    gate.started_cold("claude");
    clock.0.store(30 * 60_000, Ordering::SeqCst);
    gate.started_cold("codex");
    gate.started_cold("claude");
    let counted = gate.cold_starts();
    assert_eq!(counted.total, 3);
    assert_eq!(
        counted.tools,
        BTreeMap::from([("claude".to_owned(), 2), ("codex".to_owned(), 1)])
    );
    // An hour after the first, it is no longer counted.
    clock.0.store(60 * 60_000, Ordering::SeqCst);
    let counted = gate.cold_starts();
    assert_eq!(counted.total, 2);
    assert_eq!(
        counted.tools,
        BTreeMap::from([("claude".to_owned(), 1), ("codex".to_owned(), 1)])
    );
    clock.0.store(91 * 60_000, Ordering::SeqCst);
    assert_eq!(gate.cold_starts(), ColdStarts::default());
}

#[test]
fn the_cpu_figure_is_the_hosts_own_cgroup_where_it_can_be_read() {
    let fake = Fake::new("cgroup");
    // The machine's processors stall on the agents' own work; the Host's cgroup is calm.
    fake.set(90.0, 0.0, 0.0);
    let own = "some avg10=4.00 avg60=5.00 avg300=1.00 total=9\n\
               full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n";
    std::fs::write(fake.0.join("host-cpu"), own).unwrap();
    let gate = gate(Some(fake.0.clone()), Arc::new(Hand::default()));
    let look = gate.look();
    assert!(!look.high, "a busy machine with a calm Host held work");
    assert_eq!(look.reading.cpu_source, CpuSource::HostCgroup);
    assert_eq!(look.reading.cpu.unwrap().some.avg60, 5.0);
    let (count, order) = (Arc::default(), Arc::default());
    assert!(!gate.hold("a", 1, counted(&count, &order, 1), quiet()));
    // Where the Host's cgroup cannot be read, the machine's figure counts.
    std::fs::remove_file(fake.0.join("host-cpu")).unwrap();
    let look = gate.look();
    assert!(look.high);
    assert_eq!(look.reading.cpu_source, CpuSource::System);
    // io and memory are the machine's either way.
    std::fs::write(fake.0.join("host-cpu"), own).unwrap();
    fake.set(90.0, 45.0, 0.0);
    assert!(
        gate.high(),
        "the machine's io counts whatever the Host's cpu"
    );
}

#[test]
fn the_hosts_cgroup_is_found_from_its_v2_line() {
    let text = "12:cpu,cpuacct:/old\n0::/user.slice/user-1000.slice/user@1000.service/app.slice/clarp-host.service\n";
    assert_eq!(
        cgroup_cpu_of(text).unwrap(),
        PathBuf::from(
            "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/clarp-host.service/cpu.pressure"
        )
    );
    assert_eq!(
        cgroup_cpu_of("0::/\n").unwrap(),
        PathBuf::from("/sys/fs/cgroup/cpu.pressure")
    );
    assert_eq!(cgroup_cpu_of("1:name=systemd:/x\n"), None);
}

#[test]
fn a_waiting_turn_says_what_is_high_and_is_told_when_the_shown_percent_changes() {
    let fake = Fake::new("words");
    fake.set(0.0, 45.4, 0.0);
    let gate = gate(Some(fake.0.clone()), Arc::new(Hand::default()));
    let said: Arc<Mutex<Vec<String>>> = Arc::default();
    let heard = said.clone();
    let say: Box<dyn Fn(&str) + Send> = Box::new(move |words: &str| {
        heard.lock().unwrap().push(words.to_owned());
    });
    let (count, order) = (Arc::default(), Arc::default());
    assert!(gate.hold("a", 1, counted(&count, &order, 1), say));
    assert_eq!(gate.words(), "Waiting for the machine (io 45%)");
    // The first look tells it; the same shown percent again tells nothing.
    assert_eq!(gate.tick(), Some(EVERY));
    fake.set(0.0, 45.2, 0.0);
    assert_eq!(gate.tick(), Some(EVERY));
    assert_eq!(*said.lock().unwrap(), ["Waiting for the machine (io 45%)"]);
    // Another percent, or another figure that is higher, is told.
    fake.set(0.0, 52.0, 0.0);
    assert_eq!(gate.tick(), Some(EVERY));
    fake.set(91.0, 52.0, 0.0);
    assert_eq!(gate.tick(), Some(EVERY));
    assert_eq!(
        *said.lock().unwrap(),
        [
            "Waiting for the machine (io 45%)",
            "Waiting for the machine (io 52%)",
            "Waiting for the machine (cpu 91%)",
        ]
    );
    // In the band where it stays high below every threshold, the figure nearest its own.
    fake.set(50.0, 26.0, 0.0);
    assert!(gate.high());
    assert_eq!(gate.words(), "Waiting for the machine (io 26%)");
    // Calm: the turn goes, with nothing more said.
    fake.set(0.0, 0.0, 0.0);
    assert_eq!(gate.tick(), Some(BETWEEN));
    assert_eq!(said.lock().unwrap().len(), 3);
}

#[test]
fn the_hosts_own_cpu_is_read_only_where_its_turns_run_apart_from_it() {
    let proc = Path::new(PROC_PRESSURE);
    // Started from a terminal, in a container, or with the direct launcher: the machine's.
    assert_eq!(host_cpu_of(proc, false), None);
    // A test's own folder names its own file, whatever the launcher.
    let dir = Path::new("/srv/fake-pressure");
    assert_eq!(
        host_cpu_of(dir, false),
        Some(PathBuf::from("/srv/fake-pressure/host-cpu"))
    );
    // A gate with no file of the Host's own reads the machine's, and says so.
    let fake = Fake::new("apart");
    fake.set(90.0, 0.0, 0.0);
    let gate = Gate::new(
        Some(PathBuf::from(PROC_PRESSURE)),
        Arc::new(Hand::default()),
        false,
    );
    assert_eq!(gate.host_cpu, None);
    let reading = Reading::read(&fake.0, None);
    assert_eq!(reading.cpu_source, CpuSource::System);
    assert_eq!(reading.cpu.unwrap().some.avg60, 90.0);
}

#[test]
fn a_turn_held_in_a_later_episode_is_told_its_words_even_where_they_repeat() {
    let fake = Fake::new("episodes");
    fake.set(0.0, 45.0, 0.0);
    let gate = gate(Some(fake.0.clone()), Arc::new(Hand::default()));
    let said: Arc<Mutex<Vec<(u64, String)>>> = Arc::default();
    let saying = |turn: u64| -> Box<dyn Fn(&str) + Send> {
        let heard = said.clone();
        Box::new(move |words: &str| heard.lock().unwrap().push((turn, words.to_owned())))
    };
    let (count, order) = (Arc::default(), Arc::default());
    // The first episode: held, told, let go once calm, and the queue empties.
    assert!(gate.hold("a", 1, counted(&count, &order, 1), saying(1)));
    assert_eq!(gate.tick(), Some(EVERY));
    fake.set(0.0, 0.0, 0.0);
    assert_eq!(gate.tick(), Some(BETWEEN));
    assert_eq!(gate.tick(), None);
    // A later episode with the same reading: the new turn is told it too.
    fake.set(0.0, 45.0, 0.0);
    assert!(gate.hold("b", 2, counted(&count, &order, 2), saying(2)));
    assert_eq!(gate.tick(), Some(EVERY));
    let words = "Waiting for the machine (io 45%)".to_owned();
    assert_eq!(
        *said.lock().unwrap(),
        [(1, words.clone()), (2, words.clone())]
    );
    // The same, where the queue emptied by a forget before the gate looked again.
    gate.forget("b", 2);
    assert!(gate.hold("c", 3, counted(&count, &order, 3), saying(3)));
    assert_eq!(gate.tick(), Some(EVERY));
    assert_eq!(said.lock().unwrap().last(), Some(&(3, words)));
}
