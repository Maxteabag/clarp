//! TranscriptLayout: the transcript's own virtualized list (C++
//! `TranscriptLayout`, see docs/transcript-view.md). A QQuickItem that lays
//! out rows from heights it knows; `clarp_core::transcript_layout::Geometry`
//! does the arithmetic, this item creates delegates like the C++ one (role
//! values only for properties the delegate declares, a live `model` map and
//! `index`), measures them, and keeps the Flickable on the anchor or the end.

use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::pin::Pin;
use std::time::Instant;

use clarp_core::transcript_layout::{Geometry, MARGIN_CREATION_BUDGET_MS, MAX_LAYOUT_PASSES};
use cxx_qt::{CxxQtType, QMetaObjectConnectionGuard, Threading};
use cxx_qt_lib::{QList, QMap, QMapPair_QString_QVariant, QModelIndex, QObjectMutPtr, QRectF, QString, QVariant};

use super::quick::{self, ffi as q};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!(<QtQuick/QQuickItem>);
        type QQuickItem;
        include!(<QtQml/QQmlComponent>);
        type QQmlComponent;
        include!(<QtCore/QAbstractItemModel>);
        type QAbstractItemModel;
        include!(<QtQuick/QQuickWindow>);
        type QQuickWindow;
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qrectf.h");
        type QRectF = cxx_qt_lib::QRectF;
        include!("cxx-qt-lib/qmodelindex.h");
        type QModelIndex = cxx_qt_lib::QModelIndex;
        include!("cxx-qt-lib/qlist.h");
        type QList_i32 = cxx_qt_lib::QList<i32>;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[base = QQuickItem]
        #[qproperty(*mut QAbstractItemModel, model, READ = model_value, WRITE = set_model, NOTIFY = model_changed)]
        #[qproperty(*mut QQmlComponent, delegate, READ = delegate_value, WRITE = set_delegate, NOTIFY = delegate_changed)]
        #[qproperty(*mut QQmlComponent, section_delegate, cxx_name = "sectionDelegate", READ = section_delegate_value, WRITE = set_section_delegate, NOTIFY = section_delegate_changed)]
        #[qproperty(QString, section_role, cxx_name = "sectionRole", READ = section_role_value, WRITE = set_section_role, NOTIFY = section_role_changed)]
        #[qproperty(QString, estimate_role, cxx_name = "estimateRole", READ = estimate_role_value, WRITE = set_estimate_role, NOTIFY = estimate_role_changed)]
        #[qproperty(QString, identity_role, cxx_name = "identityRole", READ = identity_role_value, WRITE = set_identity_role, NOTIFY = identity_role_changed)]
        #[qproperty(*mut QQuickItem, header, READ = header_value, WRITE = set_header, NOTIFY = header_changed)]
        #[qproperty(*mut QQuickItem, footer, READ = footer_value, WRITE = set_footer, NOTIFY = footer_changed)]
        #[qproperty(*mut QQuickItem, flickable, READ = flickable_value, WRITE = set_flickable, NOTIFY = flickable_changed)]
        #[qproperty(bool, following, READ = following_value, WRITE = set_following, NOTIFY = following_changed)]
        #[qproperty(f64, spacing, READ = spacing_value, WRITE = set_spacing, NOTIFY = spacing_changed)]
        #[qproperty(f64, left_margin, cxx_name = "leftMargin", READ = left_margin_value, WRITE = set_left_margin, NOTIFY = margins_changed)]
        #[qproperty(f64, right_margin, cxx_name = "rightMargin", READ = right_margin_value, WRITE = set_right_margin, NOTIFY = margins_changed)]
        #[qproperty(f64, top_margin, cxx_name = "topMargin", READ = top_margin_value, WRITE = set_top_margin, NOTIFY = margins_changed)]
        #[qproperty(f64, bottom_margin, cxx_name = "bottomMargin", READ = bottom_margin_value, WRITE = set_bottom_margin, NOTIFY = margins_changed)]
        #[qproperty(f64, cache_extent, cxx_name = "cacheExtent", READ = cache_extent_value, WRITE = set_cache_extent, NOTIFY = cache_extent_changed)]
        #[qproperty(i32, creation_budget, cxx_name = "creationBudget", READ = creation_budget_value, WRITE = set_creation_budget, NOTIFY = creation_budget_changed)]
        #[qproperty(i32, count, READ = count_value, NOTIFY = count_changed)]
        #[qproperty(f64, content_height, cxx_name = "contentHeight", READ = content_height_value, NOTIFY = content_height_changed)]
        #[qproperty(i32, measured_count, cxx_name = "measuredCount", READ = measured_count_value, NOTIFY = content_height_changed)]
        type TranscriptLayout = super::TranscriptLayoutRust;
    }

    unsafe extern "RustQt" {
        fn model_value(self: &TranscriptLayout) -> *mut QAbstractItemModel;
        #[cxx_name = "setModel"]
        unsafe fn set_model(self: Pin<&mut TranscriptLayout>, model: *mut QAbstractItemModel);
        fn delegate_value(self: &TranscriptLayout) -> *mut QQmlComponent;
        #[cxx_name = "setDelegate"]
        unsafe fn set_delegate(self: Pin<&mut TranscriptLayout>, delegate: *mut QQmlComponent);
        fn section_delegate_value(self: &TranscriptLayout) -> *mut QQmlComponent;
        #[cxx_name = "setSectionDelegate"]
        unsafe fn set_section_delegate(self: Pin<&mut TranscriptLayout>, delegate: *mut QQmlComponent);
        fn section_role_value(self: &TranscriptLayout) -> QString;
        #[cxx_name = "setSectionRole"]
        fn set_section_role(self: Pin<&mut TranscriptLayout>, role: &QString);
        fn estimate_role_value(self: &TranscriptLayout) -> QString;
        #[cxx_name = "setEstimateRole"]
        fn set_estimate_role(self: Pin<&mut TranscriptLayout>, role: &QString);
        fn identity_role_value(self: &TranscriptLayout) -> QString;
        #[cxx_name = "setIdentityRole"]
        fn set_identity_role(self: Pin<&mut TranscriptLayout>, role: &QString);
        fn header_value(self: &TranscriptLayout) -> *mut QQuickItem;
        #[cxx_name = "setHeader"]
        unsafe fn set_header(self: Pin<&mut TranscriptLayout>, item: *mut QQuickItem);
        fn footer_value(self: &TranscriptLayout) -> *mut QQuickItem;
        #[cxx_name = "setFooter"]
        unsafe fn set_footer(self: Pin<&mut TranscriptLayout>, item: *mut QQuickItem);
        fn flickable_value(self: &TranscriptLayout) -> *mut QQuickItem;
        #[cxx_name = "setFlickable"]
        unsafe fn set_flickable(self: Pin<&mut TranscriptLayout>, item: *mut QQuickItem);
        fn following_value(self: &TranscriptLayout) -> bool;
        #[cxx_name = "setFollowing"]
        fn set_following(self: Pin<&mut TranscriptLayout>, following: bool);
        fn spacing_value(self: &TranscriptLayout) -> f64;
        #[cxx_name = "setSpacing"]
        fn set_spacing(self: Pin<&mut TranscriptLayout>, value: f64);
        fn left_margin_value(self: &TranscriptLayout) -> f64;
        #[cxx_name = "setLeftMargin"]
        fn set_left_margin(self: Pin<&mut TranscriptLayout>, value: f64);
        fn right_margin_value(self: &TranscriptLayout) -> f64;
        #[cxx_name = "setRightMargin"]
        fn set_right_margin(self: Pin<&mut TranscriptLayout>, value: f64);
        fn top_margin_value(self: &TranscriptLayout) -> f64;
        #[cxx_name = "setTopMargin"]
        fn set_top_margin(self: Pin<&mut TranscriptLayout>, value: f64);
        fn bottom_margin_value(self: &TranscriptLayout) -> f64;
        #[cxx_name = "setBottomMargin"]
        fn set_bottom_margin(self: Pin<&mut TranscriptLayout>, value: f64);
        fn cache_extent_value(self: &TranscriptLayout) -> f64;
        #[cxx_name = "setCacheExtent"]
        fn set_cache_extent(self: Pin<&mut TranscriptLayout>, value: f64);
        fn creation_budget_value(self: &TranscriptLayout) -> i32;
        #[cxx_name = "setCreationBudget"]
        fn set_creation_budget(self: Pin<&mut TranscriptLayout>, value: i32);
        fn count_value(self: &TranscriptLayout) -> i32;
        fn content_height_value(self: &TranscriptLayout) -> f64;
        fn measured_count_value(self: &TranscriptLayout) -> i32;

        #[qsignal]
        #[cxx_name = "modelChanged"]
        fn model_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "delegateChanged"]
        fn delegate_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "sectionDelegateChanged"]
        fn section_delegate_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "sectionRoleChanged"]
        fn section_role_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "estimateRoleChanged"]
        fn estimate_role_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "identityRoleChanged"]
        fn identity_role_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "headerChanged"]
        fn header_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "footerChanged"]
        fn footer_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "flickableChanged"]
        fn flickable_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "followingChanged"]
        fn following_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "spacingChanged"]
        fn spacing_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "marginsChanged"]
        fn margins_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "cacheExtentChanged"]
        fn cache_extent_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "creationBudgetChanged"]
        fn creation_budget_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "countChanged"]
        fn count_changed(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "contentHeightChanged"]
        fn content_height_changed(self: Pin<&mut TranscriptLayout>);
        /// A correction moved the viewport to keep the anchor or the end.
        #[qsignal]
        #[cxx_name = "viewportCorrected"]
        fn viewport_corrected(self: Pin<&mut TranscriptLayout>, from: f64, to: f64);

        // Forwarding targets for signals connected by signature (the model's
        // are private, the Flickable's type is private).
        #[qsignal]
        #[cxx_name = "sourceRowsInserted"]
        fn source_rows_inserted(self: Pin<&mut TranscriptLayout>, parent: &QModelIndex, first: i32, last: i32);
        #[qsignal]
        #[cxx_name = "sourceRowsAboutToBeRemoved"]
        fn source_rows_about_to_be_removed(self: Pin<&mut TranscriptLayout>, parent: &QModelIndex, first: i32, last: i32);
        #[qsignal]
        #[cxx_name = "sourceRowsRemoved"]
        fn source_rows_removed(self: Pin<&mut TranscriptLayout>, parent: &QModelIndex, first: i32, last: i32);
        #[qsignal]
        #[cxx_name = "sourceDataChanged"]
        fn source_data_changed(self: Pin<&mut TranscriptLayout>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList_i32);
        #[qsignal]
        #[cxx_name = "sourceAboutToRebuild"]
        fn source_about_to_rebuild(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "sourceRebuilt"]
        fn source_rebuilt(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "flickableMoved"]
        fn flickable_moved(self: Pin<&mut TranscriptLayout>);
        #[qsignal]
        #[cxx_name = "layoutRequested"]
        fn layout_requested(self: Pin<&mut TranscriptLayout>);

        #[qinvokable]
        #[cxx_name = "positionOf"]
        fn position_of(self: Pin<&mut TranscriptLayout>, row: i32) -> f64;
        #[qinvokable]
        #[cxx_name = "heightOf"]
        fn height_of(self: &TranscriptLayout, row: i32) -> f64;
        #[qinvokable]
        #[cxx_name = "rowAt"]
        fn row_at(self: Pin<&mut TranscriptLayout>, y: f64) -> i32;
        #[qinvokable]
        #[cxx_name = "itemAt"]
        fn item_at(self: &TranscriptLayout, row: i32) -> *mut QQuickItem;
        #[qinvokable]
        #[cxx_name = "isMeasured"]
        fn is_measured(self: &TranscriptLayout, row: i32) -> bool;
        #[qinvokable]
        #[cxx_name = "endY"]
        fn end_y(self: Pin<&mut TranscriptLayout>) -> f64;
        #[qinvokable]
        #[cxx_name = "anchorToViewport"]
        fn anchor_to_viewport(self: Pin<&mut TranscriptLayout>);
        #[qinvokable]
        #[cxx_name = "positionAtRow"]
        fn position_at_row(self: Pin<&mut TranscriptLayout>, row: i32, at_bottom: bool);
        #[qinvokable]
        #[cxx_name = "layoutNow"]
        fn layout_now(self: Pin<&mut TranscriptLayout>);
        #[qinvokable]
        #[cxx_name = "rememberAnchorIdentity"]
        fn remember_anchor_identity(self: Pin<&mut TranscriptLayout>);
        #[qinvokable]
        #[cxx_name = "restoreAnchorIdentity"]
        fn restore_anchor_identity(self: Pin<&mut TranscriptLayout>);

        #[cxx_override]
        #[cxx_name = "updatePolish"]
        fn update_polish(self: Pin<&mut TranscriptLayout>);
        #[cxx_override]
        #[cxx_name = "geometryChange"]
        fn geometry_change(self: Pin<&mut TranscriptLayout>, new_geometry: &QRectF, old_geometry: &QRectF);
        #[cxx_override]
        #[cxx_name = "componentComplete"]
        fn component_complete(self: Pin<&mut TranscriptLayout>);

        #[inherit]
        #[cxx_name = "geometryChange"]
        fn base_geometry_change(self: Pin<&mut TranscriptLayout>, new_geometry: &QRectF, old_geometry: &QRectF);
        #[inherit]
        #[cxx_name = "componentComplete"]
        fn base_component_complete(self: Pin<&mut TranscriptLayout>);
        #[inherit]
        fn polish(self: Pin<&mut TranscriptLayout>);
        #[inherit]
        fn window(self: &TranscriptLayout) -> *mut QQuickWindow;
        #[inherit]
        #[cxx_name = "isVisible"]
        fn is_visible(self: &TranscriptLayout) -> bool;
        #[inherit]
        #[cxx_name = "isComponentComplete"]
        fn is_component_complete(self: &TranscriptLayout) -> bool;
        #[inherit]
        fn width(self: &TranscriptLayout) -> f64;
        #[inherit]
        fn height(self: &TranscriptLayout) -> f64;
        #[inherit]
        #[cxx_name = "setHeight"]
        fn set_item_height(self: Pin<&mut TranscriptLayout>, height: f64);
        #[inherit]
        #[cxx_name = "setImplicitHeight"]
        fn set_implicit_height(self: Pin<&mut TranscriptLayout>, height: f64);
        #[inherit]
        #[qsignal]
        #[cxx_name = "visibleChanged"]
        fn visible_changed(self: Pin<&mut TranscriptLayout>);
    }

    impl cxx_qt::Threading for TranscriptLayout {}
    impl cxx_qt::Initialize for TranscriptLayout {}
}

