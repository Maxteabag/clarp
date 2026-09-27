#include "app/AvatarMotionClock.h"
#include <QCoreApplication>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QSettings>
#include <QTemporaryDir>
#include <QThread>
#include <QWindow>
static void check(bool condition) {
    if (!condition) qFatal("Avatar motion lifecycle assertion failed");
}
static void spin(QCoreApplication& app, int ms) {
    QElapsedTimer elapsed;
    elapsed.start();
    while (elapsed.elapsed() < ms) {
        app.processEvents(QEventLoop::AllEvents, 20);
        QThread::msleep(10);
    }
    app.processEvents(QEventLoop::AllEvents);
}
static void checkProcessStill(QCoreApplication& app, clarp::AvatarMotionClock& clock, int ms) {
    const quint64 before = clock.processRevision();
    spin(app, ms);
    check(clock.processRevision() == before);
}
static void checkProcessAdvances(QCoreApplication& app, clarp::AvatarMotionClock& clock, int ms) {
    const quint64 before = clock.processRevision();
    spin(app, ms);
    check(clock.processRevision() > before);
}
int main(int argc, char** argv) {
    QGuiApplication app(argc, argv);
    QTemporaryDir config;
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, config.path());
    QCoreApplication::setOrganizationName("MotionTest");
    QCoreApplication::setApplicationName("MotionTest");
    clarp::AvatarMotionClock clock;
    QObject observer;
    app.applicationStateChanged(Qt::ApplicationActive);
    clock.setReducedMotion(false);
    clock.reconcile({QStringLiteral("A")});
    check(!clock.ticking());
    clock.observe(&observer, true);
    check(clock.ticking());
    QThread::msleep(35);
    const auto p = clock.phase(QStringLiteral("A"));
    clock.reconcile({QStringLiteral("A")});
    check(clock.phase(QStringLiteral("A")) >= p);
    clock.setReducedMotion(true);
    check(!clock.ticking());
    check(clock.phase(QStringLiteral("A")) == 0);
    clock.setReducedMotion(false);
    check(clock.ticking());
    app.applicationStateChanged(Qt::ApplicationInactive);
    check(!clock.ticking());
    app.applicationStateChanged(Qt::ApplicationActive);
    check(clock.ticking());
    clock.observe(&observer, false);
    check(!clock.ticking());
    clock.observe(&observer, true);
    clock.reconcile({});
    check(!clock.ticking());
    check(!clock.working(QStringLiteral("A")));

    QObject processObserver;
    QWindow firstWindow;
    QWindow secondWindow;
    clock.watchWindow(&firstWindow);
    clock.watchWindow(&secondWindow);
    clock.observeProcess(&processObserver, true);
    checkProcessStill(app, clock, 450);
    secondWindow.show();
    checkProcessAdvances(app, clock, 900);
    secondWindow.hide();
    checkProcessStill(app, clock, 450);
    firstWindow.showMinimized();
    checkProcessStill(app, clock, 450);
    firstWindow.show();
    checkProcessAdvances(app, clock, 900);
    clock.setReducedMotion(true);
    check(clock.processHop() == 0);
    checkProcessStill(app, clock, 450);
    return 0;
}
