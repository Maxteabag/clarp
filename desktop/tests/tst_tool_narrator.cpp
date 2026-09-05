#include "app/ToolNarrator.h"

#include <QCoreApplication>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSignalSpy>
#include <QTest>
#include <QThread>
#include <QTextStream>

using clarp::ToolNarrator;

namespace {
const QVariantMap Command{{QStringLiteral("name"), QStringLiteral("Bash")},
    {QStringLiteral("command"), QStringLiteral("cmake --build desktop/build/dev")}};

QJsonArray captures(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) return {};
    QJsonArray result;
    for (const auto& line : file.readAll().split('\n')) {
        if (!line.isEmpty()) result.append(QJsonDocument::fromJson(line).object());
    }
    return result;
}

int fakeCodex(const QStringList& args) {
    const QString capturePath = args.value(2), behavior = args.value(3);
    QFile input;
    if (!input.open(stdin, QIODevice::ReadOnly)) return 1;
    const QByteArray bytes = input.readAll();
    QFile capture(capturePath);
    if (!capture.open(QIODevice::WriteOnly | QIODevice::Append)) return 2;
    const QJsonObject recorded{{QStringLiteral("argv"), QJsonArray::fromStringList(args)},
        {QStringLiteral("prompt"), QString::fromUtf8(bytes)},
        {QStringLiteral("clarpToken"), qEnvironmentVariable("CLARP_TOKEN")},
        {QStringLiteral("session"), qEnvironmentVariable("CLAUDE_PWA_SESSION")}};
    capture.write(QJsonDocument(recorded).toJson(QJsonDocument::Compact) + '\n');
    capture.close();
    if (behavior == QStringLiteral("slow")) QThread::msleep(3'000);
    if (behavior == QStringLiteral("fail")) return 7;
    const QJsonArray requests = QJsonDocument::fromJson(bytes).object().value(QStringLiteral("requests")).toArray();
    QJsonArray answers;
    for (const auto& request : requests) {
        answers.append(QJsonObject{
            {QStringLiteral("id"), behavior == QStringLiteral("invalid") ? QStringLiteral("wrong-id")
                : request.toObject().value(QStringLiteral("id"))},
            {QStringLiteral("text"), QStringLiteral("Build the desktop preview.")}});
    }
    const qsizetype outputIndex = args.indexOf(QStringLiteral("--output-last-message"));
    QFile output(args.value(outputIndex + 1));
    if (!output.open(QIODevice::WriteOnly)) return 3;
    output.write(QJsonDocument(QJsonObject{{QStringLiteral("explanations"), answers}}).toJson());
    return 0;
}
} // namespace

class ToolNarratorTest : public QObject {
    Q_OBJECT
  private slots:
    void optInDeduplicatesBatchesAndPreservesCache();
    void disableCancelsAndRejectsLateReplies();
    void failureFallsBackWithoutRetryStorm_data();
    void failureFallsBackWithoutRetryStorm();
};

