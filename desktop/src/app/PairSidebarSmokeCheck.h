#pragma once
#include "app/AppController.h"
#include <QGuiApplication>
#include <QQuickItem>
#include <QQuickWindow>
#include <QTimer>

// Agent-to-agent rooms are a secondary list behind one row. The user's own
// conversations must keep the sidebar body, so this asserts the two lists
// swap rather than stack: 25 real rooms once pushed every chat off-screen.
inline void startPairSidebarSmokeCheck(QGuiApplication& application, QQuickWindow* window,
                                       clarp::AppController* controller) {
    if (QGuiApplication::platformName() != QStringLiteral("offscreen") || window == nullptr
        || controller == nullptr) {
        application.exit(EXIT_FAILURE);
        return;
    }
    window->setProperty("sidebarVisible", true); // This lane exercises the expanded sidebar.
    auto* timer = new QTimer(&application);
    timer->setInterval(120);
    QObject::connect(timer, &QTimer::timeout, &application,
        [&application, window, timer, step = 0]() mutable {
        auto* list = window->findChild<QObject*>(QStringLiteral("chatList"));
        auto* agents = window->findChild<QQuickItem*>(QStringLiteral("sidebarAgentList"));
        auto* rooms = window->findChild<QQuickItem*>(QStringLiteral("pairConversationList"));
        const auto require = [&application, timer, step](bool ok, const char* what) {
            if (!ok) {
                qCritical("Pair sidebar check failed at step %d: %s", step, what);
                timer->stop();
                application.exit(EXIT_FAILURE);
            }
            return ok;
        };
        if (!require(list != nullptr && agents != nullptr && rooms != nullptr, "sidebar objects")) return;
        switch (step++) {
        case 0:
            if (!require(!list->property("showingPairs").toBool(), "rooms must start collapsed")) return;
            if (!require(agents->isVisible(), "agent list must own the sidebar")) return;
            if (!require(!rooms->isVisible(), "room list must stay hidden")) return;
            list->setProperty("showingPairs", true);
            break;
        case 1:
            if (!require(rooms->isVisible(), "room list must open")) return;
            if (!require(!agents->isVisible(), "lists must swap, never stack")) return;
            list->setProperty("showingArchive", true);
            break;
        case 2:
            // Archived and rooms are mutually exclusive secondary lists.
            if (!require(!rooms->isVisible(), "archive must replace the room list")) return;
            list->setProperty("showingArchive", false);
            list->setProperty("showingPairs", false);
            break;
        default:
            if (!require(agents->isVisible(), "agent list must return")) return;
            if (!require(!rooms->isVisible(), "room list must close")) return;
            timer->stop();
            application.exit(EXIT_SUCCESS);
            return;
        }
    });
    timer->start();
}
