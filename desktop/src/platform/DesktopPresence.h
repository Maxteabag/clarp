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
    static bool eligible(bool enabled, bool foreground, bool unlocked, qint64 inputAgeMs);
    bool active() const { return m_active; }
    bool sessionAvailable() const { return m_sessionAvailable; }
    bool sessionUnlocked() const { return m_unlocked; }
    void setEnabled(bool enabled);
    void setConnected(bool connected);
    void setSessionState(bool available, bool unlocked);
    void noteInteraction();
  signals:
    void presenceReport(const QString& instance, quint64 sequence, bool active);
  protected:
    bool eventFilter(QObject* receiver, QEvent* event) override;
  private slots:
    void refresh();
    void querySession();
    void sessionChanged(const QString& interface, const QVariantMap& changed, const QStringList& invalidated);
    void prepareForSleep(bool sleeping);
  private:
    void discoverSession();
    void watchSession(const QString& path);
    QQuickWindow* m_window;
    QElapsedTimer m_clock;
    QTimer m_tick;
    QTimer m_sessionPoll;
    QString m_instance;
    QString m_sessionPath;
    quint64 m_sequence = 0;
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
