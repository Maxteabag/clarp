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
    Q_PROPERTY(quint64 revision READ revision NOTIFY changed)
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
                        emit changed();
                    });
        }
        m_timer.setInterval(33);
        connect(&m_timer, &QTimer::timeout, this, [this] {
            ++m_revision;
            emit changed();
        });
    }
    bool ticking() const { return m_timer.isActive(); }
    Q_INVOKABLE void observe(QObject* owner, bool visible) {
        if (!owner)
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
        } else
            m_observers.remove(owner);
        schedule();
    }
    quint64 revision() const { return m_revision; }
    bool reducedMotion() const { return m_reduced; }
    void setReducedMotion(bool value) {
        if (value == m_reduced)
            return;
        m_reduced = value;
        QSettings().setValue(QStringLiteral("appearance/reducedMotion"), value);
        schedule();
        ++m_revision;
        emit changed();
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
        emit changed();
    }
    Q_INVOKABLE bool working(const QString& session) const { return m_epochs.contains(session); }
    Q_INVOKABLE double phase(const QString& session) const {
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
  signals:
    void changed();
};
} // namespace clarp
