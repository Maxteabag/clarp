#include "media/AudioCoordinator.h"
#include "media/RecordingSession.h"
#include <QCoreApplication>
#include <QFile>
#include <QJsonDocument>
#include <QSocketNotifier>
#include <cstdio>

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    QCoreApplication::setOrganizationName(QStringLiteral("ClarpTests"));
    QCoreApplication::setApplicationName(QStringLiteral("AudioCoordinationProbe"));
    clarp::AudioCoordinator coordinator;
    clarp::RecordingSession recording;
    auto output = [](const QJsonObject& object) {
        const auto bytes = QJsonDocument(object).toJson(QJsonDocument::Compact);
        std::puts(bytes.constData());
        std::fflush(stdout);
    };
    QObject::connect(&coordinator, &clarp::AudioCoordinator::ownershipChanged, &app, [&] {
        output({{QStringLiteral("kind"), QStringLiteral("owner")}, {QStringLiteral("owner"), coordinator.owner()}});
    });
    QObject::connect(&coordinator, &clarp::AudioCoordinator::clipReady, &app, [&](const QJsonObject& event) {
        output({{QStringLiteral("kind"), QStringLiteral("clip")}, {QStringLiteral("id"), event.value(QStringLiteral("clip_id"))}});
    });
    QObject::connect(&coordinator, &clarp::AudioCoordinator::commandReceived, &app, [&](const QString& action) {
        output({{QStringLiteral("kind"), QStringLiteral("command")}, {QStringLiteral("action"), action}});
    });
    QObject::connect(&coordinator, &clarp::AudioCoordinator::stateReceived, &app,
        [&](bool muted, bool playing, bool paused, bool available) {
            output({{QStringLiteral("kind"), QStringLiteral("state")}, {QStringLiteral("muted"), muted},
                {QStringLiteral("playing"), playing}, {QStringLiteral("paused"), paused}, {QStringLiteral("available"), available}});
        });
    QObject::connect(&coordinator, &clarp::AudioCoordinator::error, &app, [&](const QString& message) {
        output({{QStringLiteral("kind"), QStringLiteral("error")}, {QStringLiteral("message"), message}});
    });
    QFile input;
    if (!input.open(stdin, QIODevice::ReadOnly)) return 1;
    QSocketNotifier notifier(0, QSocketNotifier::Read);
    QObject::connect(&notifier, &QSocketNotifier::activated, &app, [&] {
        const auto line = input.readLine();
        if (line.isEmpty()) { app.quit(); return; }
        const auto request = QJsonDocument::fromJson(line).object();
        const auto op = request.value(QStringLiteral("op")).toString();
        const auto event = QJsonObject{{QStringLiteral("clip_id"), request.value(QStringLiteral("id"))},
            {QStringLiteral("url"), QStringLiteral("/clip.wav")}};
        if (op == QStringLiteral("submit")) {
            coordinator.submit(event);
        } else if (op == QStringLiteral("begin")) {
            output({{QStringLiteral("kind"), op}, {QStringLiteral("ok"), coordinator.begin(event)}});
        } else if (op == QStringLiteral("finish")) {
            coordinator.finish(event);
        } else if (op == QStringLiteral("control")) {
            coordinator.command(request.value(QStringLiteral("action")).toString(), request.value(QStringLiteral("muted")).toBool());
        } else if (op == QStringLiteral("publish")) {
            coordinator.publish(true, false, true);
        } else if (op == QStringLiteral("record")) {
            const bool acquired = recording.acquire(request.value(QStringLiteral("session")).toString());
            output({{QStringLiteral("kind"), op}, {QStringLiteral("ok"), acquired}, {QStringLiteral("target"), recording.target()}});
        } else if (op == QStringLiteral("stopRecording")) {
            output({{QStringLiteral("kind"), op}, {QStringLiteral("target"), recording.release()}});
        } else if (op == QStringLiteral("status")) {
            output({{QStringLiteral("kind"), op}, {QStringLiteral("owner"), coordinator.owner()}, {QStringLiteral("target"), recording.target()}});
        } else if (op == QStringLiteral("quit")) {
            app.quit();
        }
        output({{QStringLiteral("kind"), QStringLiteral("done")}, {QStringLiteral("op"), op}});
    });
    coordinator.configure(app.arguments().value(1, QStringLiteral("offline-test-host")), false);
    output({{QStringLiteral("kind"), QStringLiteral("ready")}, {QStringLiteral("owner"), coordinator.owner()}});
    return app.exec();
}