/// The delegates of one row.
struct RowItems {
    item: *mut q::QQuickItem,
    section: *mut q::QQuickItem,
    model_data: *mut q::QQmlPropertyMap,
    guards: Vec<QMetaObjectConnectionGuard>,
}

impl Default for RowItems {
    fn default() -> Self {
        Self { item: std::ptr::null_mut(), section: std::ptr::null_mut(), model_data: std::ptr::null_mut(), guards: Vec::new() }
    }
}

impl RowItems {
    fn release(&mut self) {
        self.guards.clear();
        for item in [std::mem::replace(&mut self.item, std::ptr::null_mut()), std::mem::replace(&mut self.section, std::ptr::null_mut())] {
            if let Some(visible) = quick::pin(item) {
                visible.set_visible(false);
                if let Some(object) = quick::pin(quick::item_object(item)) {
                    object.delete_later();
                }
            }
        }
        if let Some(map) = quick::pin(std::mem::replace(&mut self.model_data, std::ptr::null_mut()).cast::<q::QObject>()) {
            map.delete_later();
        }
    }
}

pub struct TranscriptLayoutRust {
    geometry: Geometry,
    items: Vec<RowItems>,
    model: *mut qobject::QAbstractItemModel,
    model_guards: Vec<QMetaObjectConnectionGuard>,
    delegate: *mut qobject::QQmlComponent,
    section_delegate: *mut qobject::QQmlComponent,
    section_role: String,
    estimate_role: String,
    identity_role: String,
    header: *mut qobject::QQuickItem,
    footer: *mut qobject::QQuickItem,
    chrome_guards: Vec<QMetaObjectConnectionGuard>,
    flickable: *mut qobject::QQuickItem,
    flickable_guards: Vec<QMetaObjectConnectionGuard>,
    creation_budget: i32,
    creation_pending: bool,
    /// role name -> role id
    role_ids: HashMap<String, i32>,
    /// Whether the delegate declares a property, learned from the first row.
    delegate_properties: HashMap<String, bool>,
    correcting: bool,
    in_layout: bool,
}

