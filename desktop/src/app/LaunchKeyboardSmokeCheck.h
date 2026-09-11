#pragma once
#include <QCoreApplication>
#include <QDebug>
#include <QGuiApplication>
#include <QMap>
#include <QStringList>
#include <QKeyEvent>
#include <QQuickItem>
#include <QQuickWindow>
#include <QTimer>

// Real key delivery, restricted to an offscreen desktop with fixture-only auth.
inline void startLaunchKeyboardSmokeCheck(QQuickWindow* window) {
    const QString sequence = qEnvironmentVariable("CLARP_SCREENSHOT_LAUNCH_KEYS");
    if (sequence.isEmpty()) return;
    if (QGuiApplication::platformName() != QStringLiteral("offscreen")
        || qEnvironmentVariable("CLARP_TOKEN") != QStringLiteral("offline-launch-keyboard-fixture")) {
        qCritical("Launch keyboard simulation requires isolated fixture authentication");
        QCoreApplication::exit(EXIT_FAILURE);
        return;
    }
    const QMap<QString, Qt::Key> keys{
        {QStringLiteral("Left"), Qt::Key_Left}, {QStringLiteral("Right"), Qt::Key_Right},
        {QStringLiteral("Up"), Qt::Key_Up}, {QStringLiteral("Down"), Qt::Key_Down},
        {QStringLiteral("Tab"), Qt::Key_Tab}, {QStringLiteral("Backtab"), Qt::Key_Backtab},
        {QStringLiteral("M"), Qt::Key_M}, {QStringLiteral("Enter"), Qt::Key_Return},
        {QStringLiteral("Escape"), Qt::Key_Escape}};
    const QStringList input = sequence.split(u',');
    for (const QString& name : input) {
        if (!keys.contains(name)) { qCritical("Unknown simulated key"); QCoreApplication::exit(EXIT_FAILURE); return; }
    }
    QTimer::singleShot(1200, window, [window, input, keys] {
        if (!window->activeFocusItem() || !window->activeFocusItem()->objectName().startsWith(QStringLiteral("providerCard-"))) {
            qCritical().noquote() << "Startup stole focus from the provider cards:" << (window->activeFocusItem() ? window->activeFocusItem()->objectName() : QStringLiteral("none"));
            QCoreApplication::exit(EXIT_FAILURE);
            return;
        }
        auto* timer = new QTimer(window);
        timer->setInterval(120);
        QObject::connect(timer, &QTimer::timeout, window, [window, timer, input, keys, index = 0]() mutable {
            if (index >= input.size()) { timer->stop(); timer->deleteLater(); return; }
            const QString keyName = input.at(index++);
            const Qt::Key key = keys.value(keyName);
            qInfo().noquote() << "Simulated key" << keyName << "focus"
                << (window->activeFocusItem() ? window->activeFocusItem()->objectName() : QStringLiteral("none"));
            QKeyEvent down(QEvent::KeyPress, key, Qt::NoModifier);
            QKeyEvent up(QEvent::KeyRelease, key, Qt::NoModifier);
            QCoreApplication::sendEvent(window, &down);
            QCoreApplication::sendEvent(window, &up);
        });
        timer->start();
    });
}
