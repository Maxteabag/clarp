#include "app/AvatarMotionClock.h"
#include <QDebug>
#include <QDir>
#include <QGuiApplication>
#include <QImage>
#include <QQmlComponent>
#include <QQmlContext>
#include <QQmlEngine>
#include <QQuickWindow>
#include <QTemporaryDir>
#include <QTimer>
int main(int argc, char** argv) {
    QGuiApplication app(argc, argv);
    QTemporaryDir settings;
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, settings.path());
    app.setOrganizationName("AvatarRenderTest");
    app.setApplicationName("AvatarRenderTest");
    clarp::AvatarMotionClock clock;
    app.applicationStateChanged(Qt::ApplicationActive);
    clock.setReducedMotion(false);
    clock.reconcile({QStringLiteral("A")});
    QQmlEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("proofClock"), &clock);
    QQmlComponent component(&engine, QUrl::fromLocalFile(QStringLiteral(CLARP_AVATAR_TEST_QML)));
    QScopedPointer<QObject> root(component.create());
    if (!root) {
        qCritical() << component.errors();
        return 2;
    }
    auto* window = qobject_cast<QQuickWindow*>(root.data());
    if (window == nullptr)
        return 3;
    window->show();
    QImage active;
    QImage reduced;
    QImage background;
    QString dir = qEnvironmentVariable("CLARP_AVATAR_PROOF_DIR");
    if (!dir.isEmpty())
        QDir().mkpath(dir);
    auto shot = [&](const QString& name) {
        auto image = window->grabWindow();
        if (!dir.isEmpty())
            image.save(dir + QLatin1Char('/') + name + QStringLiteral(".png"));
        return image;
    };
    QTimer::singleShot(300, &app, [&] {
        active = shot(QStringLiteral("active-first"));
        if (!clock.ticking())
            app.exit(4);
    });
    QTimer::singleShot(900, &app, [&] {
        if (active == shot(QStringLiteral("active-second")))
            app.exit(5);
        clock.setReducedMotion(true);
    });
    QTimer::singleShot(1200, &app, [&] {
        reduced = shot(QStringLiteral("reduced-first"));
        if (clock.ticking())
            app.exit(6);
    });
    QTimer::singleShot(1700, &app, [&] {
        if (reduced != shot(QStringLiteral("reduced-second")))
            app.exit(7);
        clock.setReducedMotion(false);
        app.applicationStateChanged(Qt::ApplicationInactive);
    });
    QTimer::singleShot(2000, &app, [&] {
        background = shot(QStringLiteral("background-first"));
        if (clock.ticking())
            app.exit(8);
    });
    QTimer::singleShot(2500, &app, [&] {
        if (background != shot(QStringLiteral("background-second")))
            app.exit(9);
        window->hide();
        app.applicationStateChanged(Qt::ApplicationActive);
    });
    QTimer::singleShot(2700, &app, [&] {
        if (clock.ticking())
            app.exit(10);
        window->show();
    });
    QTimer::singleShot(2900, &app, [&] {
        if (!clock.ticking())
            app.exit(11);
        clock.reconcile({});
    });
    QTimer::singleShot(3100, &app, [&] {
        shot(QStringLiteral("idle"));
        if (clock.ticking())
            app.exit(12);
        else
            app.quit();
    });
    return app.exec();
}