impl Default for TranscriptLayoutRust {
    fn default() -> Self {
        Self {
            geometry: Geometry::default(),
            items: Vec::new(),
            model: std::ptr::null_mut(),
            model_guards: Vec::new(),
            delegate: std::ptr::null_mut(),
            section_delegate: std::ptr::null_mut(),
            section_role: String::new(),
            estimate_role: "body".into(),
            identity_role: "rowKey".into(),
            header: std::ptr::null_mut(),
            footer: std::ptr::null_mut(),
            chrome_guards: Vec::new(),
            flickable: std::ptr::null_mut(),
            flickable_guards: Vec::new(),
            creation_budget: 0,
            creation_pending: false,
            role_ids: HashMap::new(),
            delegate_properties: HashMap::new(),
            correcting: false,
            in_layout: false,
        }
    }
}

impl Drop for TranscriptLayoutRust {
    fn drop(&mut self) {
        self.model_guards.clear();
        self.flickable_guards.clear();
        self.chrome_guards.clear();
        for row in &mut self.items {
            row.release();
        }
    }
}

fn source(model: *mut qobject::QAbstractItemModel) -> Option<&'static q::QAbstractItemModel> {
    // SAFETY: the model outlives its assignment; setModel(null) clears it.
    unsafe { model.cast::<q::QAbstractItemModel>().as_ref() }
}

fn item(pointer: *mut qobject::QQuickItem) -> *mut q::QQuickItem {
    pointer.cast()
}

fn item_height(pointer: *mut q::QQuickItem) -> f64 {
    unsafe { pointer.as_ref() }.map_or(0.0, |i| i.height())
}

fn cstring(text: &str) -> CString {
    CString::new(text).unwrap_or_default()
}

