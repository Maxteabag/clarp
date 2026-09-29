//! Qt Quick and QML object APIs the transcript view needs that cxx-qt-lib
//! does not bind: creating delegates from a QQmlComponent the way views do
//! (begin, set initial properties, complete), placing QQuickItems, reading
//! any QAbstractItemModel, and connecting signals by signature (Qt's model
//! signals are private and cannot be named from cxx-qt). Only Qt's own
//! declarations are bound; there is no C++ here.

use std::ffi::CStr;
use std::pin::Pin;

use cxx::UniquePtr;
use cxx_qt::{ConnectionType, QMetaObjectConnectionGuard};
use cxx_qt_lib::{QModelIndex, QString, QVariant};

#[cxx_qt::bridge]
pub mod ffi {
    unsafe extern "C++" {
        include!(<QtCore/QObject>);
        include!(<QtCore/QMetaObject>);
        include!(<QtCore/QAbstractItemModel>);
        include!(<QtQuick/QQuickItem>);
        include!(<QtQml/QQmlComponent>);
        include!(<QtQml/QQmlContext>);
        include!(<QtQml/QQmlPropertyMap>);
        include!(<QtQml/qqml.h>);
        include!("cxx-qt-lib/qvariant.h");
        type QVariant = cxx_qt_lib::QVariant;
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qmodelindex.h");
        type QModelIndex = cxx_qt_lib::QModelIndex;
        include!("cxx-qt-lib/qhash.h");
        type QHash_i32_QByteArray = cxx_qt_lib::QHash<cxx_qt_lib::QHashPair_i32_QByteArray>;
        include!("cxx-qt-lib/qmap.h");
        type QMap_QString_QVariant = cxx_qt_lib::QMap<cxx_qt_lib::QMapPair_QString_QVariant>;
        include!("cxx-qt/connection.h");
        #[namespace = "rust::cxxqt1"]
        type QMetaObjectConnection = cxx_qt::QMetaObjectConnection;
        include!("cxx-qt-lib/common.h");

        type QObject;
        type QMetaObject;
        type QQuickItem;
        type QQmlComponent;
        type QQmlContext;
        type QQmlPropertyMap;
        type QAbstractItemModel;

        #[cxx_name = "metaObject"]
        fn meta_object(self: &QObject) -> *const QMetaObject;
        unsafe fn inherits(self: &QObject, class_name: *const c_char) -> bool;
        unsafe fn property(self: &QObject, name: *const c_char) -> QVariant;
        #[cxx_name = "setProperty"]
        unsafe fn set_property(self: Pin<&mut QObject>, name: *const c_char, value: &QVariant) -> bool;
        #[cxx_name = "deleteLater"]
        fn delete_later(self: Pin<&mut QObject>);
        #[cxx_name = "indexOfProperty"]
        unsafe fn index_of_property(self: &QMetaObject, name: *const c_char) -> i32;
        #[Self = "QObject"]
        unsafe fn connect(
            sender: *const QObject,
            signal: *const c_char,
            receiver: *const QObject,
            method: *const c_char,
            kind: ConnectionType,
        ) -> QMetaObjectConnection;

        fn x(self: &QQuickItem) -> f64;
        fn y(self: &QQuickItem) -> f64;
        fn width(self: &QQuickItem) -> f64;
        fn height(self: &QQuickItem) -> f64;
        #[cxx_name = "setX"]
        fn set_x(self: Pin<&mut QQuickItem>, x: f64);
        #[cxx_name = "setY"]
        fn set_y(self: Pin<&mut QQuickItem>, y: f64);
        #[cxx_name = "setWidth"]
        fn set_width(self: Pin<&mut QQuickItem>, width: f64);
        #[cxx_name = "setVisible"]
        fn set_visible(self: Pin<&mut QQuickItem>, visible: bool);
        #[cxx_name = "setParentItem"]
        unsafe fn set_parent_item(self: Pin<&mut QQuickItem>, parent: *mut QQuickItem);

        #[cxx_name = "beginCreate"]
        unsafe fn begin_create(self: Pin<&mut QQmlComponent>, context: *mut QQmlContext) -> *mut QObject;
        #[cxx_name = "completeCreate"]
        fn complete_create(self: Pin<&mut QQmlComponent>);
        #[cxx_name = "setInitialProperties"]
        unsafe fn set_initial_properties(self: Pin<&mut QQmlComponent>, object: *mut QObject, properties: &QMap_QString_QVariant);
        #[cxx_name = "creationContext"]
        fn creation_context(self: &QQmlComponent) -> *mut QQmlContext;
        #[cxx_name = "qmlContext"]
        unsafe fn qml_context(object: *const QObject) -> *mut QQmlContext;

        #[namespace = "rust::cxxqtlib1"]
        #[cxx_name = "make_unique"]
        fn new_property_map() -> UniquePtr<QQmlPropertyMap>;
        fn insert(self: Pin<&mut QQmlPropertyMap>, key: &QString, value: &QVariant);

        #[cxx_name = "rowCount"]
        fn model_row_count(self: &QAbstractItemModel, parent: &QModelIndex) -> i32;
        #[cxx_name = "index"]
        fn model_index(self: &QAbstractItemModel, row: i32, column: i32, parent: &QModelIndex) -> QModelIndex;
        #[cxx_name = "data"]
        fn model_data(self: &QAbstractItemModel, index: &QModelIndex, role: i32) -> QVariant;
        #[cxx_name = "roleNames"]
        fn model_role_names(self: &QAbstractItemModel) -> QHash_i32_QByteArray;
    }

