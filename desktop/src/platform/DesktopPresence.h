#pragma once
#include <QObject>
#include <QElapsedTimer>
#include <QTimer>
#include <QVariantMap>
class QQuickWindow;
namespace clarp {
class DesktopPresence final : public QObject {
    Q_OBJECT
  public:
    explicit DesktopPresence(QQuickWindow* window, QObject* parent = nullptr, bool monitorSystemSession = true);
    [[nodiscard]] static bool eligible(bool enabled, bool foreground, bool unlocked, qint64 inputAgeMs);
    [[nodiscard]] bool active() const { return m_active; }
    [[nodiscard]] bool sessionAvailable() const { return m_sessionAvailable; }
    [[nodiscard]] bool sessionUnlocked() const { return m_unlocked; }
    void setEnabled(bool enabled);
    void setConnected(bool connected);
    void setSessionState(bool available, bool unlocked);
    void noteInteraction();
  signals:
    void applicationActivity(const QString& instance, quint64 sequence, bool foreground, qint64 inputAgeMs);
    void presenceReport(const QString& instance, quint64 sequence, bool active);
  protected:
    bool eventFilter(QObject* receiver, QEvent* event) override;
  private:
    // Per-function Q_SLOT instead of a second `private slots:` label: logind
    // connects sessionChanged/prepareForSleep through the SLOT() string macro,
    // so they must stay registered slots, and one access section keeps
    // readability-redundant-access-specifiers satisfied.
    Q_SLOT void refresh();
    Q_SLOT void querySession();
    Q_SLOT void sessionChanged(const QString& interface, const QVariantMap& changed, const QStringList& invalidated);
    Q_SLOT void prepareForSleep(bool sleeping);
    void discoverSession();
    void watchSession(const QString& path);
    QQuickWindow* m_window;
    QElapsedTimer m_clock;
    QTimer m_tick;
    QTimer m_sessionPoll;
    QString m_instance;
    QString m_sessionPath;
    quint64 m_sequence = 0;
    quint64 m_activitySequence = 0;
    qint64 m_lastActivityReport = -10'000;
    bool m_reportedForeground = false;
    quint64 m_queryGeneration = 0;
    bool m_monitorSystem = true;
    qint64 m_lastInput = -1;
    qint64 m_lastReport = -10'000;
    bool m_enabled = true;
    bool m_connected = false;
    bool m_unlocked = false;
    bool m_sessionAvailable = false;
    bool m_sleeping = false;
    bool m_active = false;
    bool m_reported = false;
};
}