impl cxx_qt::Initialize for qobject::TranscriptLayout {
    fn initialize(mut self: Pin<&mut Self>) {
        let guards = vec![
            self.as_mut().on_source_rows_inserted(|this, parent, first, last| this.rows_inserted(parent, first, last)),
            self.as_mut().on_source_rows_about_to_be_removed(|this, parent, first, last| this.rows_about_to_be_removed(parent, first, last)),
            self.as_mut().on_source_rows_removed(|this, parent, first, last| this.rows_removed(parent, first, last)),
            self.as_mut().on_source_data_changed(|this, top_left, bottom_right, roles| this.data_changed(top_left, bottom_right, roles)),
            self.as_mut().on_source_about_to_rebuild(|this| this.remember_anchor_identity()),
            self.as_mut().on_source_rebuilt(|mut this| {
                this.as_mut().reset_rows();
                this.restore_anchor_identity();
            }),
            self.as_mut().on_flickable_moved(|this| this.flickable_moved_handler()),
            // The layout pass measures every created row, so a height change
            // only has to schedule one, and not from inside that pass.
            self.as_mut().on_layout_requested(|this| {
                if !this.in_layout {
                    this.schedule_layout();
                }
            }),
            self.as_mut().on_visible_changed(|this| {
                if this.is_visible() {
                    this.schedule_layout();
                }
            }),
        ];
        for guard in guards {
            guard.release();
        }
    }
}

impl qobject::TranscriptLayout {
    fn self_object(&self) -> *const q::QObject {
        (self as *const Self).cast()
    }

    fn self_item(self: Pin<&mut Self>) -> *mut q::QQuickItem {
        (unsafe { self.get_unchecked_mut() } as *mut Self).cast()
    }

    // ---- properties --------------------------------------------------------

    fn model_value(&self) -> *mut qobject::QAbstractItemModel {
        self.model
    }
    fn delegate_value(&self) -> *mut qobject::QQmlComponent {
        self.delegate
    }
    fn section_delegate_value(&self) -> *mut qobject::QQmlComponent {
        self.section_delegate
    }
    fn section_role_value(&self) -> QString {
        QString::from(self.section_role.as_str())
    }
    fn estimate_role_value(&self) -> QString {
        QString::from(self.estimate_role.as_str())
    }
    fn identity_role_value(&self) -> QString {
        QString::from(self.identity_role.as_str())
    }
    fn header_value(&self) -> *mut qobject::QQuickItem {
        self.header
    }
    fn footer_value(&self) -> *mut qobject::QQuickItem {
        self.footer
    }
    fn flickable_value(&self) -> *mut qobject::QQuickItem {
        self.flickable
    }
    fn following_value(&self) -> bool {
        self.geometry.following()
    }
    fn spacing_value(&self) -> f64 {
        self.geometry.spacing
    }
    fn left_margin_value(&self) -> f64 {
        self.geometry.left_margin
    }
    fn right_margin_value(&self) -> f64 {
        self.geometry.right_margin
    }
    fn top_margin_value(&self) -> f64 {
        self.geometry.top_margin
    }
    fn bottom_margin_value(&self) -> f64 {
        self.geometry.bottom_margin
    }
    fn cache_extent_value(&self) -> f64 {
        self.geometry.cache_extent
    }
    fn creation_budget_value(&self) -> i32 {
        self.creation_budget
    }
    fn count_value(&self) -> i32 {
        self.geometry.count() as i32
    }
    /// The height the last layout pass gave the item (C++ recomputes the
    /// same sum; its NOTIFY only fires from the layout pass).
    fn content_height_value(&self) -> f64 {
        self.height()
    }
    fn measured_count_value(&self) -> i32 {
        self.geometry.measured_count() as i32
    }

    unsafe fn set_model(mut self: Pin<&mut Self>, model: *mut qobject::QAbstractItemModel) {
        if self.model == model {
            return;
        }
        self.as_mut().rust_mut().model_guards.clear();
        self.as_mut().rust_mut().model = model;
        if !model.is_null() {
            let (sender, receiver) = (model.cast::<q::QObject>().cast_const(), self.self_object());
            let forwards: [(&CStr, &CStr); 10] = [
                (c"rowsInserted(QModelIndex,int,int)", c"sourceRowsInserted(QModelIndex,int,int)"),
                (c"rowsAboutToBeRemoved(QModelIndex,int,int)", c"sourceRowsAboutToBeRemoved(QModelIndex,int,int)"),
                (c"rowsRemoved(QModelIndex,int,int)", c"sourceRowsRemoved(QModelIndex,int,int)"),
                (c"dataChanged(QModelIndex,QModelIndex,QList<int>)", c"sourceDataChanged(QModelIndex,QModelIndex,QList<int>)"),
                (c"modelAboutToBeReset()", c"sourceAboutToRebuild()"),
                (c"modelReset()", c"sourceRebuilt()"),
                (c"layoutAboutToBeChanged()", c"sourceAboutToRebuild()"),
                (c"layoutChanged()", c"sourceRebuilt()"),
                (c"rowsAboutToBeMoved(QModelIndex,int,int,QModelIndex,int)", c"sourceAboutToRebuild()"),
                (c"rowsMoved(QModelIndex,int,int,QModelIndex,int)", c"sourceRebuilt()"),
            ];
            let guards: Vec<_> =
                forwards.iter().filter_map(|(from, to)| unsafe { quick::forward_signal(sender, from, receiver, to) }).collect();
            self.as_mut().rust_mut().model_guards = guards;
        }
        self.as_mut().rust_mut().geometry.clear_anchor();
        self.as_mut().reset_rows();
        self.model_changed();
    }

    unsafe fn set_delegate(mut self: Pin<&mut Self>, delegate: *mut qobject::QQmlComponent) {
        if self.delegate == delegate {
            return;
        }
        self.as_mut().rust_mut().delegate = delegate;
        self.as_mut().rust_mut().delegate_properties.clear();
        self.as_mut().reset_rows();
        self.delegate_changed();
    }

    unsafe fn set_section_delegate(mut self: Pin<&mut Self>, delegate: *mut qobject::QQmlComponent) {
        if self.section_delegate == delegate {
            return;
        }
        self.as_mut().rust_mut().section_delegate = delegate;
        self.as_mut().rust_mut().geometry.has_section_delegate = !delegate.is_null();
        self.as_mut().reset_rows();
        self.section_delegate_changed();
    }

    fn set_section_role(mut self: Pin<&mut Self>, role: &QString) {
        let role = role.to_string();
        if self.section_role == role {
            return;
        }
        self.as_mut().rust_mut().section_role = role;
        self.as_mut().reset_rows();
        self.section_role_changed();
    }

