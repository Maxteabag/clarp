#include "app/PreviewVersions.h"
#include "app/PreviewRelaunch.h"
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QSettings>

namespace clarp {
PreviewVersions::PreviewVersions(QObject* parent) : QObject(parent) {
    m_helper = QDir::homePath() + QStringLiteral("/.local/lib/clarp-desktop-preview/auto_update.py");
    m_fixture = qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PATH")
        && qEnvironmentVariable("CLARP_SCREENSHOT_SCENARIO") == QStringLiteral("preview-versions");
    m_enabled = m_fixture || ((qEnvironmentVariable("CLARP_INSTANCE_NAME") == QStringLiteral("com.maxteabag.Clarp.WorktreePreview")
        || QCoreApplication::arguments().contains(QStringLiteral("--preview-versions")))
        && QFileInfo::exists(m_helper) && !qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PATH"));
    if (!m_enabled) return;
    QFile executable(QStringLiteral("/proc/self/exe"));
    if (executable.open(QIODevice::ReadOnly)) {
        QCryptographicHash hash(QCryptographicHash::Sha256);
        if (hash.addData(&executable)) m_runningHash = QString::fromLatin1(hash.result().toHex());
    }
    if (QCoreApplication::arguments().contains(QStringLiteral("--preview-versions"))) m_runningHash.clear();
    if (m_fixture) {
        m_runningHash = QStringLiteral("old");
        m_catalog = {{QStringLiteral("current"), QStringLiteral("new")}, {QStringLiteral("latest"), QStringLiteral("new")},
            {QStringLiteral("pinned"), QString{}}, {QStringLiteral("versions"), QVariantList{
                QVariantMap{{QStringLiteral("hash"), QStringLiteral("new")}, {QStringLiteral("label"), QStringLiteral("v1.1.3 · abcd1234")}},
                QVariantMap{{QStringLiteral("hash"), QStringLiteral("old")}, {QStringLiteral("label"), QStringLiteral("v1.1.2 · 1234abcd")}}}}};
        return;
    }
    connect(&m_process, &QProcess::stateChanged, this, [this] { emit changed(); });
    connect(&m_process, &QProcess::errorOccurred, this, [this] {
        m_error = QStringLiteral("Cannot run the preview updater."); emit changed();
    });
    connect(&m_process, &QProcess::finished, this, [this](int code, QProcess::ExitStatus status) {
        const auto reply = QJsonDocument::fromJson(m_process.readAllStandardOutput()).toVariant().toMap();
        if (code != 0 || status != QProcess::NormalExit || reply.isEmpty()) {
            m_error = reply.value(QStringLiteral("error"), QStringLiteral("Update action failed; the open window was kept.")).toString();
        } else if (!m_selecting) {
            m_catalog = reply; m_error.clear();
        } else if (reply.value(QStringLiteral("ok")).toBool()) {
            if (QCoreApplication::arguments().contains(QStringLiteral("--preview-versions"))) {
                m_notice = QStringLiteral("Version selected. Close the preview window and reopen with Super+Alt+A.");
                m_selecting = false; emit changed();
                refresh();
                return;
            }
            if (!m_restartAllowed || m_selectedSession != m_requestedSession || m_selectedHost != m_requestedHost) {
                m_error = QStringLiteral("Version selected. Finish local activity and update again to reopen this conversation.");
                m_selecting = false; emit changed(); return;
            }
            QSettings settings;
            settings.sync();
            if (settings.status() != QSettings::NoError) {
                m_error = QStringLiteral("Version selected, but settings could not be saved. Window kept open.");
            } else {
                QProcess relaunch;
                relaunch.setProgram(QStringLiteral("/usr/bin/python3"));
                relaunch.setArguments(previewRelaunchArguments(m_helper, QCoreApplication::applicationPid()));
                relaunch.setProcessEnvironment(previewRelaunchEnvironment(QProcessEnvironment::systemEnvironment(),
                    m_requestedHost, m_requestedSession));
                if (relaunch.startDetached()) QCoreApplication::quit();
                else m_error = QStringLiteral("Version selected. Close and reopen the preview manually.");
            }
        }
        m_selecting = false; emit changed();
    });
    m_timer.setInterval(15000);
    connect(&m_timer, &QTimer::timeout, this, &PreviewVersions::refresh);
    if (m_enabled) { m_timer.start(); QTimer::singleShot(0, this, &PreviewVersions::refresh); }
}
QVariantMap PreviewVersions::restartContext() const {
    return {{QStringLiteral("host"), m_requestedHost}, {QStringLiteral("session"), m_requestedSession},
            {QStringLiteral("arguments"), previewRelaunchArguments(m_helper, QCoreApplication::applicationPid())}};
}
void PreviewVersions::refresh() {
    if (!m_enabled || m_fixture || busy()) return;
    m_process.start(QStringLiteral("/usr/bin/python3"), {m_helper, QStringLiteral("--catalog")});
}
void PreviewVersions::selectVersion(const QString& hash) {
    if (!m_enabled || busy() || m_catalog.isEmpty()) return;
    if (!m_restartAllowed) {
        m_error = QStringLiteral("Finish sending, uploading, recording or playback before updating this window.");
        emit changed(); return;
    }
    m_requestedHost = m_selectedHost;
    m_requestedSession = m_selectedSession;
    if (m_fixture) { m_error.clear(); m_notice = QStringLiteral("Fixture captured restart request"); emit changed(); return; }
    m_selecting = true; m_error.clear(); m_notice.clear();
    m_process.start(QStringLiteral("/usr/bin/python3"), {m_helper, QStringLiteral("--select"), hash,
        QStringLiteral("--expected-current"), m_catalog.value(QStringLiteral("current")).toString()});
}
}
