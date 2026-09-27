#pragma once
#include <algorithm>
#include <QElapsedTimer>
#include <QEvent>
#include <QGuiApplication>
#include <QHash>
#include <QObject>
#include <QSet>
#include <QSettings>
#include <QTimer>
#include <QWindow>

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
    Q_PROPERTY(quint64 processRevision READ processRevision NOTIFY processTick)
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
        // The process glyph is a discrete "still running" cue. It should share
        // one low-frequency clock no matter how many helpers are visible.
        m_processTimer.setInterval(350);
        connect(&m_processTimer, &QTimer::timeout, this, [this] {
            ++m_processRevision;
            emit processTick();
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
    Q_INVOKABLE void observeProcess(QObject* owner, bool visible) {
        if (owner == nullptr)
            return;
        if (visible) {
            if (!m_processObservers.contains(owner)) {
                m_processObservers.insert(owner);
                if (!m_knownProcessObservers.contains(owner)) {
                    m_knownProcessObservers.insert(owner);
                    connect(owner, &QObject::destroyed, this, [this, owner] {
                        m_processObservers.remove(owner);
                        m_knownProcessObservers.remove(owner);
                        schedule();
                    });
                }
            }
        } else {
            m_processObservers.remove(owner);
        }
        schedule();
    }
    [[nodiscard]] quint64 revision() const { return m_revision; }
    [[nodiscard]] quint64 processRevision() const { return m_processRevision; }
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
        if (m_reduced || !canAnimateWindow() || !m_epochs.contains(session))
            return 0;
        return static_cast<double>((m_elapsed.elapsed() - m_epochs.value(session)) % 2400) / 2400.0;
    }
    Q_INVOKABLE [[nodiscard]] int processHop() const {
        if (m_reduced || !canAnimateProcessWindow())
            return 0;
        switch (m_processRevision % 4) {
        case 1: return -1;
        case 2: return -2;
        case 3: return -1;
        default: return 0;
        }
    }
    void watchWindow(QWindow* window) {
        if (window == nullptr || m_windows.contains(window))
            return;
        m_windows.insert(window);
        window->installEventFilter(this);
        connect(window, &QWindow::visibleChanged, this, [this] { noteWindowStateChanged(); });
        connect(window, &QWindow::visibilityChanged, this, [this] { noteWindowStateChanged(); });
        connect(window, &QObject::destroyed, this, [this, window] {
            m_windows.remove(window);
            noteWindowStateChanged();
        });
        noteWindowStateChanged();
    }
    bool eventFilter(QObject* watched, QEvent* event) override {
        if (m_windows.contains(qobject_cast<QWindow*>(watched)) && (event->type() == QEvent::Expose
                || event->type() == QEvent::Hide || event->type() == QEvent::Show
                || event->type() == QEvent::WindowStateChange)) {
            QTimer::singleShot(0, this, [this] { noteWindowStateChanged(); });
        }
        return QObject::eventFilter(watched, event);
    }

  private:
    [[nodiscard]] bool canAnimateWindow() const {
        if (!m_foreground)
            return false;
        return canAnimateProcessWindow();
    }
    [[nodiscard]] bool canAnimateProcessWindow() const {
        if (m_windows.isEmpty())
            return true;
        return std::ranges::any_of(m_windows, [](const auto* window) {
            return window != nullptr && window->isVisible()
                && window->visibility() != QWindow::Hidden
                && window->visibility() != QWindow::Minimized;
        });
    }
    void noteWindowStateChanged() {
        const bool exposed = canAnimateProcessWindow();
        if (exposed == m_windowExposed)
            return;
        m_windowExposed = exposed;
        schedule();
        ++m_revision;
        ++m_processRevision;
        ++m_workingRevision;
        emit changed();
        emit tick();
        emit processTick();
    }
    void schedule() {
        const bool animate = canAnimateWindow() && !m_reduced;
        if (animate && !m_observers.isEmpty() && !m_epochs.isEmpty())
            m_timer.start();
        else
            m_timer.stop();
        if (canAnimateProcessWindow() && !m_reduced && !m_processObservers.isEmpty())
            m_processTimer.start();
        else
            m_processTimer.stop();
    }
    bool m_foreground = true;
    bool m_windowExposed = true;
    QSet<QWindow*> m_windows;
    QSet<QObject*> m_observers;
    QSet<QObject*> m_knownObservers;
    QSet<QObject*> m_processObservers;
    QSet<QObject*> m_knownProcessObservers;
    QElapsedTimer m_elapsed;
    QTimer m_timer;
    QTimer m_processTimer;
    QHash<QString, qint64> m_epochs;
    bool m_reduced = false;
    quint64 m_revision = 0;
    quint64 m_processRevision = 0;
    quint64 m_workingRevision = 0;
  signals:
    void changed();
    void tick();
    void processTick();
};
} // namespace clarp