    fn set_estimate_role(mut self: Pin<&mut Self>, role: &QString) {
        let role = role.to_string();
        if self.estimate_role != role {
            self.as_mut().rust_mut().estimate_role = role;
            self.estimate_role_changed();
        }
    }

    fn set_identity_role(mut self: Pin<&mut Self>, role: &QString) {
        let role = role.to_string();
        if self.identity_role != role {
            self.as_mut().rust_mut().identity_role = role;
            self.identity_role_changed();
        }
    }

    unsafe fn adopt_chrome(mut self: Pin<&mut Self>, pointer: *mut qobject::QQuickItem) {
        let this = self.as_mut().self_item();
        if let Some(chrome) = quick::pin(item(pointer)) {
            unsafe { chrome.set_parent_item(this) };
            let guard = unsafe { quick::forward_signal(pointer.cast::<q::QObject>(), c"heightChanged()", self.self_object(), c"layoutRequested()") };
            self.as_mut().rust_mut().chrome_guards.extend(guard);
        }
    }

    unsafe fn set_header(mut self: Pin<&mut Self>, pointer: *mut qobject::QQuickItem) {
        if self.header == pointer {
            return;
        }
        self.as_mut().rust_mut().header = pointer;
        self.as_mut().rust_mut().chrome_guards.clear();
        let (header, footer) = (self.header, self.footer);
        unsafe {
            self.as_mut().adopt_chrome(header);
            self.as_mut().adopt_chrome(footer);
        }
        self.as_mut().schedule_layout();
        self.header_changed();
    }

    unsafe fn set_footer(mut self: Pin<&mut Self>, pointer: *mut qobject::QQuickItem) {
        if self.footer == pointer {
            return;
        }
        self.as_mut().rust_mut().footer = pointer;
        self.as_mut().rust_mut().chrome_guards.clear();
        let (header, footer) = (self.header, self.footer);
        unsafe {
            self.as_mut().adopt_chrome(header);
            self.as_mut().adopt_chrome(footer);
        }
        self.as_mut().schedule_layout();
        self.footer_changed();
    }

    unsafe fn set_flickable(mut self: Pin<&mut Self>, pointer: *mut qobject::QQuickItem) {
        if self.flickable == pointer {
            return;
        }
        self.as_mut().rust_mut().flickable_guards.clear();
        self.as_mut().rust_mut().flickable = pointer;
        if !pointer.is_null() {
            let sender = pointer.cast::<q::QObject>().cast_const();
            let receiver = self.self_object();
            let guards: Vec<_> = unsafe {
                // Flickable's C++ type is private; its notify signal is public API.
                [
                    quick::forward_signal(sender, c"contentYChanged()", receiver, c"flickableMoved()"),
                    quick::forward_signal(sender, c"heightChanged()", receiver, c"layoutRequested()"),
                ]
            }
            .into_iter()
            .flatten()
            .collect();
            self.as_mut().rust_mut().flickable_guards = guards;
        }
        self.as_mut().schedule_layout();
        self.flickable_changed();
    }

    fn set_following(mut self: Pin<&mut Self>, following: bool) {
        let viewport = self.viewport_y();
        if self.as_mut().rust_mut().geometry.set_following(following, viewport) {
            self.as_mut().schedule_layout();
            self.following_changed();
        }
    }

    fn set_geometry_value(mut self: Pin<&mut Self>, value: f64, field: fn(&mut Geometry) -> &mut f64) -> bool {
        let mut rust = self.as_mut().rust_mut();
        let slot = field(&mut rust.geometry);
        if (*slot - value).abs() < f64::EPSILON {
            return false;
        }
        *slot = value;
        rust.geometry.invalidate();
        self.schedule_layout();
        true
    }

    fn set_spacing(mut self: Pin<&mut Self>, value: f64) {
        if self.as_mut().set_geometry_value(value, |g| &mut g.spacing) {
            self.spacing_changed();
        }
    }
    fn set_left_margin(mut self: Pin<&mut Self>, value: f64) {
        if self.as_mut().set_geometry_value(value, |g| &mut g.left_margin) {
            self.margins_changed();
        }
    }
    fn set_right_margin(mut self: Pin<&mut Self>, value: f64) {
        if self.as_mut().set_geometry_value(value, |g| &mut g.right_margin) {
            self.margins_changed();
        }
    }
    fn set_top_margin(mut self: Pin<&mut Self>, value: f64) {
        if self.as_mut().set_geometry_value(value, |g| &mut g.top_margin) {
            self.margins_changed();
        }
    }
    fn set_bottom_margin(mut self: Pin<&mut Self>, value: f64) {
        if self.as_mut().set_geometry_value(value, |g| &mut g.bottom_margin) {
            self.margins_changed();
        }
    }
    fn set_cache_extent(mut self: Pin<&mut Self>, value: f64) {
        if self.as_mut().set_geometry_value(value, |g| &mut g.cache_extent) {
            self.cache_extent_changed();
        }
    }

    fn set_creation_budget(mut self: Pin<&mut Self>, value: i32) {
        if self.creation_budget == value {
            return;
        }
        self.as_mut().rust_mut().creation_budget = value;
        if self.creation_pending {
            self.as_mut().schedule_layout();
        }
        self.creation_budget_changed();
    }

    // ---- model data --------------------------------------------------------

    fn role_id(&self, name: &str) -> i32 {
        self.role_ids.get(name).copied().unwrap_or(-1)
    }

    fn identity_role_id(&self) -> i32 {
        self.role_ids.get(&self.identity_role).or_else(|| self.role_ids.get("messageId")).copied().unwrap_or(-1)
    }

    fn value(&self, row: usize, role: i32) -> QVariant {
        match source(self.model) {
            Some(model) if role >= 0 => model.model_data(&model.model_index(row as i32, 0, &QModelIndex::default()), role),
            _ => QVariant::default(),
        }
    }

    fn text(&self, row: usize, role: i32) -> String {
        let value = self.value(row, role);
        value.value::<QString>().map(|s| s.to_string()).unwrap_or_default()
    }

    fn row_texts(&self) -> (impl Fn(usize) -> String + '_, impl Fn(usize) -> String + '_, impl Fn(usize) -> String + '_) {
        let (estimate, section, identity) = (self.role_id(&self.estimate_role), self.role_id(&self.section_role), self.identity_role_id());
        (move |i| self.text(i, estimate), move |i| if section >= 0 { self.text(i, section) } else { String::new() }, move |i| self.text(i, identity))
    }

