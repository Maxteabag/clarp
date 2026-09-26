#pragma once
#include <QElapsedTimer>
#include <QGuiApplication>
#include <QHash>
#include <QObject>
#include <QSet>
#include <QSettings>
#include <QTimer>

namespace clarp {
class AvatarMotionClock final : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool ticking READ ticking NOTIFY changed)
    // `revision` advances on every animation frame; bind only the pulse phase
    // to it. `workingRevision` advances when the working set, reduced motion
    // or foreground state changes, so "is this agent working" and settings
    // bindings are not re-evaluated on every frame.
    Q_PROPERTY(quint64 revision READ revision NOTIFY tick)
    Q_PROPERTY(quint64 workingRevision READ workingRevision NOTIFY changed)
    Q_PROPERTY(bool reducedMotion READ reducedMotion WRITE setReducedMotion NOTIFY changed)
  public:
    explicit AvatarMotionClock(QObject* parent = nullptr) : QObject(parent) {
        m_elapsed.start();
        m_reduced = QSettings().value(QStringLiteral("appearance/reducedMotion"), false).toBool();
        if (auto* app = qobject_cast<QGuiApplication*>(QCoreApplication::instance())) {
            m_foreground = app->applicationState() == Qt::ApplicationActive;
            connect(app, &QGuiApplication::applicationStateChanged, this,
                    [this](Qt::ApplicationState state) {
                        m_foreground = state == Qt::ApplicationActive;
                        schedule();
                        ++m_revision;
                        ++m_workingRevision;
                        emit changed();
                        emit tick();
                    });
        }
        // 20 fps is plenty for a 2.4 s breathing pulse and halves the redraws.
        m_timer.setInterval(50);
        connect(&m_timer, &QTimer::timeout, this, [this] {
            ++m_revision;
            emit tick();
        });
    }
    [[nodiscard]] bool ticking() const { return m_timer.isActive(); }
    Q_INVOKABLE void observe(QObject* owner, bool visible) {
        if (owner == nullptr)
            return;
        if (visible) {
            if (!m_observers.contains(owner)) {
                m_observers.insert(owner);
                if (!m_knownObservers.contains(owner)) {
                    m_knownObservers.insert(owner);
                    connect(owner, &QObject::destroyed, this, [this, owner] {
                        m_observers.remove(owner);
                        m_knownObservers.remove(owner);
                        schedule();
                    });
                }
            }
        } else {
            m_observers.remove(owner);
        }
        schedule();
    }
    [[nodiscard]] quint64 revision() const { return m_revision; }
    [[nodiscard]] quint64 workingRevision() const { return m_workingRevision; }
    [[nodiscard]] bool reducedMotion() const { return m_reduced; }
    void setReducedMotion(bool value) {
        if (value == m_reduced)
            return;
        m_reduced = value;
        QSettings().setValue(QStringLiteral("appearance/reducedMotion"), value);
        schedule();
        ++m_revision;
        ++m_workingRevision;
        emit changed();
        emit tick();
    }
    void reconcile(const QSet<QString>& active) {
        for (auto it = m_epochs.begin(); it != m_epochs.end();) {
            if (!active.contains(it.key()))
                it = m_epochs.erase(it);
            else
                ++it;
        }
        for (const auto& session : active)
            if (!m_epochs.contains(session))
                m_epochs.insert(session, m_elapsed.elapsed());
        schedule();
        ++m_revision;
        ++m_workingRevision;
        emit changed();
        emit tick();
    }
    Q_INVOKABLE [[nodiscard]] bool working(const QString& session) const { return m_epochs.contains(session); }
    Q_INVOKABLE [[nodiscard]] double phase(const QString& session) const {
        if (m_reduced || !m_foreground || !m_epochs.contains(session))
            return 0;
        return static_cast<double>((m_elapsed.elapsed() - m_epochs.value(session)) % 2400) / 2400.0;
    }

  private:
    void schedule() {
        if (m_foreground && !m_observers.isEmpty() && !m_reduced && !m_epochs.isEmpty())
            m_timer.start();
        else
            m_timer.stop();
    }
    bool m_foreground = true;
    QSet<QObject*> m_observers;
    QSet<QObject*> m_knownObservers;
    QElapsedTimer m_elapsed;
    QTimer m_timer;
    QHash<QString, qint64> m_epochs;
    bool m_reduced = false;
    quint64 m_revision = 0;
    quint64 m_workingRevision = 0;
  signals:
    void changed();
    void tick();
};
} // namespace clarp
