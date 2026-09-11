#include "media/AudioCoordinator.h"

#include <QCryptographicHash>
#include <QDBusMessage>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QSaveFile>
#include <QStandardPaths>
#include <QUuid>
#include <algorithm>

namespace clarp {
namespace {
constexpr auto Path = "/com/maxteabag/Clarp/Audio";
constexpr auto Interface = "com.maxteabag.Clarp.Audio";
QString digest(const QByteArray& bytes) {
    return QString::fromLatin1(QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex());
}
}

AudioCoordinator::AudioCoordinator(QObject* parent)
    : QObject(parent),
      m_connectionName(QStringLiteral("clarp-audio-") + QUuid::createUuid().toString(QUuid::WithoutBraces)),
      m_bus(QDBusConnection::connectToBus(QDBusConnection::SessionBus, m_connectionName)) {
    m_tick.setInterval(500);
    connect(&m_tick, &QTimer::timeout, this, &AudioCoordinator::tick);
}
AudioCoordinator::~AudioCoordinator() {
    stop();
    QDBusConnection::disconnectFromBus(m_connectionName);
}
void AudioCoordinator::configure(const QString& scope, bool initiallyMuted) {
    const QString service = QStringLiteral("com.maxteabag.Clarp.Audio.h") + digest(scope.toUtf8());
    if (service == m_service) return;
    stop();
    m_service = service;
    m_initiallyMuted = initiallyMuted;
    const QString directory = QDir(QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation))
        .filePath(QStringLiteral("audio-coordination"));
    if (!QDir().mkpath(directory)) {
        emit error(QStringLiteral("Cannot create the shared audio journal directory"));
        return;
    }
    QFile::setPermissions(directory, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner);
    m_journalPath = QDir(directory).filePath(digest(scope.toUtf8()) + QStringLiteral(".json"));
    if (!m_bus.isConnected()) {
        emit error(QStringLiteral("Shared audio requires a desktop session bus"));
        return; // Fail closed: never create an uncoordinated second player.
    }
    m_tick.start();
    tick();
}
void AudioCoordinator::stop() {
    ++m_generation;
    m_tick.stop();
    if (m_owner) {
        m_owner = false;
        m_bus.unregisterObject(QString::fromLatin1(Path));
        m_bus.unregisterService(m_service);
        emit ownershipChanged();
    }
    m_service.clear();
    m_journalPath.clear();
    m_journal = {};
    m_outbox = {};
    m_outboxOrder.clear();
    m_sending = false;
    m_polling = false;
    m_playing = m_paused = m_available = false;
}
QString AudioCoordinator::clipKey(const QJsonObject& event) {
    const qint64 id = event.value(QStringLiteral("clip_id")).toInteger();
    if (id > 0) return QString::number(id);
    return digest(QJsonDocument(event).toJson(QJsonDocument::Compact));
}
bool AudioCoordinator::save(const QJsonObject& journal) {
    QSaveFile file(m_journalPath);
    if (!file.open(QIODevice::WriteOnly)) {
        emit error(QStringLiteral("Cannot persist shared audio state"));
        return false;
    }
    file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
    const QByteArray data = QJsonDocument(journal).toJson(QJsonDocument::Compact);
    if (file.write(data) != data.size() || !file.commit()) {
        emit error(QStringLiteral("Cannot persist shared audio state"));
        return false;
    }
    m_journal = journal;
    return true;
}
void AudioCoordinator::rememberFinished(QJsonObject& journal, const QString& key) const {
    auto pending = journal.value(QStringLiteral("pending")).toObject();
    pending.remove(key);
    journal.insert(QStringLiteral("pending"), pending);
    auto completed = journal.value(QStringLiteral("completed")).toArray();
    if (!completed.contains(key)) completed.append(key);
    while (completed.size() > 4096) completed.removeFirst();
    journal.insert(QStringLiteral("completed"), completed);
}
bool AudioCoordinator::takeOwnership() {
    if (!m_bus.registerService(m_service)) return false;
    if (!m_bus.registerObject(QString::fromLatin1(Path), this, QDBusConnection::ExportScriptableSlots)) {
        m_bus.unregisterService(m_service);
        return false;
    }
    QJsonObject journal{{QStringLiteral("muted"), m_initiallyMuted}};
    QFile file(m_journalPath);
    if (file.exists()) {
        if (!file.open(QIODevice::ReadOnly)) {
            m_bus.unregisterObject(QString::fromLatin1(Path));
            m_bus.unregisterService(m_service);
            emit error(QStringLiteral("Cannot read shared audio state"));
            return false;
        }
        const auto document = QJsonDocument::fromJson(file.readAll());
        if (!document.isObject()) {
            m_bus.unregisterObject(QString::fromLatin1(Path));
            m_bus.unregisterService(m_service);
            emit error(QStringLiteral("Shared audio state is invalid; playback remains stopped"));
            return false;
        }
        journal = document.object();
    }
    const auto pending = journal.value(QStringLiteral("pending")).toObject();
    for (auto it = pending.begin(); it != pending.end(); ++it) {
        if (it.value().toObject().value(QStringLiteral("started")).toBool()) rememberFinished(journal, it.key());
    }
    if (!save(journal)) {
        m_bus.unregisterObject(QString::fromLatin1(Path));
        m_bus.unregisterService(m_service);
        return false;
    }
    m_owner = true;
    emit ownershipChanged();
    announce();
    const auto queued = m_journal.value(QStringLiteral("pending")).toObject();
    auto keys = queued.keys();
    std::sort(keys.begin(), keys.end(), [&queued](const QString& left, const QString& right) {
        return queued.value(left).toObject().value(QStringLiteral("sequence")).toInteger()
            < queued.value(right).toObject().value(QStringLiteral("sequence")).toInteger();
    });
    for (const auto& key : keys) emit clipReady(queued.value(key).toObject().value(QStringLiteral("event")).toObject());
    return true;
}
void AudioCoordinator::submit(const QJsonObject& event) {
    if (!configured()) return;
    if (!m_outbox.contains(clipKey(event))) m_outboxOrder.append(clipKey(event));
    m_outbox.insert(clipKey(event), event);
    tick();
}
bool AudioCoordinator::offer(const QByteArray& events) {
    if (!m_owner) return false;
    const auto document = QJsonDocument::fromJson(events);
    if (!document.isArray()) return false;
    if (m_journal.value(QStringLiteral("muted")).toBool()) return true;
    auto journal = m_journal;
    auto pending = journal.value(QStringLiteral("pending")).toObject();
    const auto completed = journal.value(QStringLiteral("completed")).toArray();
    QJsonArray added;
    qint64 sequence = journal.value(QStringLiteral("sequence")).toInteger();
    for (const auto& value : document.array()) {
        if (!value.isObject()) continue;
        const auto event = value.toObject();
        const QString key = clipKey(event);
        if (pending.contains(key) || completed.contains(key)) continue;
        pending.insert(key, QJsonObject{{QStringLiteral("event"), event}, {QStringLiteral("started"), false}, {QStringLiteral("sequence"), ++sequence}});
        added.append(event);
    }
    if (added.isEmpty()) return true;
    journal.insert(QStringLiteral("sequence"), sequence);
    journal.insert(QStringLiteral("pending"), pending);
    if (!save(journal)) return false;
    for (const auto& event : added) emit clipReady(event.toObject());
    return true;
}
// Every watcher is QObject-parented to this coordinator and deleteLater() is
// called before every completion branch. Like CredentialStore, the Qt ownership
// path is not understood by the static analyzer; sanitizers exercise shutdown.
// NOLINTBEGIN(clang-analyzer-cplusplus.NewDeleteLeaks)
void AudioCoordinator::tick() {
    if (!m_bus.isConnected() && m_owner) {
        m_owner = false;
        emit ownershipChanged();
        emit error(QStringLiteral("Shared audio lost its session bus; playback stopped"));
    }
    if (!configured() || !m_bus.isConnected() || m_journalPath.isEmpty()) return;
    if (!m_owner) takeOwnership();
    if (!m_outbox.isEmpty() && !m_sending) {
        QJsonArray events;
        const QStringList keys = m_outboxOrder;
        for (const auto& key : keys) events.append(m_outbox.value(key));
        const QByteArray bytes = QJsonDocument(events).toJson(QJsonDocument::Compact);
        if (m_owner) {
            if (offer(bytes)) for (const auto& key : keys) { m_outbox.remove(key); m_outboxOrder.removeAll(key); }
        } else {
            m_sending = true;
            auto message = QDBusMessage::createMethodCall(m_service, QString::fromLatin1(Path), QString::fromLatin1(Interface), QStringLiteral("offer"));
            message << bytes;
            auto* watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(message, 1500), this);
            const quint64 generation = m_generation;
            connect(watcher, &QDBusPendingCallWatcher::finished, this, [this, watcher, keys, generation] {
                const QDBusPendingReply<bool> reply = *watcher;
                watcher->deleteLater();
                if (generation != m_generation) return;
                m_sending = false;
                if (!reply.isError() && reply.value()) for (const auto& key : keys) { m_outbox.remove(key); m_outboxOrder.removeAll(key); }
            });
        }
    }
    if (m_owner || m_polling) return;
    m_polling = true;
    const auto message = QDBusMessage::createMethodCall(m_service, QString::fromLatin1(Path), QString::fromLatin1(Interface), QStringLiteral("snapshot"));
    auto* watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(message, 1500), this);
    const quint64 generation = m_generation;
    connect(watcher, &QDBusPendingCallWatcher::finished, this, [this, watcher, generation] {
        const QDBusPendingReply<QByteArray> reply = *watcher;
        watcher->deleteLater();
        if (generation != m_generation) return;
        m_polling = false;
        if (!reply.isError() && !m_owner) applySnapshot(reply.value());
    });
}
void AudioCoordinator::command(const QString& action, bool muted) {
    if (m_owner) {
        control(action, muted);
        return;
    }
    if (!configured()) return;
    auto message = QDBusMessage::createMethodCall(m_service, QString::fromLatin1(Path), QString::fromLatin1(Interface), QStringLiteral("control"));
    message << action << muted;
    auto* watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(message, 1500), this);
    const quint64 generation = m_generation;
    connect(watcher, &QDBusPendingCallWatcher::finished, this, [this, watcher, generation] {
        const QDBusPendingReply<> reply = *watcher;
        watcher->deleteLater();
        if (generation != m_generation) return;
        if (reply.isError()) emit error(QStringLiteral("The audio owner changed; please retry the playback control"));
        tick();
    });
}
// NOLINTEND(clang-analyzer-cplusplus.NewDeleteLeaks)
void AudioCoordinator::control(const QString& action, bool muted) {
    if (!m_owner) return;
    if (action == QStringLiteral("mute")) {
        auto journal = m_journal;
        journal.insert(QStringLiteral("muted"), muted);
        if (!save(journal)) return;
        announce();
    } else if (action == QStringLiteral("pause") || action == QStringLiteral("resume") || action == QStringLiteral("toggle") || action == QStringLiteral("stop")) {
        emit commandReceived(action);
        announce();
    }
}
bool AudioCoordinator::begin(const QJsonObject& event) {
    if (!m_owner) return false;
    auto journal = m_journal;
    auto pending = journal.value(QStringLiteral("pending")).toObject();
    const QString key = clipKey(event);
    if (!pending.contains(key)) return false;
    auto record = pending.value(key).toObject();
    if (record.value(QStringLiteral("started")).toBool()) return false;
    record.insert(QStringLiteral("started"), true);
    pending.insert(key, record);
    journal.insert(QStringLiteral("pending"), pending);
    return save(journal);
}
void AudioCoordinator::finish(const QJsonObject& event) {
    if (!m_owner) return;
    auto journal = m_journal;
    rememberFinished(journal, clipKey(event));
    save(journal);
}
QByteArray AudioCoordinator::snapshot() const {
    return QJsonDocument(QJsonObject{{QStringLiteral("muted"), m_journal.value(QStringLiteral("muted")).toBool()},
        {QStringLiteral("playing"), m_playing}, {QStringLiteral("paused"), m_paused}, {QStringLiteral("available"), m_available}}).toJson(QJsonDocument::Compact);
}
void AudioCoordinator::applySnapshot(const QByteArray& bytes) {
    const auto document = QJsonDocument::fromJson(bytes);
    if (!document.isObject()) return;
    const auto state = document.object();
    emit stateReceived(state.value(QStringLiteral("muted")).toBool(), state.value(QStringLiteral("playing")).toBool(),
        state.value(QStringLiteral("paused")).toBool(), state.value(QStringLiteral("available")).toBool());
}
void AudioCoordinator::announce() { applySnapshot(snapshot()); }
void AudioCoordinator::publish(bool playing, bool paused, bool available) {
    if (!m_owner) return;
    m_playing = playing;
    m_paused = paused;
    m_available = available;
}
} // namespace clarp
