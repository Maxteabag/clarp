#include "app/AvatarMotionClock.h"
#include <QCoreApplication>
#include <QSettings>
#include <QTemporaryDir>
#include <QThread>
#include <cassert>
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
    assert(!clock.ticking());
    clock.observe(&observer, true);
    assert(clock.ticking());
    QThread::msleep(35);
    const auto p = clock.phase(QStringLiteral("A"));
    clock.reconcile({QStringLiteral("A")});
    assert(clock.phase(QStringLiteral("A")) >= p);
    clock.setReducedMotion(true);
    assert(!clock.ticking());
    assert(clock.phase(QStringLiteral("A")) == 0);
    clock.setReducedMotion(false);
    assert(clock.ticking());
    app.applicationStateChanged(Qt::ApplicationInactive);
    assert(!clock.ticking());
    app.applicationStateChanged(Qt::ApplicationActive);
    assert(clock.ticking());
    clock.observe(&observer, false);
    assert(!clock.ticking());
    clock.observe(&observer, true);
    clock.reconcile({});
    assert(!clock.ticking());
    assert(!clock.working(QStringLiteral("A")));
    return 0;
}
