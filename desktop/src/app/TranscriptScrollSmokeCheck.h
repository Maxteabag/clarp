#pragma once
#include "app/AppController.h"
#include "models/ConversationModel.h"
#include <QCoreApplication>
#include <QGuiApplication>
#include <QJsonArray>
#include <QJsonObject>
#include <QQuickItem>
#include <QQuickWindow>
#include <QTimer>
#include <QWheelEvent>
#include <functional>

// Drives the real transcript (ConversationModel -> presentation proxy ->
// TranscriptList -> MessageDelegate) offscreen: streams into a live row while
// following, wheels the reader up, keeps streaming and refreshing, and checks
// that the reader is never pulled away. Reproduces scroll regressions the
// stub-delegate QML fixture cannot see.
namespace clarp::transcriptscroll {
inline QJsonObject turn(int i, bool live = false) {
    const bool user = i % 2 == 0;
    QJsonObject row{{QStringLiteral("id"), QStringLiteral("fixture-%1").arg(i)},
                    {QStringLiteral("role"), user ? QStringLiteral("user") : QStringLiteral("assistant")},
                    {QStringLiteral("timestamp"), QStringLiteral("2026-09-15T08:%1:00Z").arg(i % 60, 2, 10, QLatin1Char('0'))},
                    {QStringLiteral("revision"), i + 1}};
    if (user) {
        row.insert(QStringLiteral("text"), QStringLiteral("Question %1: what changed in the transcript scroll code?").arg(i));
    } else {
        row.insert(QStringLiteral("text"), live ? QStringLiteral("Streaming answer") :
            QStringLiteral("Answer %1. The list follows new rows while the reader is at the end and stays put otherwise.\n\nSecond paragraph with a little more text so rows differ in height.").arg(i));
        if (live) row.insert(QStringLiteral("kind"), QStringLiteral("live"));
        row.insert(QStringLiteral("tools"), QJsonArray{QJsonObject{{QStringLiteral("name"), QStringLiteral("Read")},
            {QStringLiteral("summary"), QStringLiteral("desktop/qml/components/TranscriptList.qml")}}});
    }
    return row;
}
inline QJsonArray turns(int count, bool liveTail) {
    QJsonArray rows;
    for (int i = 0; i < count; ++i) rows.append(turn(i, liveTail && i == count - 1));
    return rows;
}
inline QJsonObject groupedTurn(int i) {
    QJsonObject row = turn(i, false);
    if (i % 2 == 1) {
        row.insert(QStringLiteral("text"), QString{});
        row.insert(QStringLiteral("activity_count"), 3);
    }
    return row;
}
inline QJsonArray groupedTurns(int count) {
    QJsonArray rows;
    for (int i = 0; i < count; ++i) rows.append(groupedTurn(i));
    return rows;
}
inline QJsonObject activityEvent(const QString& status, const QString& tool, const QString& path) {
    return {{QStringLiteral("activity_status"), status}, {QStringLiteral("activity_action"), QStringLiteral("tool")},
            {QStringLiteral("tool"), tool}, {QStringLiteral("file_path"), path},
            {QStringLiteral("activity_summary"), QStringLiteral("%1 %2").arg(tool, path)}};
}
inline void wheel(QQuickWindow* window, QQuickItem* item, int notches) {
    const QPointF pos = item->mapToScene(QPointF(item->width() / 2, item->height() / 2));
    QWheelEvent event(pos, window->mapToGlobal(pos.toPoint()), QPoint(), QPoint(0, 120 * notches),
                      Qt::NoButton, Qt::NoModifier, Qt::NoScrollPhase, false);
    QCoreApplication::sendEvent(window, &event);
}
}

