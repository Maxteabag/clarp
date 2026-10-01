//! QObject over `clarp_core::narrator::Narrator`, owned by the AppController
//! (whose API client carries the Host requests and forwards the replies).
//! The presentation model reads explanations through `explain`, cache-only.

use std::cell::RefCell;
use std::pin::Pin;
use std::time::Duration;

use clarp_core::narrator::{DEBOUNCE_MS, DETAIL_LEVELS, Effect, Narrator, POLL_MS, TIMEOUT_MS};
use clarp_net::ApiClient;
use cxx_qt::{CxxQtType, Threading};
use cxx_qt_lib::{QJsonObject, QString, QStringList};
use serde_json::Value;

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qstringlist.h");
        type QStringList = cxx_qt_lib::QStringList;
        include!("cxx-qt-lib/qjsonobject.h");
        type QJsonObject = cxx_qt_lib::QJsonObject;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(bool, enabled, READ = enabled_value, WRITE = set_enabled, NOTIFY = enabled_changed)]
        #[qproperty(u64, revision, READ = revision_value, NOTIFY = changed)]
        #[qproperty(QString, status, READ = status_value, NOTIFY = status_changed)]
        #[qproperty(QString, diagnostics_path, cxx_name = "diagnosticsPath", READ = diagnostics_path_value, CONSTANT)]
        #[qproperty(bool, unavailable, READ = unavailable_value, NOTIFY = status_changed)]
        #[qproperty(i32, detail_level, cxx_name = "detailLevel", READ = detail_level_value, WRITE = set_detail_level, NOTIFY = detail_level_changed)]
        #[qproperty(QStringList, detail_levels, cxx_name = "detailLevels", READ = detail_levels_value, CONSTANT)]
        #[qproperty(QString, level_description, cxx_name = "levelDescription", READ = level_description_value, NOTIFY = detail_level_changed)]
        type ToolNarrator = super::ToolNarratorRust;
    }

    unsafe extern "RustQt" {
        fn enabled_value(self: &ToolNarrator) -> bool;
        #[cxx_name = "setEnabled"]
        fn set_enabled(self: Pin<&mut ToolNarrator>, enabled: bool);
        fn revision_value(self: &ToolNarrator) -> u64;
        fn status_value(self: &ToolNarrator) -> QString;
        fn diagnostics_path_value(self: &ToolNarrator) -> QString;
        fn unavailable_value(self: &ToolNarrator) -> bool;
        fn detail_level_value(self: &ToolNarrator) -> i32;
        #[cxx_name = "setDetailLevel"]
        fn set_detail_level(self: Pin<&mut ToolNarrator>, level: i32);
        fn detail_levels_value(self: &ToolNarrator) -> QStringList;
        fn level_description_value(self: &ToolNarrator) -> QString;

        #[qsignal]
        #[cxx_name = "enabledChanged"]
        fn enabled_changed(self: Pin<&mut ToolNarrator>);
        #[qsignal]
        fn changed(self: Pin<&mut ToolNarrator>);
        #[qsignal]
        #[cxx_name = "statusChanged"]
        fn status_changed(self: Pin<&mut ToolNarrator>);
        #[qsignal]
        #[cxx_name = "detailLevelChanged"]
        fn detail_level_changed(self: Pin<&mut ToolNarrator>);

        #[qinvokable]
        #[cxx_name = "acquireView"]
        unsafe fn acquire_view(self: Pin<&mut ToolNarrator>, owner: *mut QObject, activity: &QJsonObject);
        #[qinvokable]
        #[cxx_name = "releaseView"]
        unsafe fn release_view(self: Pin<&mut ToolNarrator>, owner: *mut QObject);
        #[qinvokable]
        fn request(self: Pin<&mut ToolNarrator>, activity: &QJsonObject, working_directory: &QString, local_files_allowed: bool);
        #[qinvokable]
        fn explanation(self: &ToolNarrator, activity: &QJsonObject, working_directory: &QString, local_files_allowed: bool) -> QString;
        #[qinvokable]
        fn failed(self: &ToolNarrator, activity: &QJsonObject, working_directory: &QString, local_files_allowed: bool) -> bool;
    }

    #[namespace = "rust::cxxqtlib1"]
    unsafe extern "C++" {
        include!("cxx-qt-lib/common.h");
        #[rust_name = "new_tool_narrator"]
        fn make_unique() -> UniquePtr<ToolNarrator>;
    }

    impl cxx_qt::Threading for ToolNarrator {}
    impl cxx_qt::Initialize for ToolNarrator {}
}

