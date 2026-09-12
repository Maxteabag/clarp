#pragma once
#include "app/AppController.h"
#include "network/SseClient.h"
#include <QGuiApplication>
#include <QDebug>
#include <QQuickWindow>
#include <QQuickItem>
#include <QSaveFile>
#include <QJsonDocument>
#include <QJsonArray>
#include <memory>
#include <QTimer>
#include <cmath>

// Real window/model/SSE-consumer path, with deterministic voice events and no
// provider, microphone, playback or visible desktop interaction.
inline void startVoiceViewportSmokeCheck(QGuiApplication& application, QQuickWindow* window,
                                         clarp::AppController* controller) {
    if (QGuiApplication::platformName() != QStringLiteral("offscreen") || !window || !controller) {
        application.exit(EXIT_FAILURE); return;
    }
    struct State { int step = 0; int baselineAttempts = 0; int stableSamples = 0; bool passed = true; QString anchor; qreal anchorY = 0; qreal height = 0; QJsonArray observations; };
    auto state = std::make_shared<State>();
    auto* timer = new QTimer(&application); timer->setInterval(150);
    QObject::connect(timer, &QTimer::timeout, &application, [&application, window, controller, timer, state] {
        const QString current = QStringLiteral("voice-current"), other = QStringLiteral("voice-other");
        const auto find = [window](const QString& name) -> QQuickItem* {
            QList<QQuickItem*> pending{window->contentItem()};
            while (!pending.isEmpty()) {
                auto* item = pending.takeLast(); pending.append(item->childItems());
                if (item->isVisible() && (item->objectName() == name || item->property("messageId").toString() == name)) return item;
            }
            return nullptr;
        };
        auto* transcript = find(QStringLiteral("transcriptList"));
        const auto capture = [&](const QString& label) {
            auto* anchor = find(state->anchor);
            const qreal y = anchor ? anchor->mapToScene(QPointF{}).y() : -99999;
            const qreal height = transcript ? transcript->height() : 0;
            state->observations.append(QJsonObject{{"stage", label}, {"anchor_id", state->anchor},
                {"anchor_y", y}, {"height", height}, {"global_error", controller->errorMessage()},
                {"current_error", controller->conversationForSession(current)->error()},
                {"voice_error", controller->conversationForSession(current)->property("voiceError").toString()}});
            return anchor && std::abs(y - state->anchorY) < 2 && std::abs(height - state->height) < 2;
        };
        const auto inject = [&](const QString& session) {
            auto* sse = controller->findChild<clarp::SseClient*>();
            const QJsonObject event{{"type", "tts-error"}, {"session", session},
                {"message", "Voice synthesis failed."}, {"error", "fixture provider refused synthesis"}};
            return sse && QMetaObject::invokeMethod(sse, "eventReceived", Qt::DirectConnection, Q_ARG(QJsonObject, event));
        };
        switch (state->step++) {
        case 0: {
            controller->agents()->applySnapshot({{"agents", QJsonArray{
                QJsonObject{{"session", current}, {"persona", "Current"}, {"backend", "local"}, {"latest_state", "idle"}},
                QJsonObject{{"session", other}, {"persona", "Other"}, {"backend", "local"}, {"latest_state", "idle"}}}}});
            controller->selectSession(current);
            controller->panes()->setActiveSession(current);
            (void)controller->conversationForSession(other);
            QJsonArray turns;
            for (int i = 0; i < 80; ++i) turns.append(QJsonObject{{"id", QStringLiteral("voice-row-%1").arg(i)},
                {"role", "assistant"}, {"text", QStringLiteral("Retained fixture message %1. Read this conversation without moving the viewport.").arg(i)},
                {"timestamp", "2026-09-11T12:00:00Z"}});
            controller->conversationForSession(current)->applyLog({{"conversation_id", "voice-fixture"}, {"turns", turns}}, clarp::ConversationModel::LoadKind::Replace);
            break;
        }
        case 1:
            if (!transcript) { application.exit(EXIT_FAILURE); timer->stop(); return; }
            controller->clearError(); controller->conversationForSession(current)->setError({});
            QMetaObject::invokeMethod(transcript, "pauseFollowing");
            transcript->setProperty("contentY", transcript->property("originY").toReal() + 900);
            break;
        case 2: {
            // ListView creates and measures delegates lazily. Establish a stable
            // reader anchor before injecting events; a fixed 150ms delay is not
            // enough on sanitizer/CI builds.
            ++state->baselineAttempts;
            if (transcript && state->anchor.isEmpty()) {
                const qreal top = transcript->mapToScene(QPointF{}).y();
                for (int i = 0; i < 80; ++i) {
                    auto* row = find(QStringLiteral("voice-row-%1").arg(i));
                    if (row && row->mapToScene(QPointF{}).y() > top + 120 && row->mapToScene(QPointF{}).y() < top + transcript->height() - 100) {
                        state->anchor = row->property("messageId").toString(); break;
                    }
                }
            }
            auto* anchor = find(state->anchor);
            const qreal y = anchor ? anchor->mapToScene(QPointF{}).y() : -99999;
            const qreal height = transcript ? transcript->height() : 0;
            if (anchor && height > 0 && std::abs(y - state->anchorY) < 0.5 && std::abs(height - state->height) < 0.5)
                ++state->stableSamples;
            else state->stableSamples = 0;
            state->anchorY = y; state->height = height;
            if (state->stableSamples < 3) {
                if (state->baselineAttempts >= 25) {
                    qCritical("Reader baseline never settled before voice injection");
                    application.exit(EXIT_FAILURE); timer->stop(); return;
                }
                --state->step; break;
            }
            capture("before");
            state->passed = inject(other) && state->passed;
            break;
        }
        case 3:
            state->passed = capture("after-other-session-voice-error") && controller->errorMessage().isEmpty() && controller->conversationForSession(current)->voiceError().isEmpty() && !controller->conversationForSession(other)->voiceError().isEmpty() && state->passed;
            controller->clearError(); controller->conversationForSession(current)->setError({});
            break;
        case 4:
            state->passed = inject(current) && state->passed;
            break;
        case 5:
            state->passed = capture("after-current-session-voice-error") && !controller->conversationForSession(current)->voiceError().isEmpty() && state->passed;
            controller->conversationForSession(current)->applyLog({{"conversation_id", "voice-fixture"}, {"turns", QJsonArray{
                QJsonObject{{"id", "voice-stream"}, {"role", "assistant"}, {"kind", "live"}, {"text", "New spoken reply is streaming."}, {"timestamp", "2026-09-11T12:05:00Z"}}}}}, clarp::ConversationModel::LoadKind::Delta);
            break;
        default: {
            state->passed = capture("after-streamed-reply") && state->passed;
            QSaveFile output(qEnvironmentVariable("CLARP_VOICE_VIEWPORT_TRACE"));
            if (output.open(QIODevice::WriteOnly)) { output.write(QJsonDocument(QJsonObject{{"passed", state->passed}, {"observations", state->observations}}).toJson()); output.commit(); }
            window->setProperty("voiceViewportVerified", state->passed); timer->stop();
            if (!state->passed) { qCritical().noquote() << "Voice event changed the reader viewport or leaked another session error:" << QJsonDocument(state->observations).toJson(QJsonDocument::Compact); application.exit(EXIT_FAILURE); }
            break;
        }
        }
    });
    QTimer::singleShot(2200, timer, [timer] { timer->start(); });
}