void ToolNarratorTest::optInDeduplicatesBatchesAndPreservesCache() {
    QTemporaryDir directory;
    const QString capture = directory.filePath(QStringLiteral("capture"));
    ToolNarrator narrator(nullptr, QCoreApplication::applicationFilePath(),
        {QStringLiteral("--fake-codex"), capture, QStringLiteral("ok")});
    QVERIFY(!narrator.enabled());
    narrator.request(Command);
    QTest::qWait(220);
    QVERIFY(captures(capture).isEmpty());
    narrator.setEnabled(true);
    narrator.request(Command);
    narrator.request(Command);
    auto updated = Command;
    updated.insert(QStringLiteral("status"), QStringLiteral("error"));
    updated.insert(QStringLiteral("result"), QStringLiteral("PRIVATE OUTPUT NEVER SENT"));
    narrator.request(updated);
    auto second = Command;
    second.insert(QStringLiteral("command"), QStringLiteral("curl -H 'Authorization: Bearer very-secret' https://example.test"));
    second.insert(QStringLiteral("input"), QVariantMap{
        {QStringLiteral("cmd"), QStringLiteral("cmake --build desktop/build/dev")},
        {QStringLiteral("result"), QStringLiteral("PRIVATE NESTED OUTPUT")},
        {QStringLiteral("token"), QStringLiteral("PRIVATE JSON TOKEN")}});
    narrator.request(second);
    QTRY_COMPARE_WITH_TIMEOUT(narrator.explanation(Command), QStringLiteral("Build the desktop preview."), 3'000);
    QCOMPARE(narrator.explanation(updated), narrator.explanation(Command));
    const auto calls = captures(capture);
    QCOMPARE(calls.size(), 1);
    const auto call = calls.first().toObject();
    const QString prompt = call.value(QStringLiteral("prompt")).toString();
    QCOMPARE(QJsonDocument::fromJson(prompt.toUtf8()).object().value(QStringLiteral("requests")).toArray().size(), 2);
    QVERIFY(!prompt.contains(QStringLiteral("PRIVATE OUTPUT")));
    QVERIFY(!prompt.contains(QStringLiteral("PRIVATE NESTED OUTPUT")));
    QVERIFY(!prompt.contains(QStringLiteral("PRIVATE JSON TOKEN")));
    QVERIFY(!prompt.contains(QStringLiteral("very-secret")));
    QVERIFY(prompt.contains(QStringLiteral("[redacted]")));
    const auto arguments = call.value(QStringLiteral("argv")).toArray().toVariantList();
    QVERIFY(arguments.contains(QStringLiteral("gpt-6-astra")));
    QVERIFY(arguments.contains(QStringLiteral("model_reasoning_effort=\"low\"")));
    QVERIFY(arguments.contains(QStringLiteral("--ignore-user-config")));
    QVERIFY(arguments.contains(QStringLiteral("read-only")));
    QVERIFY(!arguments.contains(QStringLiteral("--dangerously-bypass-approvals-and-sandbox")));
    QVERIFY(call.value(QStringLiteral("clarpToken")).toString().isEmpty());
    QVERIFY(call.value(QStringLiteral("session")).toString().isEmpty());
    narrator.setEnabled(false);
    QVERIFY(narrator.explanation(Command).isEmpty());
    narrator.setEnabled(true);
    narrator.request(Command);
    QCOMPARE(narrator.explanation(Command), QStringLiteral("Build the desktop preview."));
    QTest::qWait(220);
    QCOMPARE(captures(capture).size(), 1);
}

void ToolNarratorTest::disableCancelsAndRejectsLateReplies() {
    QTemporaryDir directory;
    const QString capture = directory.filePath(QStringLiteral("capture"));
    ToolNarrator narrator(nullptr, QCoreApplication::applicationFilePath(),
        {QStringLiteral("--fake-codex"), capture, QStringLiteral("slow")});
    narrator.setEnabled(true);
    narrator.request(Command);
    QTRY_COMPARE_WITH_TIMEOUT(captures(capture).size(), 1, 2'000);
    narrator.setEnabled(false);
    QVERIFY(narrator.explanation(Command).isEmpty());
    QVERIFY(narrator.status().startsWith(QStringLiteral("Off")));
    narrator.reset();
    QVERIFY(narrator.explanation(Command).isEmpty());
}

void ToolNarratorTest::failureFallsBackWithoutRetryStorm_data() {
    QTest::addColumn<QString>("behavior");
    QTest::addColumn<int>("timeout");
    QTest::newRow("failure") << QStringLiteral("fail") << 2'000;
    QTest::newRow("invalid response") << QStringLiteral("invalid") << 2'000;
    QTest::newRow("timeout") << QStringLiteral("slow") << 400;
}

void ToolNarratorTest::failureFallsBackWithoutRetryStorm() {
    QFETCH(QString, behavior);
    QFETCH(int, timeout);
    QTemporaryDir directory;
    const QString capture = directory.filePath(QStringLiteral("capture"));
    ToolNarrator narrator(nullptr, QCoreApplication::applicationFilePath(),
        {QStringLiteral("--fake-codex"), capture, behavior}, timeout);
    narrator.setEnabled(true);
    narrator.request(Command);
    QTRY_COMPARE_WITH_TIMEOUT(captures(capture).size(), 1, 2'000);
    QTRY_VERIFY_WITH_TIMEOUT(!narrator.status().startsWith(QStringLiteral("Translating")), 2'000);
    QVERIFY(!narrator.status().startsWith(QStringLiteral("Ready")));
    QVERIFY(narrator.explanation(Command).isEmpty());
    narrator.request(Command);
    QTest::qWait(220);
    QCOMPARE(captures(capture).size(), 1);
}

int main(int argc, char** argv) {
    QCoreApplication application(argc, argv);
    if (application.arguments().value(1) == QStringLiteral("--fake-codex"))
        return fakeCodex(application.arguments());
    // Explicit, opt-in smoke lane. Normal ctest never consumes model usage.
    if (application.arguments().contains(QStringLiteral("--live-smoke"))) {
        ToolNarrator narrator;
        narrator.setEnabled(true);
        QObject::connect(&narrator, &ToolNarrator::changed, &application, [&] {
            const QString text = narrator.explanation(Command);
            if (!text.isEmpty()) {
                QTextStream(stdout) << text << Qt::endl;
                application.exit(0);
            } else if (!narrator.status().startsWith(QStringLiteral("Translating"))) {
                QTextStream(stderr) << narrator.status() << Qt::endl;
                application.exit(1);
            }
        });
        QTimer::singleShot(0, &application, [&] { narrator.request(Command); });
        return application.exec();
    }
    ToolNarratorTest test;
    return QTest::qExec(&test, argc, argv);
}

#include "tst_tool_narrator.moc"