thread_local! {
    static LIVE_NARRATORS: std::cell::RefCell<std::collections::HashSet<usize>> = Default::default();
}

pub struct ToolNarratorRust {
    /// RefCell: lookups memoize keys, and views read through `&self`.
    core: RefCell<Narrator>,
    api: Option<ApiClient>,
    address: usize,
}

impl Default for ToolNarratorRust {
    fn default() -> Self {
        Self { core: RefCell::new(Narrator::new()), api: None, address: 0 }
    }
}

impl Drop for ToolNarratorRust {
    fn drop(&mut self) {
        LIVE_NARRATORS.with(|live| live.borrow_mut().remove(&self.address));
    }
}

impl cxx_qt::Initialize for qobject::ToolNarrator {
    fn initialize(mut self: Pin<&mut Self>) {
        let address = unsafe { self.as_mut().get_unchecked_mut() } as *mut qobject::ToolNarrator as usize;
        self.as_mut().rust_mut().address = address;
        LIVE_NARRATORS.with(|live| live.borrow_mut().insert(address));
    }
}

/// Read access to the live narrator at `address`, if it is one.
///
/// SAFETY: the caller must not keep the reference past the current call.
pub unsafe fn live<'a>(address: usize) -> Option<&'a qobject::ToolNarrator> {
    let live = LIVE_NARRATORS.with(|live| live.borrow().contains(&address));
    live.then(|| unsafe { &*(address as *const qobject::ToolNarrator) })
}

fn activity(object: &QJsonObject) -> serde_json::Map<String, Value> {
    crate::qjson::from_qjson_object(object)
}

impl qobject::ToolNarrator {
    pub fn cache_size(&self) -> usize {
        self.core.borrow().cache_size()
    }

    fn enabled_value(&self) -> bool {
        self.core.borrow().enabled()
    }
    fn revision_value(&self) -> u64 {
        self.core.borrow().revision()
    }
    fn status_value(&self) -> QString {
        QString::from(self.core.borrow().status().as_str())
    }
    fn diagnostics_path_value(&self) -> QString {
        QString::from("Host event log: toolExplanationsBatch")
    }
    fn unavailable_value(&self) -> bool {
        self.core.borrow().unavailable()
    }
    fn detail_level_value(&self) -> i32 {
        self.core.borrow().detail_level()
    }
    fn detail_levels_value(&self) -> QStringList {
        let mut levels = QStringList::default();
        for level in DETAIL_LEVELS {
            levels.append(QString::from(level));
        }
        levels
    }
    fn level_description_value(&self) -> QString {
        QString::from(self.core.borrow().level_description())
    }

    /// The Host client the controller shares; replies come back through
    /// `handle_reply`/`handle_failure`.
    pub fn set_api(mut self: Pin<&mut Self>, api: ApiClient) {
        let effects = self.core.borrow_mut().reset();
        self.as_mut().rust_mut().api = Some(api);
        self.perform(effects);
    }

    pub fn owns_tag(&self, tag: &str) -> bool {
        tag == "explanation-release" || self.core.borrow().is_own_tag(tag)
    }

    pub fn handle_reply(self: Pin<&mut Self>, tag: &str, object: &serde_json::Map<String, Value>) {
        let effects = self.core.borrow_mut().handle_reply(tag, object);
        self.perform(effects);
    }

