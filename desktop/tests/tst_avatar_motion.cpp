#include "app/AvatarMotionClock.h"
#include <QCoreApplication>
#include <QSettings>
#include <QTemporaryDir>
#include <QThread>
#define CHECK(condition) do { if (!(condition)) qFatal("Check failed: %s", #condition); } while (false)
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
    CHECK(!clock.ticking());
    clock.observe(&observer, true);
    CHECK(clock.ticking());
    QThread::msleep(35);
    const auto p = clock.phase(QStringLiteral("A"));
    clock.reconcile({QStringLiteral("A")});
    CHECK(clock.phase(QStringLiteral("A")) >= p);
    clock.setReducedMotion(true);
    CHECK(!clock.ticking());
    CHECK(clock.phase(QStringLiteral("A")) == 0);
    clock.setReducedMotion(false);
    CHECK(clock.ticking());
    app.applicationStateChanged(Qt::ApplicationInactive);
    CHECK(!clock.ticking());
    app.applicationStateChanged(Qt::ApplicationActive);
    CHECK(clock.ticking());
    clock.observe(&observer, false);
    CHECK(!clock.ticking());
    clock.observe(&observer, true);
    clock.reconcile({});
    CHECK(!clock.ticking());
    CHECK(!clock.working(QStringLiteral("A")));
    return 0;
}
