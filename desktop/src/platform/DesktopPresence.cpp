#include "platform/DesktopPresence.h"
#include <QCoreApplication>
#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusObjectPath>
#include <QDBusPendingCallWatcher>
#include <QDBusVariant>
#include <QEvent>
#include <QQuickItem>
#include <QQuickWindow>
#include <QUuid>
#ifdef Q_OS_UNIX
#include <unistd.h>
#endif

namespace clarp {
namespace {
QDBusMessage loginCall(const QString& path, const QString& interface, const QString& method) {
    return QDBusMessage::createMethodCall(QStringLiteral("org.freedesktop.login1"), path, interface, method);
}
}
bool DesktopPresence::eligible(bool enabled, bool foreground, bool unlocked, qint64 inputAgeMs) {
    return enabled && foreground && unlocked && inputAgeMs >= 0 && inputAgeMs < 120'000;
}
DesktopPresence::DesktopPresence(QQuickWindow* window, QObject* parent, bool monitorSystemSession)
    : QObject(parent), m_window(window), m_instance(QUuid::createUuid().toString(QUuid::WithoutBraces)) {
    m_monitorSystem = monitorSystemSession;
    m_clock.start();
    qApp->installEventFilter(this);
    connect(window, &QWindow::activeChanged, this, [this] {
        if (m_window->isActive()) noteInteraction();
        else refresh();
    });
    connect(window, &QWindow::visibilityChanged, this, &DesktopPresence::refresh);
    connect(qApp, &QCoreApplication::aboutToQuit, this, [this] { setEnabled(false); });
    m_tick.setInterval(1'000);
    connect(&m_tick, &QTimer::timeout, this, &DesktopPresence::refresh);
    m_tick.start();
    if (window->isActive()) noteInteraction();
    if (monitorSystemSession) {
        auto bus = QDBusConnection::systemBus();
        bus.connect(QStringLiteral("org.freedesktop.login1"), QStringLiteral("/org/freedesktop/login1"),
            QStringLiteral("org.freedesktop.login1.Manager"), QStringLiteral("PrepareForSleep"),
            this, SLOT(prepareForSleep(bool)));
        m_sessionPoll.setInterval(10'000);
        connect(&m_sessionPoll, &QTimer::timeout, this, &DesktopPresence::querySession);
        m_sessionPoll.start();
        discoverSession();
    }
}
void DesktopPresence::setEnabled(bool enabled) { m_enabled = enabled; refresh(); }
void DesktopPresence::setConnected(bool connected) { m_connected = connected; refresh(); }
void DesktopPresence::setSessionState(bool available, bool unlocked) {
    m_sessionAvailable = available; m_unlocked = unlocked; refresh();
}
void DesktopPresence::noteInteraction() { m_lastInput = m_clock.elapsed(); refresh(); }
void DesktopPresence::prepareForSleep(bool sleeping) {
    m_sleeping = sleeping;
    if (!sleeping) { m_lastInput = -1; querySession(); }
    refresh();
}
bool DesktopPresence::eventFilter(QObject* receiver, QEvent* event) {
    switch (event->type()) {
    case QEvent::KeyPress: case QEvent::MouseButtonPress: case QEvent::MouseMove:
    case QEvent::Wheel: case QEvent::TouchBegin: case QEvent::TouchUpdate: {
        const auto* item = qobject_cast<QQuickItem*>(receiver);
        if (receiver == m_window || (item && item->window() == m_window)) noteInteraction();
        break;
    }
    default: break;
    }
    return false;
}
void DesktopPresence::refresh() {
    const qint64 now = m_clock.elapsed();
    const bool foreground = m_window && m_window->isActive() && m_window->isVisible()
        && m_window->visibility() != QWindow::Minimized;
    m_active = eligible(m_enabled && m_connected, foreground,
        m_sessionAvailable && m_unlocked && !m_sleeping, m_lastInput < 0 ? -1 : now - m_lastInput);
    // Inactive transitions release immediately; only active use renews leases.
    if ((m_sequence == 0 && m_active) || m_active != m_reported || (m_active && now - m_lastReport >= 10'000)) {
        m_lastReport = now; m_reported = m_active;
        emit presenceReport(m_instance, ++m_sequence, m_active);
    }
}
void DesktopPresence::discoverSession() {
#ifdef Q_OS_UNIX
    const QString explicitSession = qEnvironmentVariable("XDG_SESSION_ID");
    auto call = loginCall(QStringLiteral("/org/freedesktop/login1"),
        QStringLiteral("org.freedesktop.login1.Manager"), explicitSession.isEmpty() ? QStringLiteral("GetUser") : QStringLiteral("GetSession"));
    call.setArguments(explicitSession.isEmpty() ? QList<QVariant>{static_cast<uint>(getuid())} : QList<QVariant>{explicitSession});
    auto* watcher = new QDBusPendingCallWatcher(QDBusConnection::systemBus().asyncCall(call, 1'000), this);
    connect(watcher, &QDBusPendingCallWatcher::finished, this, [this, watcher, explicitSession] {
        const auto reply = watcher->reply(); watcher->deleteLater();
        if (reply.type() == QDBusMessage::ErrorMessage || reply.arguments().isEmpty()) { setSessionState(false, false); return; }
        const QString path = qvariant_cast<QDBusObjectPath>(reply.arguments().first()).path();
        if (!explicitSession.isEmpty()) { watchSession(path); return; }
        auto display = loginCall(path, QStringLiteral("org.freedesktop.DBus.Properties"), QStringLiteral("Get"));
        display.setArguments({QStringLiteral("org.freedesktop.login1.User"), QStringLiteral("Display")});
        auto* lookup = new QDBusPendingCallWatcher(QDBusConnection::systemBus().asyncCall(display, 1'000), this);
        connect(lookup, &QDBusPendingCallWatcher::finished, this, [this, lookup] {
            const auto result = lookup->reply(); lookup->deleteLater();
            if (result.type() == QDBusMessage::ErrorMessage || result.arguments().isEmpty()) { setSessionState(false, false); return; }
            const auto wrapped = qvariant_cast<QDBusVariant>(result.arguments().first()).variant();
            const auto argument = qvariant_cast<QDBusArgument>(wrapped);
            QString id; QDBusObjectPath sessionPath;
            argument.beginStructure(); argument >> id >> sessionPath; argument.endStructure();
            watchSession(sessionPath.path());
        });
    });
#else
    // Unknown lock/session state must never suppress phone alerts.
    setSessionState(false, false);
#endif
}
void DesktopPresence::watchSession(const QString& path) {
    if (path.isEmpty() || path == QStringLiteral("/")) { setSessionState(false, false); return; }
    if (m_sessionPath != path) {
        if (!m_sessionPath.isEmpty()) QDBusConnection::systemBus().disconnect(QStringLiteral("org.freedesktop.login1"),
            m_sessionPath, QStringLiteral("org.freedesktop.DBus.Properties"), QStringLiteral("PropertiesChanged"),
            this, SLOT(sessionChanged(QString,QVariantMap,QStringList)));
        m_sessionPath = path;
        QDBusConnection::systemBus().connect(QStringLiteral("org.freedesktop.login1"), path,
            QStringLiteral("org.freedesktop.DBus.Properties"), QStringLiteral("PropertiesChanged"),
            this, SLOT(sessionChanged(QString,QVariantMap,QStringList)));
    }
    querySession();
}
void DesktopPresence::sessionChanged(const QString& interface, const QVariantMap& changed, const QStringList& invalidated) {
    if (interface != QStringLiteral("org.freedesktop.login1.Session")) return;
    if (changed.value(QStringLiteral("LockedHint")).toBool() ||
        (changed.contains(QStringLiteral("Active")) && !changed.value(QStringLiteral("Active")).toBool()) || !invalidated.isEmpty())
        setSessionState(false, false);
    querySession();
}
void DesktopPresence::querySession() {
    if (!m_monitorSystem) return;
    const quint64 generation = ++m_queryGeneration;
    if (m_sessionPath.isEmpty()) { discoverSession(); return; }
    auto call = loginCall(m_sessionPath, QStringLiteral("org.freedesktop.DBus.Properties"), QStringLiteral("GetAll"));
    call.setArguments({QStringLiteral("org.freedesktop.login1.Session")});
    auto* watcher = new QDBusPendingCallWatcher(QDBusConnection::systemBus().asyncCall(call, 1'000), this);
    connect(watcher, &QDBusPendingCallWatcher::finished, this, [this, watcher, generation] {
        const auto reply = watcher->reply(); watcher->deleteLater();
        if (generation != m_queryGeneration) return;
        if (reply.type() == QDBusMessage::ErrorMessage || reply.arguments().isEmpty()) { m_sessionPath.clear(); setSessionState(false, false); return; }
        const auto values = qdbus_cast<QVariantMap>(reply.arguments().first());
        setSessionState(values.contains(QStringLiteral("Active")) && values.contains(QStringLiteral("LockedHint")),
            values.value(QStringLiteral("Active")).toBool() && !values.value(QStringLiteral("LockedHint")).toBool());
    });
}
}