    // ---- rows --------------------------------------------------------------

    fn release_all(mut self: Pin<&mut Self>) {
        for row in &mut self.as_mut().rust_mut().items {
            row.release();
        }
    }

    fn reset_rows(mut self: Pin<&mut Self>) {
        self.as_mut().release_all();
        let mut role_ids = HashMap::new();
        let mut count = 0;
        if let Some(model) = source(self.model) {
            for (id, name) in model.model_role_names().iter() {
                role_ids.insert(name.to_string(), *id);
            }
            count = model.model_row_count(&QModelIndex::default()).max(0) as usize;
        }
        self.as_mut().rust_mut().role_ids = role_ids;
        let mut geometry = std::mem::take(&mut self.as_mut().rust_mut().geometry);
        {
            let (text, section, _) = self.row_texts();
            geometry.reset(count, text, section);
        }
        let mut rust = self.as_mut().rust_mut();
        rust.geometry = geometry;
        rust.items = (0..count).map(|_| RowItems::default()).collect();
        self.as_mut().count_changed();
        self.schedule_layout();
    }

    fn remember_anchor_identity(mut self: Pin<&mut Self>) {
        let mut geometry = std::mem::take(&mut self.as_mut().rust_mut().geometry);
        geometry.remember_anchor_identity(&self.row_texts().2);
        self.as_mut().rust_mut().geometry = geometry;
    }

    fn restore_anchor_identity(mut self: Pin<&mut Self>) {
        let mut geometry = std::mem::take(&mut self.as_mut().rust_mut().geometry);
        geometry.restore_anchor_identity(&self.row_texts().2);
        self.as_mut().rust_mut().geometry = geometry;
        self.schedule_layout();
    }

    fn rows_inserted(mut self: Pin<&mut Self>, parent: &QModelIndex, first: i32, last: i32) {
        if parent.is_valid() || first < 0 || last < first {
            return;
        }
        let (first, last) = (first as usize, last as usize);
        let mut geometry = std::mem::take(&mut self.as_mut().rust_mut().geometry);
        {
            let (text, section, identity) = self.row_texts();
            geometry.inserted(first, last, text, section, identity);
        }
        let mut rust = self.as_mut().rust_mut();
        rust.geometry = geometry;
        let at = first.min(rust.items.len());
        rust.items.splice(at..at, (first..=last).map(|_| RowItems::default()));
        self.as_mut().count_changed();
        self.schedule_layout();
    }

    fn rows_about_to_be_removed(mut self: Pin<&mut Self>, parent: &QModelIndex, first: i32, last: i32) {
        if parent.is_valid() || first < 0 || last < first {
            return;
        }
        let (first, last) = (first as usize, last as usize);
        let mut geometry = std::mem::take(&mut self.as_mut().rust_mut().geometry);
        geometry.about_to_remove(first, last, &self.row_texts().2);
        let mut rust = self.as_mut().rust_mut();
        rust.geometry = geometry;
        let end = (last + 1).min(rust.items.len());
        for row in &mut rust.items[first.min(end)..end] {
            row.release();
        }
    }

    fn rows_removed(mut self: Pin<&mut Self>, parent: &QModelIndex, first: i32, last: i32) {
        if parent.is_valid() || first < 0 || last < first || self.items.is_empty() {
            return;
        }
        let (first, last) = (first as usize, (last as usize).min(self.items.len() - 1));
        if first > last {
            return;
        }
        let mut rust = self.as_mut().rust_mut();
        rust.geometry.removed(first, last);
        rust.items.drain(first..=last);
        self.as_mut().count_changed();
        self.schedule_layout();
    }

    fn data_changed(mut self: Pin<&mut Self>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList<i32>) {
        if top_left.parent().is_valid() || top_left.row() < 0 {
            return;
        }
        let roles: Vec<i32> = roles.iter().copied().collect();
        let names: Vec<(String, i32)> =
            self.role_ids.iter().filter(|(_, id)| roles.is_empty() || roles.contains(id)).map(|(n, id)| (n.clone(), *id)).collect();
        let (first, last) = (top_left.row() as usize, bottom_right.row().max(top_left.row()) as usize);
        for row in first..(last + 1).min(self.items.len()) {
            let (created_ptr, map) = (self.items[row].item, self.items[row].model_data);
            if created_ptr.is_null() {
                continue;
            }
            for (name, id) in &names {
                let value = self.value(row, *id);
                if let Some(map) = quick::pin(map) {
                    map.insert(&QString::from(name.as_str()), &value);
                }
                if self.delegate_properties.get(name).copied().unwrap_or(false) {
                    let name = cstring(name);
                    let object = quick::pin(quick::item_object(created_ptr));
                    if let Some(object) = object {
                        unsafe { object.set_property(name.as_ptr(), &value) };
                    }
                }
            }
            if self.delegate_properties.get("index").copied().unwrap_or(false) {
                let object = quick::pin(quick::item_object(created_ptr));
                if let Some(object) = object {
                    unsafe { object.set_property(c"index".as_ptr(), &QVariant::from(&(row as i32))) };
                }
            }
        }
        let (estimate, section) = (self.role_id(&self.estimate_role), self.role_id(&self.section_role));
        let text_changed = roles.is_empty() || roles.contains(&estimate);
        let section_changed = section >= 0 && (roles.is_empty() || roles.contains(&section));
        let mut geometry = std::mem::take(&mut self.as_mut().rust_mut().geometry);
        let relabelled = {
            let (text, section_text, _) = self.row_texts();
            geometry.data_changed(first, last, text_changed, section_changed, text, section_text)
        };
        self.as_mut().rust_mut().geometry = geometry;
        for row in relabelled {
            let label = QVariant::from(&QString::from(self.text(row, section).as_str()));
            if let Some(object) = quick::pin(quick::item_object(self.items[row].section)) {
                unsafe { object.set_property(c"section".as_ptr(), &label) };
            }
        }
        self.schedule_layout();
    }

    /// Whether the delegate declares `name`, learned once per delegate.
    fn declares(mut self: Pin<&mut Self>, object: *const q::QObject, name: &str) -> bool {
        if let Some(known) = self.delegate_properties.get(name) {
            return *known;
        }
        let declared = unsafe { quick::has_property(object, name) };
        self.as_mut().rust_mut().delegate_properties.insert(name.to_owned(), declared);
        declared
    }

