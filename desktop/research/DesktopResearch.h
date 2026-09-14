#pragma once
#include "app/AppController.h"
#include <QQuickWindow>
#include <QQuickItem>
#include <QJsonDocument>
#include <QFile>
#include <QDir>
#include <QTimer>
#include <QDateTime>
#include <QMetaObject>
#include <QGuiApplication>
#include <QImage>
#include <QPointer>
#include <memory>

// Synthetic, offscreen-only runner shared unchanged by baseline and candidate.
inline void startDesktopResearch(QGuiApplication& app, QQuickWindow* window, clarp::AppController* controller) {
    const QString scenario=qEnvironmentVariable("CLARP_DESKTOP_RESEARCH");
    const QString output=qEnvironmentVariable("CLARP_DESKTOP_RESEARCH_OUTPUT");
    if(scenario.isEmpty()) return;
    if(QGuiApplication::platformName()!=QStringLiteral("offscreen") || output.isEmpty()) {app.exit(2);return;}
    QDir().mkpath(output);
    auto original=std::make_shared<QPointer<QQuickItem>>();
    const auto find=[window](const QString& name)->QQuickItem* {
        QList<QQuickItem*> pending{window->contentItem()};
        while(!pending.isEmpty()){auto* i=pending.takeLast();pending.append(i->childItems());if(i->objectName()==name && i->isVisible())return i;}return nullptr;
    };
    QTimer::singleShot(2150,&app,[controller,window,scenario,find,output]{
        controller->setMuted(true);
        QJsonArray agents;
        for(const QString& name:{QStringLiteral("Aura"),QStringLiteral("Boreal"),QStringLiteral("Cedar")})
            agents.append(QJsonObject{{QStringLiteral("session"),name},{QStringLiteral("agent_id"),name},{QStringLiteral("persona"),name},{QStringLiteral("backend"),QStringLiteral("local")},{QStringLiteral("latest_state"),QStringLiteral("idle")}});
        controller->agents()->applySnapshot({{QStringLiteral("agents"),agents}});
        controller->selectSession(QStringLiteral("Aura"));controller->panes()->setActiveSession(QStringLiteral("Aura"));
        for(const QString& name:{QStringLiteral("Aura"),QStringLiteral("Boreal"),QStringLiteral("Cedar")}) {
            QJsonArray rows;
            for(int i=0;i<55;i++) rows.append(QJsonObject{{QStringLiteral("id"),QStringLiteral("message-%1").arg(i)},
                {QStringLiteral("role"),i%3==0?QStringLiteral("user"):QStringLiteral("assistant")},
                {QStringLiteral("text"), i%3==0 ? QStringLiteral("Review the workspace changes and keep the open questions together.") : QStringLiteral("The conversation keeps its own draft and reading position. I have checked the changes and recorded the remaining decisions.\n\nThe current behavior is ready to review.")},
                {QStringLiteral("kind"),QStringLiteral("message")},{QStringLiteral("timestamp"),QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)}});
            controller->conversationForSession(name)->applyLog({{QStringLiteral("conversation_id"),name},{QStringLiteral("turns"),rows}},clarp::ConversationModel::LoadKind::Replace);
        }
        controller->clearError();controller->conversationForSession(QStringLiteral("Aura"))->setError({});
        if(scenario==QStringLiteral("composer")) controller->setPaneDraft(controller->panes()->activePaneId(),QStringLiteral("Aura"),{});
        if(scenario==QStringLiteral("keys"))QMetaObject::invokeMethod(window,"runCommand",Q_ARG(QVariant,QVariant(QStringLiteral("edit-keymap"))));
        if(scenario==QStringLiteral("workspaces")) {
            QMetaObject::invokeMethod(controller->panes(),"createWorkspace",Q_ARG(QString,QStringLiteral("Review")));
            controller->panes()->setActiveSession(QStringLiteral("Boreal"));
            controller->panes()->splitActive(QStringLiteral("vertical"),QStringLiteral("Cedar"));
        }
        if(scenario==QStringLiteral("zoom")){controller->panes()->splitActive(QStringLiteral("vertical"),QStringLiteral("Boreal"));controller->panes()->toggleZoom();}
        Q_UNUSED(find)
        Q_UNUSED(output)
    });
    QTimer::singleShot(2550,&app,[controller,window,scenario,find,output,original]{
        controller->clearError();
        if(auto* overlay=find(QStringLiteral("connectionPage")))overlay->setVisible(false);
        if(scenario!=QStringLiteral("lifecycle"))return;
        auto* view=find(QStringLiteral("transcriptList"));if(!view)return;
        *original=view;
        QMetaObject::invokeMethod(view,"pauseFollowing");
        view->setProperty("contentY",800.0);
        QJsonObject before{{QStringLiteral("contentY"),view->property("contentY").toDouble()},{QStringLiteral("follow"),view->property("followLatest").toBool()}};
        window->setProperty("researchBefore",before.toVariantMap());
        window->grabWindow().save(output+QStringLiteral("/before.png"));
        controller->panes()->splitActive(QStringLiteral("vertical"),QStringLiteral("Boreal"));
    });
    if(scenario==QStringLiteral("lifecycle")) {
        auto* frames=new QTimer(&app); frames->setInterval(100);
        QObject::connect(frames,&QTimer::timeout,&app,[window,output,frames,index=0]() mutable {
            window->grabWindow().save(output+QStringLiteral("/frame-%1.png").arg(index++,3,10,QChar('0')));
            if(index>=15)frames->stop();
        });
        QTimer::singleShot(2350,frames,[frames]{frames->start();});
    }
    QTimer::singleShot(3550,&app,[window,controller,scenario,output]{

        if(scenario==QStringLiteral("zoom")) {
            window->grabWindow().save(output+QStringLiteral("/maximized.png"));
            controller->panes()->toggleZoom();
        }
        if(scenario==QStringLiteral("workspaces")) {
            window->grabWindow().save(output+QStringLiteral("/review.png"));
            QMetaObject::invokeMethod(controller->panes(),"switchWorkspace",Q_ARG(QString,QStringLiteral("workspace-1")));
        }
    });
    if (scenario == QStringLiteral("keys")) {
        QTimer::singleShot(2900, &app, [window, output] {
            window->grabWindow().save(output + QStringLiteral("/keymap.png"));
            QMetaObject::invokeMethod(window, "escapeFocus");
        });
        QTimer::singleShot(3250, &app, [window, output] {
            const auto* editor = window->findChild<QObject*>(QStringLiteral("keymapEditor"));
            const auto* focus = window->activeFocusItem();
            const bool passed = editor != nullptr && !editor->property("visible").toBool()
                && focus != nullptr && focus->objectName() == QStringLiteral("paneComposerEditor");
            QFile file(output + QStringLiteral("/keymap-focus.json"));
            if (file.open(QIODevice::WriteOnly)) file.write(QJsonDocument(QJsonObject{
                {QStringLiteral("passed"), passed},
                {QStringLiteral("focus"), focus != nullptr ? focus->objectName() : QString{}}
            }).toJson());
        });
    }
    QTimer::singleShot(3250,&app,[window,scenario,find,output,original]{
        if(scenario!=QStringLiteral("lifecycle"))return;
        // Inspect the original Aura pane even after focus moved to Boreal.
        QList<QQuickItem*> pending{window->contentItem()};QQuickItem* found=nullptr;
        while(!pending.isEmpty()){auto* i=pending.takeLast();pending.append(i->childItems());if(i->objectName()==QStringLiteral("transcriptList")&&i->isVisible()){
            auto* parent=i->parentItem();while(parent && !parent->property("session").isValid())parent=parent->parentItem();
            if(parent&&parent->property("session").toString()==QStringLiteral("Aura")){found=i;break;}
        }}
        QJsonObject result{{QStringLiteral("scenario"),scenario},{QStringLiteral("before"),QJsonObject::fromVariantMap(window->property("researchBefore").toMap())},
            {QStringLiteral("originalObjectAlive"),!original->isNull()},
            {QStringLiteral("afterY"),found?found->property("contentY").toDouble():-1},{QStringLiteral("afterFollow"),found?found->property("followLatest").toBool():true}};
        QFile f(output+QStringLiteral("/lifecycle.json"));if(f.open(QIODevice::WriteOnly))f.write(QJsonDocument(result).toJson());
        window->grabWindow().save(output+QStringLiteral("/after.png"));
        Q_UNUSED(find)
    });
}
