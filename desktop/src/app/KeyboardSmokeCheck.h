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
            if (!require(!window->property("sidebarVisible").toBool())) return;
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
            {
                const bool hints = window->property("shortcutsVisible").toBool();
                press(Qt::Key_K, Qt::ControlModifier | Qt::ShiftModifier);
                if (!require(window->property("shortcutsVisible").toBool() != hints && state("composer"))) return;
                press(Qt::Key_K, Qt::ControlModifier | Qt::ShiftModifier);
                if (!require(window->property("shortcutsVisible").toBool() == hints
                    && window->activeFocusItem()->property("text").toString() == QStringLiteral("ejki "))) return;
            }
            // Escape dismisses a visible error before changing composer focus.
            controller->conversationForSession(second)->setError(QStringLiteral("Keyboard error fixture"));
            press(Qt::Key_Escape);
            if (!require(controller->conversationForSession(second)->error().isEmpty()
                && state("composer")
                && window->activeFocusItem()->property("text").toString() == QStringLiteral("ejki "))) return;
            controller->conversationForSession(second)->setError(QStringLiteral("Palette error fixture"));
            QMetaObject::invokeMethod(window, "runCommand", Q_ARG(QVariant, QVariant(QStringLiteral("dismiss-error"))));
            if (!require(controller->conversationForSession(second)->error().isEmpty())) return;
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
            // Selecting agents can produce a real error from the deliberately
            // unavailable fixture Host. Dismiss it before testing navigation.
            if (!controller->errorMessage().isEmpty()
                || !controller->conversationForSession(second)->error().isEmpty()) {
                press(Qt::Key_Escape);
                if (!require(controller->errorMessage().isEmpty()
                    && controller->conversationForSession(second)->error().isEmpty()
                    && state("composer"))) return;
            }
            press(Qt::Key_Escape);
            press(Qt::Key_E);
            break;
        case 20:
            if (!require(state("sidebar") && selected())) return;
            press(Qt::Key_N, Qt::ControlModifier | Qt::ShiftModifier);
            break;
        case 21:
            if (!require(state("modal") && window->activeFocusItem() != nullptr
                         && window->activeFocusItem()->objectName() == QStringLiteral("quickNewAgentName"))) return;
            press(Qt::Key_Escape);
            break;
        case 22:
            if (!require(state("composer"))) return;
            press(Qt::Key_A, Qt::ControlModifier);
            break;
        case 23:
            if (!require(state("modal") && window->findChild<QObject*>(QStringLiteral("assignAgent"))->property("visible").toBool())) return;
            press(Qt::Key_Escape);
            break;
        case 24:
            if (!require(state("composer") && window->activeFocusItem()->property("text").toString() == QStringLiteral("ejki "))) return;
            press(Qt::Key_A, Qt::ControlModifier | Qt::ShiftModifier);
            break;
        case 25:
            if (!require(state("modal") && window->findChild<QObject*>(QStringLiteral("assignAgent"))->property("visible").toBool())) return;
            press(Qt::Key_Escape);
            break;
        case 26: {
            if (!require(state("composer"))) return;
            QJsonArray turns;
            for (int row = 0; row < 80; ++row) {
                turns.append(QJsonObject{{QStringLiteral("id"), QStringLiteral("scroll-%1").arg(row)},
                    {QStringLiteral("role"), QStringLiteral("assistant")},
                    {QStringLiteral("text"), QStringLiteral("Keyboard scroll fixture row %1: a retained conversation message.").arg(row)},
                    {QStringLiteral("timestamp"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)}});
            }
            controller->conversationForSession(second)->applyLog(
                {{QStringLiteral("conversation_id"), QStringLiteral("keyboard-scroll")},
                 {QStringLiteral("turns"), turns}}, clarp::ConversationModel::LoadKind::Replace);
            break;
        }
        case 27: {
            auto* transcript = window->findChild<QObject*>(QStringLiteral("transcriptList"));
            if (!require(transcript && transcript->property("contentHeight").toReal() > transcript->property("height").toReal())) return;
            QMetaObject::invokeMethod(transcript, "pauseFollowing");
            QMetaObject::invokeMethod(transcript, "positionViewAtBeginning");
            break;
        }
        case 28: {
            auto* transcript = window->findChild<QObject*>(QStringLiteral("transcriptList"));
            if (!require(transcript && !transcript->property("atYEnd").toBool())) return;
            press(Qt::Key_End, Qt::ControlModifier);
            break;
        }
        case 29: {
            auto* transcript = window->findChild<QObject*>(QStringLiteral("transcriptList"));
            if (!require(transcript && transcript->property("followLatest").toBool()
                && transcript->property("atYEnd").toBool() && state("composer")
                && window->activeFocusItem()->property("text").toString() == QStringLiteral("ejki "))) return;
            QMetaObject::invokeMethod(transcript, "pauseFollowing");
            QMetaObject::invokeMethod(transcript, "positionViewAtBeginning");
            QMetaObject::invokeMethod(window, "runCommand", Q_ARG(QVariant, QVariant(QStringLiteral("jump-latest"))));
            break;
        }
        case 30: {
            auto* transcript = window->findChild<QObject*>(QStringLiteral("transcriptList"));
            if (!require(transcript && transcript->property("atYEnd").toBool())) return;
            QMetaObject::invokeMethod(transcript, "pauseFollowing");
            QMetaObject::invokeMethod(transcript, "positionViewAtBeginning");
            break;
        }
        case 31: {
            auto* latest = window->findChild<QObject*>(QStringLiteral("jumpToLatestButton"));
            if (!require(latest && latest->property("visible").toBool())) return;
            QMetaObject::invokeMethod(latest, "clicked");
            break;
        }
        default:
            if (!require(window->findChild<QObject*>(QStringLiteral("transcriptList"))->property("atYEnd").toBool())) return;
            if (!require(state("composer") && controller->selectedSession() == second)) return;
            window->setProperty("contextKeyboardVerified", true);
            timer->stop();
        }
    });
    QTimer::singleShot(2'050, timer, [timer] { timer->start(); });
}