    fn create_row(mut self: Pin<&mut Self>, row: usize) {
        if !self.items[row].item.is_null() || self.delegate.is_null() || self.model.is_null() {
            return;
        }
        let Some(mut delegate) = quick::pin(self.delegate.cast::<q::QQmlComponent>()) else { return };
        let mut context = delegate.creation_context();
        if context.is_null() {
            context = unsafe { q::qml_context(self.self_object()) };
        }
        if context.is_null() {
            return;
        }
        let names: Vec<(String, i32)> = self.role_ids.iter().map(|(n, id)| (n.clone(), *id)).collect();
        let values: Vec<(String, QVariant)> = names.iter().map(|(n, id)| (n.clone(), self.value(row, *id))).collect();
        let mut map = quick::new_property_map();
        if let Some(mut map) = map.as_mut() {
            for (name, value) in &values {
                map.as_mut().insert(&QString::from(name.as_str()), value);
            }
        }
        let model_data = map.into_raw();

        let object = unsafe { delegate.as_mut().begin_create(context) };
        let created = unsafe { quick::as_item(object) };
        if created.is_null() {
            if let Some(object) = quick::pin(object) {
                delegate.as_mut().complete_create();
                object.delete_later();
            }
            if let Some(map) = quick::pin(model_data.cast::<q::QObject>()) {
                map.delete_later();
            }
            return;
        }
        let mut initial = QMap::<QMapPair_QString_QVariant>::default();
        for (name, value) in values {
            if self.as_mut().declares(object, &name) {
                initial.insert(QString::from(name.as_str()), value);
            }
        }
        if self.as_mut().declares(object, "model") {
            let pointer = unsafe { QObjectMutPtr::from_raw(model_data.cast()) };
            initial.insert(QString::from("model"), QVariant::from(&pointer));
        }
        if self.as_mut().declares(object, "index") {
            initial.insert(QString::from("index"), QVariant::from(&(row as i32)));
        }
        unsafe { delegate.as_mut().set_initial_properties(object, &initial) };
        let this = self.as_mut().self_item();
        let width = self.geometry.row_width();
        let left = self.geometry.left_margin;
        if let Some(mut created) = quick::pin(created) {
            unsafe { created.as_mut().set_parent_item(this) };
            // SAFETY: the layout deletes its delegates, or they go with it.
            unsafe { quick::keep_cpp_owned(object, self.self_object().cast_mut()) };
            delegate.as_mut().complete_create();
            created.as_mut().set_x(left);
            created.as_mut().set_width(width);
        }
        let mut guards = Vec::new();
        guards.extend(unsafe { quick::forward_signal(object, c"heightChanged()", self.self_object(), c"layoutRequested()") });

        let mut section: *mut q::QQuickItem = std::ptr::null_mut();
        let shown = self.geometry.rows()[row].section_shown;
        if let Some(mut section_delegate) = quick::pin(self.section_delegate.cast::<q::QQmlComponent>()).filter(|_| shown) {
            let mut section_context = section_delegate.creation_context();
            if section_context.is_null() {
                section_context = context;
            }
            let section_object = unsafe { section_delegate.as_mut().begin_create(section_context) };
            let section_item = unsafe { quick::as_item(section_object) };
            if section_item.is_null() {
                if let Some(object) = quick::pin(section_object) {
                    section_delegate.as_mut().complete_create();
                    object.delete_later();
                }
            } else {
                let label = self.geometry.rows()[row].section_label.clone();
                let mut properties = QMap::<QMapPair_QString_QVariant>::default();
                properties.insert(QString::from("section"), QVariant::from(&QString::from(label.as_str())));
                unsafe { section_delegate.as_mut().set_initial_properties(section_object, &properties) };
                let width = self.width();
                if let Some(mut heading) = quick::pin(section_item) {
                    unsafe { heading.as_mut().set_parent_item(this) };
                    unsafe { quick::keep_cpp_owned(section_object, self.self_object().cast_mut()) };
                    section_delegate.as_mut().complete_create();
                    heading.as_mut().set_x(0.0);
                    heading.as_mut().set_width(width);
                }
                self.as_mut().rust_mut().geometry.section_measured(item_height(section_item));
                guards.extend(unsafe { quick::forward_signal(section_object, c"heightChanged()", self.self_object(), c"layoutRequested()") });
                section = section_item;
            }
        }
        let mut rust = self.as_mut().rust_mut();
        rust.items[row] = RowItems { item: created, section, model_data, guards };
        rust.geometry.set_created(row, true, !section.is_null());
    }

    // ---- viewport ----------------------------------------------------------

    fn viewport_y(&self) -> f64 {
        match unsafe { self.flickable.cast::<q::QObject>().as_ref() } {
            Some(flickable) => unsafe { flickable.property(c"contentY".as_ptr()) }.value::<f64>().unwrap_or(0.0),
            None => 0.0,
        }
    }

    fn viewport_height(&self) -> f64 {
        if self.flickable.is_null() { self.height() } else { item_height(item(self.flickable)) }
    }

    fn set_viewport_y(mut self: Pin<&mut Self>, y: f64) {
        let Some(flickable) = quick::pin(self.flickable.cast::<q::QObject>()) else { return };
        let from = self.viewport_y();
        if (from - y).abs() <= 0.5 {
            return;
        }
        self.as_mut().rust_mut().correcting = true;
        unsafe { flickable.set_property(c"contentY".as_ptr(), &QVariant::from(&y)) };
        self.as_mut().rust_mut().correcting = false;
        self.viewport_corrected(from, y);
    }

    fn sync_chrome(mut self: Pin<&mut Self>) {
        let (header, footer) = (item_height(item(self.header)), item_height(item(self.footer)));
        let mut rust = self.as_mut().rust_mut();
        rust.geometry.header_height = header;
        rust.geometry.footer_height = footer;
    }

    fn flickable_moved_handler(mut self: Pin<&mut Self>) {
        if self.correcting || self.in_layout {
            return;
        }
        if !self.geometry.following() {
            self.as_mut().anchor_to_viewport();
        }
        self.schedule_layout();
    }

    fn schedule_layout(self: Pin<&mut Self>) {
        if !self.window().is_null() {
            self.polish();
        }
    }

    // ---- invokables --------------------------------------------------------

