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
    void scriptContextIsOptInBoundedAndInvalidatesCache();
    void detailLevelsChangeInstructionsAndDiscardPreviousTranslations();
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
    const auto requests = QJsonDocument::fromJson(prompt.toUtf8()).object().value(QStringLiteral("requests")).toArray();
    QCOMPARE(requests.first().toObject().value(QStringLiteral("id")).toString(), QStringLiteral("1"));
    QCOMPARE(requests.last().toObject().value(QStringLiteral("id")).toString(), QStringLiteral("2"));
    QCOMPARE(QJsonDocument::fromJson(prompt.toUtf8()).object().value(QStringLiteral("requests")).toArray().size(), 2);
    QVERIFY(!prompt.contains(QStringLiteral("PRIVATE OUTPUT")));
    QVERIFY(!prompt.contains(QStringLiteral("PRIVATE NESTED OUTPUT")));
    QVERIFY(!prompt.contains(QStringLiteral("PRIVATE JSON TOKEN")));
    QVERIFY(!prompt.contains(QStringLiteral("very-secret")));
    QVERIFY(prompt.contains(QStringLiteral("[redacted]")));
    const auto arguments = call.value(QStringLiteral("argv")).toArray().toVariantList();
    QVERIFY(arguments.contains(QStringLiteral("gpt-5.3-codex-spark")));
    QVERIFY(arguments.contains(QStringLiteral("model_reasoning_effort=\"low\"")));
    QVERIFY(arguments.contains(QStringLiteral("--ignore-user-config")));
    QVERIFY(arguments.contains(QStringLiteral("read-only")));
    QVERIFY(!arguments.contains(QStringLiteral("--dangerously-bypass-approvals-and-sandbox")));
    QVERIFY(call.value(QStringLiteral("clarpToken")).toString().isEmpty());
    QVERIFY(call.value(QStringLiteral("session")).toString().isEmpty());
    const auto diagnostics = captures(narrator.diagnosticsPath());
    QVERIFY(!diagnostics.isEmpty());
    const QByteArray diagnosticText = QJsonDocument(diagnostics).toJson();
    QVERIFY(diagnosticText.contains("queue_wait_ms"));
    QVERIFY(diagnosticText.contains("process_started"));
    QVERIFY(diagnosticText.contains("batch_completed"));
    QVERIFY(!diagnosticText.contains("very-secret"));
    QVERIFY(!diagnosticText.contains("cmake"));
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

