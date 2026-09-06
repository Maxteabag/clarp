#pragma once
#include "app/AppController.h"
#include <QGuiApplication>
#include <QJsonArray>
#include <QQuickItem>
#include <QQuickWindow>
#include <QTimer>
inline void startReadyReplySmokeCheck(QGuiApplication& application, QQuickWindow* window,
                                      clarp::AppController* controller, const QString& screenshotPath) {
    if (QGuiApplication::platformName() != QStringLiteral("offscreen") || window == nullptr || controller == nullptr) {
        application.exit(EXIT_FAILURE); return;
    }
    auto* timer = new QTimer(&application);
    timer->setInterval(140);
    QObject::connect(timer, &QTimer::timeout, &application,
        [&application, window, controller, screenshotPath, timer, step = 0]() mutable {
        const auto items = [window] {
            QList<QQuickItem*> pending{window->contentItem()};
            QList<QQuickItem*> result;
            while (!pending.isEmpty()) {
                auto* item = pending.takeLast(); result.append(item); pending.append(item->childItems());
            }
            return result;
        }();
        bool typing = false;
        bool partial = false;
        bool final = false;
        bool tool = false;
        for (const auto* item : items) {
            if (item->objectName() == QStringLiteral("replyTypingIndicator") && item->isVisible()) typing = true;
            if (item->objectName() == QStringLiteral("toolCard") && item->isVisible()
                && item->property("summary").toString() == QStringLiteral("Search project files")) tool = true;
            if (item->objectName() == QStringLiteral("messageTextBlock")) {
                partial = partial || item->property("text").toString().contains(QStringLiteral("Provisional secret reply"));
                final = final || item->property("text").toString().contains(QStringLiteral("Finished answer"));
            }
        }
        const auto require = [&application, timer, step](bool ok) {
            if (!ok) { qCritical("Ready reply check failed at step %d", step); timer->stop(); application.exit(EXIT_FAILURE); }
            return ok;
        };
        const QString session = controller->selectedSession().isEmpty()
            ? controller->panes()->activeSession() : controller->selectedSession();
        auto* model = controller->conversationForSession(session);
        if (!require(model != nullptr && !session.isEmpty())) return;
        const auto update = [model](const QString& kind, const QString& text, int revision) {
            model->applyLog({{QStringLiteral("conversation_id"), model->conversationId()},
                {QStringLiteral("turns"), QJsonArray{QJsonObject{
                    {QStringLiteral("id"), QStringLiteral("ready-reply")}, {QStringLiteral("role"), QStringLiteral("assistant")},
                    {QStringLiteral("kind"), kind}, {QStringLiteral("text"), text}, {QStringLiteral("revision"), revision},
                    {QStringLiteral("tools"), QJsonArray{QJsonObject{
                        {QStringLiteral("name"), QStringLiteral("Bash")},
                        {QStringLiteral("summary"), QStringLiteral("Search project files")}}}}}}},
                {QStringLiteral("latest_revision"), revision}}, clarp::ConversationModel::LoadKind::Delta);
        };
        switch (step++) {
        case 0:
            controller->setShowWhenReady(true);
            update(QStringLiteral("live"), QStringLiteral("Provisional secret reply"), 1000);
            controller->agents()->applyStateEvent({{QStringLiteral("session"), session}, {QStringLiteral("kind"), QStringLiteral("thinking")}, {QStringLiteral("ts"), 4'000'000'000'000LL}});
            break;
        case 1:
            if (!require(typing && !partial && tool && model->indexOfMessage(QStringLiteral("ready-reply")) >= 0)) return;
            if (!require(window->grabWindow().save(screenshotPath + QStringLiteral(".typing.png")))) return;
            update(QStringLiteral("assistant"), QStringLiteral("Finished answer"), 1001);
            controller->agents()->applyStateEvent({{QStringLiteral("session"), session}, {QStringLiteral("kind"), QStringLiteral("done")}, {QStringLiteral("ts"), 4'000'000'000'001LL}});
            break;
        default:
            if (!require(!typing && !partial && final && tool)) return;
            window->setProperty("readyReplyVerified", true);
            timer->stop();
        }
    });
    QTimer::singleShot(2'050, timer, [timer] { timer->start(); });
}