    fn position_of(mut self: Pin<&mut Self>, row: i32) -> f64 {
        self.as_mut().sync_chrome();
        self.as_mut().rust_mut().geometry.position_of(i64::from(row))
    }

    fn height_of(&self, row: i32) -> f64 {
        self.geometry.height_of(i64::from(row))
    }

    fn row_at(mut self: Pin<&mut Self>, y: f64) -> i32 {
        self.as_mut().sync_chrome();
        self.as_mut().rust_mut().geometry.row_at(y).map_or(-1, |row| row as i32)
    }

    fn item_at(&self, row: i32) -> *mut qobject::QQuickItem {
        usize::try_from(row).ok().and_then(|r| self.items.get(r)).map_or(std::ptr::null_mut(), |r| r.item.cast())
    }

    fn is_measured(&self, row: i32) -> bool {
        self.geometry.is_measured(i64::from(row))
    }

    fn end_y(mut self: Pin<&mut Self>) -> f64 {
        self.as_mut().sync_chrome();
        let height = self.viewport_height();
        self.as_mut().rust_mut().geometry.end_y(height)
    }

    fn anchor_to_viewport(mut self: Pin<&mut Self>) {
        self.as_mut().sync_chrome();
        let y = self.viewport_y();
        self.as_mut().rust_mut().geometry.anchor_to_viewport(y);
    }

    fn position_at_row(mut self: Pin<&mut Self>, row: i32, at_bottom: bool) {
        self.as_mut().sync_chrome();
        let height = self.viewport_height();
        let Some((target, stopped)) = self.as_mut().rust_mut().geometry.position_at_row(i64::from(row), at_bottom, height) else {
            return;
        };
        if stopped {
            self.as_mut().following_changed();
        }
        self.as_mut().set_viewport_y(target);
        self.as_mut().anchor_to_viewport();
        self.relayout();
    }

    fn layout_now(self: Pin<&mut Self>) {
        self.relayout();
    }

    // ---- QQuickItem --------------------------------------------------------

    fn update_polish(self: Pin<&mut Self>) {
        self.relayout();
    }

    fn component_complete(mut self: Pin<&mut Self>) {
        self.as_mut().base_component_complete();
        self.reset_rows();
    }

    fn geometry_change(mut self: Pin<&mut Self>, new_geometry: &QRectF, old_geometry: &QRectF) {
        self.as_mut().base_geometry_change(new_geometry, old_geometry);
        self.as_mut().rust_mut().geometry.set_width(new_geometry.width());
        self.schedule_layout();
    }

    // ---- layout ------------------------------------------------------------

    fn correct_viewport(mut self: Pin<&mut Self>) {
        self.as_mut().sync_chrome();
        let total = self.as_mut().rust_mut().geometry.content_height();
        if (self.height() - total).abs() > 1e-9 {
            self.as_mut().set_implicit_height(total);
            self.as_mut().set_item_height(total);
            self.as_mut().content_height_changed();
        }
        if self.flickable.is_null() {
            return;
        }
        let (y, height) = (self.viewport_y(), self.viewport_height());
        let target = self.as_mut().rust_mut().geometry.correct_target(y, height);
        self.set_viewport_y(target);
    }

    fn place_visible_rows(mut self: Pin<&mut Self>) -> bool {
        let (y, height) = (self.viewport_y(), self.viewport_height());
        let Some(plan) = self.as_mut().rust_mut().geometry.plan(y, height) else { return false };
        for row in plan.release {
            let mut rust = self.as_mut().rust_mut();
            rust.items[row].release();
            rust.geometry.set_created(row, false, false);
        }
        let clock = Instant::now();
        let mut moved = false;
        let width = self.geometry.row_width();
        for row in plan.order {
            if self.items[row].item.is_null() {
                let visible = row >= plan.visible_first && row <= plan.visible_last;
                let budget = if visible { i64::from(self.creation_budget) } else { MARGIN_CREATION_BUDGET_MS };
                if budget > 0 && clock.elapsed().as_millis() as i64 >= budget {
                    self.as_mut().rust_mut().creation_pending = true;
                    continue;
                }
                self.as_mut().create_row(row);
            }
            let (created, section) = (self.items[row].item, self.items[row].section);
            let Some(mut created) = quick::pin(created) else { continue };
            if (created.width() - width).abs() > 1e-9 {
                created.as_mut().set_width(width);
            }
            let measured = item_height(section) + created.height();
            moved |= self.as_mut().rust_mut().geometry.measured(row, measured);
        }
        let full_width = self.width();
        for row in plan.first..=plan.last {
            let top = self.as_mut().rust_mut().geometry.position_of(row as i64);
            let (created, section) = (self.items[row].item, self.items[row].section);
            if let Some(mut heading) = quick::pin(section) {
                heading.as_mut().set_y(top);
                heading.as_mut().set_width(full_width);
            }
            if let Some(created) = quick::pin(created) {
                created.set_y(top + item_height(section));
            }
        }
        moved
    }

    fn relayout(mut self: Pin<&mut Self>) {
        if self.in_layout || !self.is_component_complete() {
            return;
        }
        // A hidden or collapsed transcript builds nothing; it lays out when shown.
        if !self.is_visible() || self.viewport_height() <= 0.0 {
            return;
        }
        self.as_mut().rust_mut().in_layout = true;
        self.as_mut().rust_mut().creation_pending = false;
        for _ in 0..MAX_LAYOUT_PASSES {
            self.as_mut().correct_viewport();
            if !self.as_mut().place_visible_rows() {
                break;
            }
        }
        self.as_mut().correct_viewport();
        let (width, top) = (self.width(), self.geometry.top_margin);
        if let Some(mut header) = quick::pin(item(self.header)) {
            header.as_mut().set_y(top);
            header.as_mut().set_width(width);
        }
        let footer_y = self.as_mut().rust_mut().geometry.footer_y();
        if let Some(mut footer) = quick::pin(item(self.footer)) {
            footer.as_mut().set_y(footer_y);
            footer.as_mut().set_width(width);
        }
        self.as_mut().rust_mut().in_layout = false;
        // Rows left for later frames: continue on the next pass of the event loop.
        if self.creation_pending {
            let queued = self.qt_thread().queue(|this| this.schedule_layout());
            if queued.is_err() {
                eprintln!("TranscriptLayout: could not schedule the remaining rows");
            }
        }
    }
}
