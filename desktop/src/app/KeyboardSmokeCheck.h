#pragma once

#include "app/AppController.h"
#include <QGuiApplication>
#include <QJsonArray>
#include <QKeyEvent>
#include <QQuickItem>
#include <QQuickWindow>
#include <QTimer>

// Isolated fixture lane: real key delivery to this offscreen window only.
inline void startKeyboardSmokeCheck(QGuiApplication& application, QQuickWindow* window,
                                    clarp::AppController* controller) {
    if (QGuiApplication::platformName() != QStringLiteral("offscreen") || window == nullptr || controller == nullptr) {
        application.exit(EXIT_FAILURE);
        return;
    }
    auto* timer = new QTimer(&application);
    timer->setInterval(90);
    QObject::connect(timer, &QTimer::timeout, &application,
        [&application, window, controller, timer, step = 0]() mutable {
        const auto press = [window](Qt::Key key, Qt::KeyboardModifiers modifiers = {}, const QString& text = {}) {
            QKeyEvent down(QEvent::KeyPress, key, modifiers, text);
            QKeyEvent up(QEvent::KeyRelease, key, modifiers, text);
            QCoreApplication::sendEvent(window, &down);
            QCoreApplication::sendEvent(window, &up);
        };
        const auto require = [&application, window, timer, step](bool ok) {
            if (!ok) { qCritical("Context keyboard check failed at step %d (focus: %s)", step, qPrintable(window->activeFocusItem() ? window->activeFocusItem()->objectName() : QStringLiteral("none"))); timer->stop(); application.exit(EXIT_FAILURE); }
            return ok;
        };
        auto* map = window->findChild<QObject*>(QStringLiteral("keyboardMap"));
        auto* rail = window->findChild<QObject*>(QStringLiteral("sidebarRail"));
        if (!require(map != nullptr && rail != nullptr)) return;
        const auto state = [map](const char* name) { return map->property("contextName").toString() == QLatin1String(name); };
        const QString second = QStringLiteral("keyboard-second");
        const auto selected = [rail, &second] { return rail->property("keyboardSession").toString() == second; };
        switch (step++) {
        case 0: {
            QJsonArray agents;
            for (const auto& session : {QStringLiteral("keyboard-first"), second}) {
                agents.append(QJsonObject{{QStringLiteral("session"), session},
                    {QStringLiteral("agent_id"), session}, {QStringLiteral("persona"), session},
                    {QStringLiteral("backend"), QStringLiteral("local")}, {QStringLiteral("latest_state"), QStringLiteral("idle")}});
            }
            controller->agents()->applySnapshot({{QStringLiteral("agents"), agents}});
            controller->selectSession(second);
            // The screenshot fixture can change the pane under a signal blocker.
            // Restore both identities even when selection was persisted as second.
            controller->panes()->setActiveSession(second);
            controller->setPaneDraft(controller->panes()->activePaneId(), second, QString{});
            controller->requestComposerFocus(controller->panes()->activePaneId());
            break;
        }
        case 1:
            if (!require(state("composer"))) return;
            press(Qt::Key_E, {}, QStringLiteral("e"));
            press(Qt::Key_J, {}, QStringLiteral("j"));
            press(Qt::Key_K, {}, QStringLiteral("k"));
            press(Qt::Key_I, {}, QStringLiteral("i"));
            press(Qt::Key_Space, {}, QStringLiteral(" "));
            if (!require(window->activeFocusItem()->property("text").toString() == QStringLiteral("ejki "))) return;
            press(Qt::Key_Escape);
            break;
        case 2:
            if (!require(state("pane"))) return;
            press(Qt::Key_E);
            break;
        case 3:
            if (!require(state("sidebar") && selected())) return;
            press(Qt::Key_K);
            break;
        case 4:
            if (!require(!selected() && controller->selectedSession() == second)) return;
            press(Qt::Key_J);
            press(Qt::Key_Return);
            break;
        case 5:
            if (!require(state("composer") && controller->selectedSession() == second)) return;
            press(Qt::Key_Escape);
            press(Qt::Key_Tab);
            break;
        case 6:
            if (!require(state("sidebar") && selected())) return;
            press(Qt::Key_Tab);
            break;
        case 7:
            if (!require(state("pane"))) return;
            press(Qt::Key_I);
            break;
        case 8:
            if (!require(state("composer") && window->activeFocusItem()->property("text").toString() == QStringLiteral("ejki "))) return;
            press(Qt::Key_K, Qt::ControlModifier);
            break;
        case 9:
            if (!require(state("modal"))) return;
            press(Qt::Key_Escape);
            break;
        case 10:
            if (!require(state("composer"))) return;
            press(Qt::Key_B, Qt::ControlModifier);
            break;
        case 11:
            if (!require(!window->property("sidebarVisible").toBool())) return;
            press(Qt::Key_Escape);
            press(Qt::Key_E);
            break;
        case 12:
            if (!require(state("sidebar") && selected() && window->property("sidebarVisible").toBool())) return;
            press(Qt::Key_Slash);
            break;
        case 13:
            if (!require(state("search"))) return;
            press(Qt::Key_Z, {}, QStringLiteral("zzzzzz"));
            break;
        case 14:
            if (!require(state("search") && rail->property("rowCount").toInt() == 0)) return;
            press(Qt::Key_Escape);
            break;
        case 15:
            if (!require(state("sidebar") && selected() && rail->property("rowCount").toInt() == 2)) return;
            controller->agents()->applyNotificationEvent({{QStringLiteral("session"), QStringLiteral("keyboard-first")}});
            break;
        case 16:
            if (!require(map->property("hasAttention").toBool())) return;
            press(Qt::Key_N);
            break;
        case 17:
            if (!require(state("composer") && controller->selectedSession() == QStringLiteral("keyboard-first"))) return;
            controller->agents()->applyNotificationEvent({{QStringLiteral("session"), second}});
            break;
        case 18:
            if (!require(map->property("hasAttention").toBool())) return;
            press(Qt::Key_J, Qt::ControlModifier);
            break;
        case 19:
            if (!require(state("composer") && controller->selectedSession() == second
                         && !map->property("hasAttention").toBool())) return;
            press(Qt::Key_Escape);
            press(Qt::Key_E);
            break;
        default:
            if (!require(state("sidebar") && selected())) return;
            window->setProperty("contextKeyboardVerified", true);
            timer->stop();
        }
    });
    QTimer::singleShot(2'050, timer, [timer] { timer->start(); });
}