inline void startTranscriptScrollSmokeCheck(QGuiApplication& application, QQuickWindow* window,
                                            clarp::AppController* controller) {
    using namespace clarp::transcriptscroll;
    if (QGuiApplication::platformName() != QStringLiteral("offscreen") || window == nullptr || controller == nullptr) {
        application.exit(EXIT_FAILURE); return;
    }
    auto* timer = new QTimer(&application);
    timer->setInterval(220);
    QObject::connect(timer, &QTimer::timeout, &application,
        [&application, window, controller, timer, step = 0, readerY = 0.0, streamed = 0]() mutable {
        const auto require = [&application, timer, step](bool ok, const char* what) {
            if (!ok) { qCritical("Transcript scroll check failed at step %d: %s", step, what); timer->stop(); application.exit(EXIT_FAILURE); }
            return ok;
        };
        const QString session = controller->selectedSession().isEmpty()
            ? controller->panes()->activeSession() : controller->selectedSession();
        auto* model = controller->conversationForSession(session);
        if (!require(model != nullptr && !session.isEmpty(), "no fixture session")) return;
        QQuickItem* transcript = nullptr;
        QList<QQuickItem*> pending{window->contentItem()};
        while (!pending.isEmpty() && transcript == nullptr) {
            auto* item = pending.takeLast();
            pending.append(item->childItems());
            if (item->objectName() == QStringLiteral("transcriptList") && item->isVisible() && item->width() > 0) transcript = item;
        }
        if (!require(transcript != nullptr, "no visible transcript")) return;
        const qreal contentY = transcript->property("contentY").toReal();
        const qreal distance = transcript->property("distanceFromBottom").toReal();
        const bool atEnd = transcript->property("atYEnd").toBool();
        const bool following = transcript->property("followLatest").toBool();
        qInfo("transcript-scroll step=%d contentY=%.1f contentHeight=%.1f distance=%.1f atYEnd=%d follow=%d interacting=%d count=%d",
              step, contentY, transcript->property("contentHeight").toReal(), distance, atEnd, following,
              transcript->property("userInteracting").toBool(), transcript->property("count").toInt());
        const auto stream = [model, &streamed] {
            ++streamed;
            QJsonObject live = turn(39, true);
            QString text = QStringLiteral("Streaming answer");
            for (int i = 0; i < streamed; ++i) text += QStringLiteral(" and more streamed words arrive here (%1)").arg(i);
            live.insert(QStringLiteral("text"), text);
            live.insert(QStringLiteral("revision"), 40 + streamed);
            model->applyLog({{QStringLiteral("conversation_id"), model->conversationId()},
                {QStringLiteral("turns"), QJsonArray{live}}, {QStringLiteral("latest_revision"), 40 + streamed}},
                clarp::ConversationModel::LoadKind::Delta);
        };
        switch (step++) {
        case 0:
            controller->setToolsVisible(true);
            model->applyLog({{QStringLiteral("conversation_id"), QStringLiteral("transcript-scroll")},
                {QStringLiteral("turns"), turns(40, true)}, {QStringLiteral("latest_revision"), 40}},
                clarp::ConversationModel::LoadKind::Tail);
            break;
        case 1:
            if (!require(atEnd && following, "initial load must end at the bottom")) return;
            stream();
            break;
        case 2:
            if (!require(atEnd && following && distance < 2, "streaming while following must keep the end visible")) return;
            stream();
            break;
        case 3:
            if (!require(atEnd && following && distance < 2, "second stream while following must keep the end visible")) return;
            wheel(window, transcript, 5);
            break;
        case 4:
            if (!require(!following && distance > 200, "wheel up must pause following and move the reader")) return;
            readerY = contentY;
            stream();
            break;
        case 5:
            if (!require(std::abs(contentY - readerY) < 2 && !following, "streaming must not move a paused reader")) return;
            model->applyLog({{QStringLiteral("conversation_id"), model->conversationId()},
                {QStringLiteral("turns"), QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-final")},
                    {QStringLiteral("role"), QStringLiteral("assistant")}, {QStringLiteral("text"), QStringLiteral("Final answer replaces the live row.")},
                    {QStringLiteral("timestamp"), QStringLiteral("2026-09-15T08:41:00Z")}, {QStringLiteral("revision"), 90}}}},
                {QStringLiteral("latest_revision"), 90}}, clarp::ConversationModel::LoadKind::Delta);
            break;
        case 6:
            if (!require(std::abs(contentY - readerY) < 2 && !following, "a new final row must not move a paused reader")) return;
            {
                QJsonArray refreshed = turns(39, false);
                refreshed.append(QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-final")},
                    {QStringLiteral("role"), QStringLiteral("assistant")}, {QStringLiteral("text"), QStringLiteral("Final answer replaces the live row.")},
                    {QStringLiteral("timestamp"), QStringLiteral("2026-09-15T08:41:00Z")}, {QStringLiteral("revision"), 90}});
                model->applyLog({{QStringLiteral("conversation_id"), QStringLiteral("transcript-scroll")},
                    {QStringLiteral("turns"), refreshed}, {QStringLiteral("latest_revision"), 90}},
                    clarp::ConversationModel::LoadKind::Tail);
            }
            break;
        case 7:
            if (!require(std::abs(contentY - readerY) < 4 && !following, "a refresh must restore the paused reader's position")) return;
            QMetaObject::invokeMethod(transcript, "scrollToLatest");
            break;
        case 8:
            if (!require(atEnd && following && distance < 2, "jump to latest must reach the bottom and resume following")) return;
            wheel(window, transcript, 3);
            break;
        case 9:
            if (!require(!following, "second wheel up must pause following")) return;
            wheel(window, transcript, -3);
            break;
        case 10:
            if (!require(atEnd && following, "wheeling back to the end must resume following")) return;
            stream();
            break;
        case 11:
            if (!require(atEnd && following && distance < 2, "streaming after resume must keep the end visible")) return;
            // Phase B: the user's real configuration. Grouped activity, replies
            // shown when ready and tool narration make the presentation proxy
            // refresh its groups on every streamed change.
            controller->setActivityDisplayMode(2);
            controller->setShowWhenReady(true);
            controller->toolNarrator()->setEnabled(true);
            // Load into a fresh second agent rather than re-tailing this model:
            // a second Tail load into a populated model leaks QML delegate
            // objects at exit under LeakSanitizer on main today, unrelated to
            // the scroll behaviour this lane guards.
            {
                const auto agent = [](const QString& id, const QString& persona) {
                    return QJsonObject{{QStringLiteral("agent_id"), id}, {QStringLiteral("session"), id},
                        {QStringLiteral("persona"), persona}, {QStringLiteral("backend"), QStringLiteral("local")},
                        {QStringLiteral("latest_state"), QStringLiteral("idle")}};
                };
                controller->agents()->applySnapshot({{QStringLiteral("agents"), QJsonArray{
                    agent(session, QStringLiteral("OPUS")), agent(QStringLiteral("grouped-fixture"), QStringLiteral("GROUPED"))}}});
                controller->selectSession(QStringLiteral("grouped-fixture"));
                controller->conversationForSession(QStringLiteral("grouped-fixture"))->applyLog(
                    {{QStringLiteral("conversation_id"), QStringLiteral("grouped-fixture")},
                     {QStringLiteral("turns"), groupedTurns(41)}, {QStringLiteral("latest_revision"), 41}},
                    clarp::ConversationModel::LoadKind::Tail);
            }
            break;
        case 12:
            if (!require(atEnd && following, "grouped load must end at the bottom")) return;
            model->showTransientThinking(QStringLiteral("OPUS"));
            model->applyActivityEvent(activityEvent(QStringLiteral("running"), QStringLiteral("Read"), QStringLiteral("a.swift")));
            break;
        case 13:
            if (!require(atEnd && following && distance < 2, "activity rows while following must keep the end visible")) return;
            wheel(window, transcript, 5);
            break;
        case 14:
            if (!require(!following && distance > 200, "wheel up during activity must pause following")) return;
            readerY = contentY;
            model->applyActivityEvent(activityEvent(QStringLiteral("ok"), QStringLiteral("Read"), QStringLiteral("a.swift")));
            model->applyActivityEvent(activityEvent(QStringLiteral("running"), QStringLiteral("Edit"), QStringLiteral("b.swift")));
            streamed = 0;
            stream();
            break;
        case 15:
            if (!require(std::abs(contentY - readerY) < 2 && !following, "activity updates must not move a paused reader")) return;
            model->applyActivityEvent(activityEvent(QStringLiteral("ok"), QStringLiteral("Edit"), QStringLiteral("b.swift")));
            model->applyActivityEvent(activityEvent(QStringLiteral("running"), QStringLiteral("Bash"), QStringLiteral("ctest")));
            stream();
            break;
        case 16:
            if (!require(std::abs(contentY - readerY) < 2 && !following, "streamed live text must not move a paused reader")) return;
            model->applyLog({{QStringLiteral("conversation_id"), model->conversationId()},
                {QStringLiteral("turns"), QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-final-b")},
                    {QStringLiteral("role"), QStringLiteral("assistant")}, {QStringLiteral("text"), QStringLiteral("Final grouped answer.")},
                    {QStringLiteral("timestamp"), QStringLiteral("2026-09-15T08:45:00Z")}, {QStringLiteral("revision"), 95}}}},
                {QStringLiteral("latest_revision"), 95}}, clarp::ConversationModel::LoadKind::Delta);
            break;
        case 17:
            if (!require(std::abs(contentY - readerY) < 2 && !following, "the final grouped answer must not move a paused reader")) return;
            model->clearActivity();
            break;
        case 18:
            if (!require(std::abs(contentY - readerY) < 2 && !following, "clearing activity must not move a paused reader")) return;
            QMetaObject::invokeMethod(transcript, "scrollToLatest");
            break;
        default:
            if (!require(atEnd && following && distance < 2, "jump to latest after grouped activity must reach the bottom")) return;
            window->setProperty("transcriptScrollVerified", true);
            timer->stop();
        }
    });
    QTimer::singleShot(2'050, timer, [timer] { timer->start(); });
}

