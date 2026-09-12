#include "app/AvatarMotionClock.h"
#include <QCoreApplication>
#include <QSettings>
#include <QTemporaryDir>
#include <QThread>
static void check(bool condition) {
    if (!condition) qFatal("Avatar motion lifecycle assertion failed");
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
    return 0;
}