    #[namespace = "Qt"]
    unsafe extern "C++" {
        type ConnectionType = cxx_qt::ConnectionType;
    }
}

pub use ffi::{QObject, QQmlPropertyMap, QQuickItem};

/// Connects a signal of `sender` to a signal of `receiver` by their normalized
/// signatures (`"contentYChanged()"`), the SIGNAL()/SIGNAL() string form. The
/// receiver's own signal then reaches Rust through its cxx-qt `on_*` handler.
///
/// # Safety
/// Both pointers must be live QObjects.
pub unsafe fn forward_signal(sender: *const QObject, signal: &CStr, receiver: *const QObject, own_signal: &CStr) -> Option<QMetaObjectConnectionGuard> {
    // Qt's SIGNAL() macro prefixes the signature with the code 2.
    let with_code = |signature: &CStr| {
        let mut bytes = b"2".to_vec();
        bytes.extend_from_slice(signature.to_bytes_with_nul());
        bytes
    };
    let (from, to) = (with_code(signal), with_code(own_signal));
    let connection = unsafe {
        ffi::QObject::connect(sender, from.as_ptr().cast(), receiver, to.as_ptr().cast(), ConnectionType::DirectConnection)
    };
    let guard = QMetaObjectConnectionGuard::from(connection);
    Some(guard)
}

/// The item behind a created QObject, if it is one.
///
/// # Safety
/// `object` must be null or a live QObject.
pub unsafe fn as_item(object: *mut QObject) -> *mut QQuickItem {
    if object.is_null() || !unsafe { (*object).inherits(c"QQuickItem".as_ptr()) } {
        return std::ptr::null_mut();
    }
    // QQuickItem's first base is QObject, so the pointers coincide.
    object.cast()
}

pub fn item_object(item: *mut QQuickItem) -> *mut QObject {
    item.cast()
}

/// Whether the object's class (or a base) declares `name`.
///
/// # Safety
/// `object` must be a live QObject.
pub unsafe fn has_property(object: *const QObject, name: &str) -> bool {
    let Ok(name) = std::ffi::CString::new(name) else { return false };
    let meta = unsafe { (*object).meta_object() };
    !meta.is_null() && unsafe { (*meta).index_of_property(name.as_ptr()) } >= 0
}

pub fn new_property_map() -> UniquePtr<QQmlPropertyMap> {
    ffi::new_property_map()
}

pub fn pin<T>(pointer: *mut T) -> Option<Pin<&'static mut T>> {
    // SAFETY: callers pass pointers to live Qt objects they keep alive.
    (!pointer.is_null()).then(|| unsafe { Pin::new_unchecked(&mut *pointer) })
}

pub fn root_index() -> QModelIndex {
    QModelIndex::default()
}

pub fn qstring(text: &str) -> QString {
    QString::from(text)
}

pub fn variant_string(value: &QVariant) -> String {
    value.value::<QString>().map(|s| s.to_string()).unwrap_or_default()
}