// Records the transcript state of every visible pane at each frame about to be
// rendered while the pane switches agents and focus moves between panes. A
// single frame at the wrong position is the "brief unwanted movement".
inline void startTranscriptSwitchSmokeCheck(QGuiApplication& application, QQuickWindow* window,
                                            clarp::AppController* controller) {
    using namespace clarp::transcriptscroll;
    if (QGuiApplication::platformName() != QStringLiteral("offscreen") || window == nullptr || controller == nullptr) {
        application.exit(EXIT_FAILURE); return;
    }
    struct Frame { int step; QString session; qreal contentY; qreal height; bool atEnd; bool follow; int count; qreal sceneY; };
    auto frames = std::make_shared<QList<Frame>>();
    auto step = std::make_shared<int>(-1);
    const auto transcripts = [window] {
        QList<QQuickItem*> pending{window->contentItem()};
        QList<std::pair<QString, QQuickItem*>> result;
        while (!pending.isEmpty()) {
            auto* item = pending.takeLast(); pending.append(item->childItems());
            if (item->objectName() != QStringLiteral("transcriptList") || !item->isVisible() || item->width() <= 0) continue;
            auto* parent = item->parentItem();
            while (parent && !parent->property("session").isValid()) parent = parent->parentItem();
            result.append({parent ? parent->property("session").toString() : QString{}, item});
        }
        return result;
    };
    QObject::connect(window, &QQuickWindow::afterAnimating, &application, [frames, step, transcripts] {
        if (*step < 0) return;
        for (const auto& [session, item] : transcripts())
            frames->append(Frame{.step = *step, .session = session, .contentY = item->property("contentY").toReal(),
                                 .height = item->height(), .atEnd = item->property("atYEnd").toBool(),
                                 .follow = item->property("followLatest").toBool(), .count = item->property("count").toInt(),
                                 .sceneY = item->mapToScene(QPointF(0, 0)).y()});
    });
    auto* timer = new QTimer(&application);
    timer->setInterval(300);
    QObject::connect(timer, &QTimer::timeout, &application, [&application, window, controller, timer, frames, step, transcripts]() mutable {
        const auto require = [&application, timer, step](bool ok, const char* what) {
            if (!ok) { qCritical("Transcript switch check failed at step %d: %s", *step, what); timer->stop(); application.exit(EXIT_FAILURE); }
            return ok;
        };
        const auto view = [transcripts](const QString& session) -> QQuickItem* {
            for (const auto& [name, item] : transcripts()) if (name == session) return item;
            return nullptr;
        };
        const auto report = [frames](int forStep) {
            QString last;
            int total = 0;
            for (const auto& f : *frames) {
                if (f.step != forStep) continue;
                ++total;
                const QString line = QStringLiteral("%1 y=%2 h=%3 end=%4 follow=%5 n=%6 sceneY=%7")
                    .arg(f.session).arg(f.contentY, 0, 'f', 1).arg(f.height, 0, 'f', 1).arg(f.atEnd).arg(f.follow).arg(f.count).arg(f.sceneY, 0, 'f', 1);
                if (line != last) qInfo("switch-frames step=%d %s", forStep, qPrintable(line));
                last = line;
            }
            return total;
        };
        // Every frame painted for `session` during `forStep` must show the end.
        const auto everyFrameAtEnd = [frames](int forStep, const QString& session) {
            bool any = false;
            for (const auto& f : *frames) {
                if (f.step != forStep || f.session != session) continue;
                any = true;
                if (!f.atEnd) return false;
            }
            return any;
        };
        // Nothing about `session` may move on screen during `forStep`.
        const auto frozen = [frames](int forStep, const QString& session) {
            const Frame* first = nullptr;
            for (const auto& f : *frames) {
                if (f.step != forStep || f.session != session) continue;
                if (first == nullptr) { first = &f; continue; }
                if (std::abs(f.contentY - first->contentY) > 0.5 || std::abs(f.height - first->height) > 0.5
                    || std::abs(f.sceneY - first->sceneY) > 0.5) return false;
            }
            return first != nullptr;
        };
        const auto load = [controller](const QString& name, int count) {
            QJsonArray rows;
            for (int i = 0; i < count; ++i) rows.append(turn(i, false));
            controller->conversationForSession(name)->applyLog({{QStringLiteral("conversation_id"), name},
                {QStringLiteral("turns"), rows}, {QStringLiteral("latest_revision"), count}}, clarp::ConversationModel::LoadKind::Replace);
        };
        ++*step;
        switch (*step) {
        case 0: {
            controller->setMuted(true);
            QJsonArray agents;
            for (const QString& name : {QStringLiteral("Aura"), QStringLiteral("Boreal"), QStringLiteral("Cedar")})
                agents.append(QJsonObject{{QStringLiteral("session"), name}, {QStringLiteral("agent_id"), name}, {QStringLiteral("persona"), name},
                    {QStringLiteral("backend"), QStringLiteral("local")}, {QStringLiteral("latest_state"), QStringLiteral("idle")}});
            controller->agents()->applySnapshot({{QStringLiteral("agents"), agents}});
            controller->selectSession(QStringLiteral("Aura"));
            controller->panes()->setActiveSession(QStringLiteral("Aura"));
            load(QStringLiteral("Aura"), 60); load(QStringLiteral("Boreal"), 70); load(QStringLiteral("Cedar"), 50);
            controller->clearError();
            break;
        }
        case 1: {
            auto* aura = view(QStringLiteral("Aura"));
            if (!require(aura != nullptr && aura->property("atYEnd").toBool(), "Aura must open at the end")) return;
            wheel(window, aura, 6);
            break;
        }
        case 2: {
            auto* aura = view(QStringLiteral("Aura"));
            if (!require(aura != nullptr && !aura->property("followLatest").toBool(), "wheel must pause Aura")) return;
            controller->selectSession(QStringLiteral("Boreal"));
            break;
        }
        case 3: {
            const int painted = report(2);
            auto* boreal = view(QStringLiteral("Boreal"));
            if (!require(boreal != nullptr && painted > 0, "Boreal must be shown after the switch")) return;
            if (!require(everyFrameAtEnd(2, QStringLiteral("Boreal")), "switching agents must never paint the new chat away from its end")) return;
            controller->selectSession(QStringLiteral("Aura"));
            break;
        }
        case 4: {
            report(3);
            auto* aura = view(QStringLiteral("Aura"));
            if (!require(aura != nullptr, "Aura must be shown after switching back")) return;
            if (!require(everyFrameAtEnd(3, QStringLiteral("Aura")), "switching back must never paint the chat away from its end")) return;
            controller->panes()->splitActive(QStringLiteral("vertical"), QStringLiteral("Cedar"));
            break;
        }
        case 5: {
            report(4);
            auto* cedar = view(QStringLiteral("Cedar"));
            auto* aura = view(QStringLiteral("Aura"));
            if (!require(cedar != nullptr && aura != nullptr, "both panes must be visible after the split")) return;
            if (!require(everyFrameAtEnd(4, QStringLiteral("Cedar")), "a new pane must open at its end without an off-end frame")) return;
            if (!require(everyFrameAtEnd(4, QStringLiteral("Aura")), "the surviving pane must stay at its end through the split")) return;
            wheel(window, aura, 4);
            break;
        }
        case 6: {
            report(5);
            // Focus moves to the Aura pane (the split made Cedar active).
            QQuickItem* aura = view(QStringLiteral("Aura"));
            auto* leaf = aura ? aura->parentItem() : nullptr;
            while (leaf && !leaf->property("node").isValid()) leaf = leaf->parentItem();
            if (!require(leaf != nullptr, "Aura pane leaf must exist")) return;
            controller->panes()->focusPane(leaf->property("node").toMap().value(QStringLiteral("id")).toString());
            break;
        }
        case 7: {
            report(6);
            if (!require(frozen(6, QStringLiteral("Aura")) && frozen(6, QStringLiteral("Cedar")), "pane focus must not move either transcript")) return;
            QQuickItem* cedar = view(QStringLiteral("Cedar"));
            auto* leaf = cedar ? cedar->parentItem() : nullptr;
            while (leaf && !leaf->property("node").isValid()) leaf = leaf->parentItem();
            if (!require(leaf != nullptr, "Cedar pane leaf must exist")) return;
            controller->panes()->focusPane(leaf->property("node").toMap().value(QStringLiteral("id")).toString());
            break;
        }
        default:
            report(7);
            if (!require(frozen(7, QStringLiteral("Aura")) && frozen(7, QStringLiteral("Cedar")), "pane focus must not move either transcript (return)")) return;
            if (!require(!view(QStringLiteral("Aura"))->property("followLatest").toBool(), "the paused pane must stay paused through focus changes")) return;
            window->setProperty("transcriptSwitchVerified", true);
            timer->stop();
        }
    });
    QTimer::singleShot(2'050, timer, [timer] { timer->start(); });
}
