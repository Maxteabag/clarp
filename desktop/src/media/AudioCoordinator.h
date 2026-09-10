#pragma once

#include <QDBusConnection>
#include <QJsonObject>
#include <QObject>
#include <QTimer>

namespace clarp {

// A session-bus name elects one player per user/Host/account. Only that player
// writes the journal; pending clips survive ownership changes, while clips
// already started are never automatically replayed after an owner crash.
class AudioCoordinator final : public QObject {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "com.maxteabag.Clarp.Audio")
  public:
    explicit AudioCoordinator(QObject* parent = nullptr);
    ~AudioCoordinator() override;
    void configure(const QString& scope, bool initiallyMuted);
    void stop();
    [[nodiscard]] bool configured() const { return !m_service.isEmpty(); }
    [[nodiscard]] bool owner() const { return m_owner; }
    void submit(const QJsonObject& event);
    void command(const QString& action, bool muted = false);
    bool begin(const QJsonObject& event);
    void finish(const QJsonObject& event);
    void publish(bool playing, bool paused, bool available);
    static QString clipKey(const QJsonObject& event);

  signals:
    void clipReady(const QJsonObject& event);
    void commandReceived(const QString& action);
    void stateReceived(bool muted, bool playing, bool paused, bool available);
    void ownershipChanged();
    void error(const QString& message);

  private:
    void tick();
    bool save(const QJsonObject& journal);
    bool takeOwnership();
    void applySnapshot(const QByteArray& bytes);
    void announce();
    void rememberFinished(QJsonObject& journal, const QString& key) const;

    QString m_connectionName;
    QDBusConnection m_bus;
    QString m_service;
    QString m_journalPath;
    QJsonObject m_journal;
    QJsonObject m_outbox;
    QStringList m_outboxOrder;
    QTimer m_tick;
    quint64 m_generation = 0;
    bool m_owner = false;
    bool m_sending = false;
    bool m_polling = false;
    bool m_initiallyMuted = false;
    bool m_playing = false;
    bool m_paused = false;
    bool m_available = false;

  public slots:
    Q_SCRIPTABLE bool offer(const QByteArray& events);
    Q_SCRIPTABLE void control(const QString& action, bool muted);
    Q_SCRIPTABLE [[nodiscard]] QByteArray snapshot() const;
};

} // namespace clarp
