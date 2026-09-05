#pragma once

#include <QHash>
#include <QElapsedTimer>
#include <QJsonObject>
#include <QObject>
#include <QProcess>
#include <QQueue>
#include <QSet>
#include <QTemporaryDir>
#include <QTimer>
#include <QVariantMap>
#include <QtQmlIntegration/qqmlintegration.h>

namespace clarp {

// Presentation-only: never changes transcripts or dispatches an agent command.
class ToolNarrator : public QObject {
    Q_OBJECT
    QML_ANONYMOUS
    Q_PROPERTY(bool enabled READ enabled WRITE setEnabled NOTIFY enabledChanged)
    Q_PROPERTY(quint64 revision READ revision NOTIFY changed)
    Q_PROPERTY(QString status READ status NOTIFY statusChanged)
    Q_PROPERTY(QString diagnosticsPath READ diagnosticsPath CONSTANT)
    Q_PROPERTY(bool unavailable READ unavailable NOTIFY statusChanged)

  public:
    explicit ToolNarrator(QObject* parent = nullptr, QString program = QStringLiteral("codex"),
                          QStringList prefixArguments = {}, int timeoutMs = 45'000);
    ~ToolNarrator() override;
    [[nodiscard]] bool enabled() const;
    [[nodiscard]] quint64 revision() const;
    [[nodiscard]] QString status() const;
    [[nodiscard]] QString diagnosticsPath() const;
    [[nodiscard]] bool unavailable() const;
    void setEnabled(bool enabled);
    void reset();
    Q_INVOKABLE void request(const QVariantMap& activity, const QString& workingDirectory = {}, bool localFilesAllowed = false);
    Q_INVOKABLE [[nodiscard]] QString explanation(const QVariantMap& activity, const QString& workingDirectory = {}, bool localFilesAllowed = false) const;

  signals:
    void enabledChanged();
    void changed();
    void statusChanged();

  private:
    static QByteArray payload(const QVariantMap& activity, const QString& workingDirectory = {}, bool localFilesAllowed = false);
    static QString key(const QByteArray& payload);
    void startBatch();
    void finishBatch(int exitCode, QProcess::ExitStatus exitStatus);
    void fail(const QString& message);
    void stopProcess();
    void notify();
    void logEvent(const QString& event, const QJsonObject& fields = {});
    void readEvents();

    QString m_program;
    QStringList m_prefixArguments;
    int m_timeoutMs;
    QTemporaryDir m_directory;
    QProcess* m_process = nullptr;
    QTimer m_debounce;
    QTimer m_timeout;
    QTimer m_statusTick;
    QElapsedTimer m_clock;
    QElapsedTimer m_runClock;
    QHash<QString, qint64> m_enqueuedAt;
    QString m_diagnosticsPath;
    QByteArray m_events;
    quint64 m_batchNumber = 0;
    qint64 m_queueWaitMs = 0;
    QQueue<QByteArray> m_queue;
    QSet<QString> m_requested;
    QSet<QString> m_batchKeys;
    QHash<QString, QString> m_cache;
    QQueue<QString> m_cacheOrder;
    QString m_error;
    quint64 m_revision = 0;
    bool m_enabled = false;
};

} // namespace clarp