    pub fn handle_failure(self: Pin<&mut Self>, tag: &str, status: u16) {
        let effects = self.core.borrow_mut().handle_failure(tag, status);
        self.perform(effects);
    }

    pub fn reset(self: Pin<&mut Self>) {
        let effects = self.core.borrow_mut().reset();
        self.perform(effects);
    }

    pub fn enabled_value_pub(&self) -> bool {
        self.core.borrow().enabled()
    }

    pub fn unavailable_value_pub(&self) -> bool {
        self.core.borrow().unavailable()
    }

    pub fn detail_level(&self) -> i32 {
        self.core.borrow().detail_level()
    }

    /// Cache-only lookup for the presentation model's explanation runs.
    pub fn explain(&self, activity: &serde_json::Map<String, Value>) -> String {
        self.core.borrow_mut().explanation(activity)
    }

    fn perform(mut self: Pin<&mut Self>, effects: Vec<Effect>) {
        for effect in effects {
            match effect {
                Effect::HostRequest { tag, body } => {
                    if let Some(api) = self.api.as_ref() {
                        api.post_json(&tag, "/tool-explanations", body, None);
                    }
                }
                Effect::Debounce => self.as_mut().schedule(DEBOUNCE_MS, |narrator| {
                    let effects = narrator.core.borrow_mut().start_batch();
                    narrator.perform(effects);
                }),
                Effect::Poll { tag } => self.as_mut().schedule(POLL_MS, move |narrator| {
                    let effects = narrator.core.borrow_mut().poll_for(&tag);
                    narrator.perform(effects);
                }),
                Effect::Timeout { generation } => self.as_mut().schedule(TIMEOUT_MS, move |narrator| {
                    let effects = narrator.core.borrow_mut().timed_out(generation);
                    narrator.perform(effects);
                }),
                Effect::Changed => {
                    self.as_mut().changed();
                    self.as_mut().status_changed();
                }
                Effect::DetailLevelChanged => self.as_mut().detail_level_changed(),
                Effect::EnabledChanged => self.as_mut().enabled_changed(),
            }
        }
    }

    fn schedule(self: Pin<&mut Self>, delay_ms: u64, work: impl FnOnce(Pin<&mut qobject::ToolNarrator>) + Send + 'static) {
        let qt = self.qt_thread();
        crate::runtime::after(Duration::from_millis(delay_ms), move || {
            if qt.queue(work).is_err() {
                eprintln!("ToolNarrator: dropped a timer; the narrator is gone");
            }
        });
    }

    fn set_enabled(self: Pin<&mut Self>, enabled: bool) {
        let effects = self.core.borrow_mut().set_enabled(enabled);
        self.perform(effects);
    }

    pub fn set_detail_level(self: Pin<&mut Self>, level: i32) {
        let effects = self.core.borrow_mut().set_detail_level(level);
        self.perform(effects);
    }

    unsafe fn acquire_view(self: Pin<&mut Self>, owner: *mut qobject::QObject, activity_object: &QJsonObject) {
        if owner.is_null() {
            return;
        }
        let effects = self.core.borrow_mut().acquire_view(owner as u64, &activity(activity_object));
        self.perform(effects);
    }

    unsafe fn release_view(self: Pin<&mut Self>, owner: *mut qobject::QObject) {
        let effects = self.core.borrow_mut().release_view(owner as u64);
        self.perform(effects);
    }

    // Script context is local-codex only, which this client does not run.
    fn request(self: Pin<&mut Self>, activity_object: &QJsonObject, _working_directory: &QString, _local_files_allowed: bool) {
        let effects = self.core.borrow_mut().request(&activity(activity_object));
        self.perform(effects);
    }

    fn explanation(&self, activity_object: &QJsonObject, _working_directory: &QString, _local_files_allowed: bool) -> QString {
        QString::from(self.explain(&activity(activity_object)).as_str())
    }

    fn failed(&self, activity_object: &QJsonObject, _working_directory: &QString, _local_files_allowed: bool) -> bool {
        self.core.borrow_mut().failed(&activity(activity_object))
    }
}
