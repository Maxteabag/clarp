//! Rust replacement for `desktop/src/app/AppController`. Starts as the scaffold
//! bridge and grows as each C++ responsibility is ported.

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(QString, server_url, cxx_name = "serverUrl")]
        #[qproperty(bool, connected)]
        type AppController = super::AppControllerRust;
    }
}

#[derive(Default)]
pub struct AppControllerRust {
    server_url: cxx_qt_lib::QString,
    connected: bool,
}