void ToolNarratorTest::detailLevelsChangeInstructionsAndDiscardPreviousTranslations() {
    QTemporaryDir directory;
    const QString capture = directory.filePath(QStringLiteral("capture"));
    ToolNarrator narrator(nullptr, QCoreApplication::applicationFilePath(),
        {QStringLiteral("--fake-codex"), capture, QStringLiteral("ok")});
    QCOMPARE(narrator.detailLevel(), 0);
    QCOMPARE(narrator.detailLevels().size(), 5);
    QSet<QString> prompts;
    for (int level = 1; level <= 4; ++level) {
        narrator.setDetailLevel(level);
        QCOMPARE(narrator.detailLevel(), level);
        QVERIFY(narrator.enabled());
        QVERIFY(narrator.explanation(Command).isEmpty());
        narrator.request(Command);
        QTRY_VERIFY_WITH_TIMEOUT(!narrator.explanation(Command).isEmpty(), 3'000);
        const auto call = captures(capture).last().toObject();
        const auto args = call.value(QStringLiteral("argv")).toArray();
        QString instructionsPath;
        for (const auto& arg : args) {
            const QString value = arg.toString();
            if (value.startsWith(QStringLiteral("model_instructions_file=\"")))
                instructionsPath = value.mid(25).chopped(1);
        }
        QFile instructions(instructionsPath);
        QVERIFY(instructions.open(QIODevice::ReadOnly));
        prompts.insert(QString::fromUtf8(instructions.readAll()));
    }
    QCOMPARE(prompts.size(), 4);
    narrator.setDetailLevel(0);
    QVERIFY(!narrator.enabled());
    narrator.request(Command);
    QTest::qWait(220);
    QCOMPARE(captures(capture).size(), 4);
    narrator.setEnabled(true);
    QCOMPARE(narrator.detailLevel(), 4);
}

void ToolNarratorTest::scriptContextIsOptInBoundedAndInvalidatesCache() {
    QTemporaryDir directory;
    const QString capture = directory.filePath(QStringLiteral("capture"));
    const QString scriptPath = directory.filePath(QStringLiteral("meat_search.js"));
    QFile script(scriptPath);
    QVERIFY(script.open(QIODevice::WriteOnly));
    script.write("// Search the grocery catalogue for beef and compare prices.\nconst token = 'private-fixture-token';\n");
    script.close();
    const QVariantMap activity{{QStringLiteral("name"), QStringLiteral("Bash")},
        {QStringLiteral("command"), QStringLiteral("node meat_search.js")}};
    ToolNarrator narrator(nullptr, QCoreApplication::applicationFilePath(),
        {QStringLiteral("--fake-codex"), capture, QStringLiteral("ok")});
    narrator.setEnabled(true);
    narrator.request(activity, directory.path(), false);
    QTRY_VERIFY_WITH_TIMEOUT(!narrator.explanation(activity, directory.path(), false).isEmpty(), 3'000);
    QVERIFY(!captures(capture).first().toObject().value(QStringLiteral("prompt")).toString().contains(QStringLiteral("grocery catalogue")));
    narrator.request(activity, directory.path(), true);
    QTRY_VERIFY_WITH_TIMEOUT(!narrator.explanation(activity, directory.path(), true).isEmpty(), 3'000);
    const auto calls = captures(capture);
    QCOMPARE(calls.size(), 2);
    const QString prompt = calls.last().toObject().value(QStringLiteral("prompt")).toString();
    QVERIFY(prompt.contains(QStringLiteral("grocery catalogue")));
    QVERIFY(!prompt.contains(QStringLiteral("private-fixture-token")));
    QVERIFY(script.open(QIODevice::WriteOnly | QIODevice::Truncate));
    script.write("// Compare the delivery fees instead.\n");
    script.close();
    QVERIFY(narrator.explanation(activity, directory.path(), true).isEmpty());
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
    if (application.arguments().contains(QStringLiteral("--live-script-smoke"))) {
        QTemporaryDir directory;
        QFile script(directory.filePath(QStringLiteral("meat_search.js")));
        if (!script.open(QIODevice::WriteOnly)) return 2;
        script.write("const items = await fetch('https://example.test/catalog/search?q=beef').then(r => r.json());\n"
            "console.log(items.map(p => ({name:p.name, price:p.price, pricePerKg:p.price/p.weightKg})).sort((a,b)=>a.pricePerKg-b.pricePerKg));\n");
        script.close();
        const QVariantMap operation{{QStringLiteral("name"), QStringLiteral("Bash")},
            {QStringLiteral("command"), QStringLiteral("node meat_search.js")}};
        ToolNarrator narrator;
        narrator.setEnabled(true);
        QObject::connect(&narrator, &ToolNarrator::changed, &application, [&] {
            const QString text = narrator.explanation(operation, directory.path(), true);
            if (!text.isEmpty()) {
                QTextStream(stdout) << text << '\n' << "Diagnostics: " << narrator.diagnosticsPath() << Qt::endl;
                application.exit(text.contains(QStringLiteral("meat_search.js")) || text.contains(QStringLiteral("JavaScript")) ? 1 : 0);
            } else if (!narrator.status().startsWith(QStringLiteral("Translating"))) {
                QTextStream(stderr) << narrator.status() << Qt::endl;
                application.exit(1);
            }
        });
        QTimer::singleShot(0, &application, [&] { narrator.request(operation, directory.path(), true); });
        return application.exec();
    }
    ToolNarratorTest test;
    return QTest::qExec(&test, argc, argv);
}

#include "tst_tool_narrator.moc"
