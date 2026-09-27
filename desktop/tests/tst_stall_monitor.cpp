#include "app/StallMonitor.h"
#include <QFile>
#include <QRegularExpression>
#include <QTemporaryDir>
#include <QTest>
#include <QTimer>
#include <chrono>
#include <thread>

// Exported so the captured stack can name it (the test target exports symbols).
extern "C" __attribute__((noinline)) void clarpStallTestBusyGuiThread(int milliseconds) {
    const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(milliseconds);
    volatile unsigned long spin = 0;
    while (std::chrono::steady_clock::now() < until) spin = spin + 1;
}

class StallMonitorTest : public QObject {
    Q_OBJECT
  private slots:
    void blockedGuiThreadIsLoggedWithItsStack() {
        QTemporaryDir dir;
        const QString log = dir.filePath(QStringLiteral("stalls.log"));
        clarp::StallMonitor monitor(100, log);
        QTest::qWait(100);                       // let the heartbeat start
        clarpStallTestBusyGuiThread(400);        // block the GUI thread
        QTRY_COMPARE_WITH_TIMEOUT(monitor.stallCount(), 1, 2000);
        QVERIFY(monitor.longestStallMs() >= 300);
        QFile file(log);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const QByteArray text = file.readAll();
        QVERIFY2(text.contains("== stall at"), text.constData());
        QVERIFY2(text.contains("clarpStallTestBusyGuiThread"), text.constData());
        QVERIFY2(text.contains("-- stall ended after"), text.constData());
    }
    void shortPausesAreNotStalls() {
        QTemporaryDir dir;
        clarp::StallMonitor monitor(200, dir.filePath(QStringLiteral("stalls.log")));
        QTest::qWait(100);
        clarpStallTestBusyGuiThread(60);
        QTest::qWait(300);
        QCOMPARE(monitor.stallCount(), 0);
    }
    void postedBlockDurationsUseWakeTime_data() {
        QTest::addColumn<int>("delayMs");
        QTest::addColumn<int>("blockMs");
        QTest::addColumn<bool>("expectStall");

        QTest::newRow("short-after-beat") << 5 << 100 << false;
        QTest::newRow("short-before-beat") << 70 << 100 << false;
        QTest::newRow("long-after-beat") << 5 << 250 << true;
        QTest::newRow("long-before-beat") << 70 << 250 << true;
    }
    void postedBlockDurationsUseWakeTime() {
        QFETCH(int, delayMs);
        QFETCH(int, blockMs);
        QFETCH(bool, expectStall);

        QTemporaryDir dir;
        const QString log = dir.filePath(QStringLiteral("stalls.log"));
        clarp::StallMonitor monitor(150, log, nullptr, 0);
        QTest::qWait(100);
        QTimer::singleShot(delayMs, qApp, [blockMs] { clarpStallTestBusyGuiThread(blockMs); });
        if (!expectStall) {
            QTest::qWait(delayMs + blockMs + 300);
            QCOMPARE(monitor.stallCount(), 0);
            QVERIFY(!QFile::exists(log));
            return;
        }

        QTRY_COMPARE_WITH_TIMEOUT(monitor.stallCount(), 1, 2000);
        QVERIFY2(monitor.longestStallMs() >= 230, qPrintable(QString::number(monitor.longestStallMs())));
        QVERIFY2(monitor.longestStallMs() <= 280, qPrintable(QString::number(monitor.longestStallMs())));
        QFile file(log);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const QString text = QString::fromUtf8(file.readAll());
        const QRegularExpressionMatch match =
            QRegularExpression(QStringLiteral("-- stall ended after (\\d+) ms")).match(text);
        QVERIFY2(match.hasMatch(), qPrintable(text));
        const int total = match.captured(1).toInt();
        QVERIFY2(total >= 230, qPrintable(QString::number(total)));
        QVERIFY2(total <= 280, qPrintable(QString::number(total)));
    }
    void memoryThresholdCapturesTheGuiStack() {
        QTemporaryDir dir;
        const QString log = dir.filePath(QStringLiteral("stalls.log"));
        // A threshold below the test's own footprint fires on the first check.
        clarp::StallMonitor monitor(10'000, log, nullptr, 1);
        QTest::qWait(800);
        QFile file(log);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const QByteArray text = file.readAll();
        QVERIFY2(text.contains("== memory at"), text.constData());
        QVERIFY2(text.contains("rss="), text.constData());
    }
    void disabledMonitorDoesNothing() {
        QTemporaryDir dir;
        const QString log = dir.filePath(QStringLiteral("stalls.log"));
        clarp::StallMonitor monitor(0, log, nullptr, 0);
        clarpStallTestBusyGuiThread(300);
        QTest::qWait(100);
        QCOMPARE(monitor.stallCount(), 0);
        QVERIFY(!QFile::exists(log));
    }
};
QTEST_GUILESS_MAIN(StallMonitorTest)
#include "tst_stall_monitor.moc"
