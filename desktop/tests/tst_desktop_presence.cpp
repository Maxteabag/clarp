#include "platform/DesktopPresence.h"
#include <QGuiApplication>
#include <QQuickWindow>
#include <QSignalSpy>
#include <QTest>
using clarp::DesktopPresence;
class DesktopPresenceTest : public QObject {
    Q_OBJECT
  private slots:
    void maintenanceActivityDoesNotDependOnPushPreference() {
        QQuickWindow window;
        DesktopPresence presence(&window, nullptr, false);
        QSignalSpy reports(&presence, &DesktopPresence::applicationActivity);
        presence.setEnabled(false);
        presence.setConnected(true);
        presence.setSessionState(true, true);
        window.show(); window.requestActivate();
        QTRY_VERIFY(window.isActive());
        presence.noteInteraction();
        QTRY_VERIFY(!reports.isEmpty());
        QVERIFY(reports.last().at(2).toBool());
        QVERIFY(!presence.active());
        window.hide();
        QTRY_VERIFY(!reports.last().at(2).toBool());
    }
    void eligibilityExpiresWithoutUserInput() {
        QVERIFY(DesktopPresence::eligible(true, true, true, 0));
        QVERIFY(DesktopPresence::eligible(true, true, true, 119999));
        QVERIFY(!DesktopPresence::eligible(true, true, true, 120000));
        QVERIFY(!DesktopPresence::eligible(true, true, true, -1));
        QVERIFY(!DesktopPresence::eligible(false, true, true, 1));
        QVERIFY(!DesktopPresence::eligible(true, false, true, 1));
        QVERIFY(!DesktopPresence::eligible(true, true, false, 1));
    }
    void focusLockSleepAndPreferenceReleasePresence() {
        QQuickWindow window;
        DesktopPresence presence(&window, nullptr, false);
        QSignalSpy reports(&presence, &DesktopPresence::presenceReport);
        presence.setSessionState(true, true);
        presence.setConnected(true);
        window.show(); window.requestActivate();
        QTRY_VERIFY(window.isActive());
        presence.noteInteraction();
        QTRY_VERIFY(presence.active());
        QVERIFY(!reports.isEmpty());
        QVERIFY(reports.last().at(2).toBool());
        const auto firstSequence = reports.last().at(1).toULongLong();
        presence.setSessionState(true, false);
        QVERIFY(!presence.active());
        QVERIFY(!reports.last().at(2).toBool());
        presence.setSessionState(true, true);
        presence.noteInteraction();
        QVERIFY(presence.active());
        presence.setEnabled(false);
        QVERIFY(!presence.active());
        presence.setEnabled(true);
        QVERIFY(presence.active());
        QVERIFY(QMetaObject::invokeMethod(&presence, "prepareForSleep", Q_ARG(bool, true)));
        QVERIFY(!presence.active());
        QVERIFY(QMetaObject::invokeMethod(&presence, "prepareForSleep", Q_ARG(bool, false)));
        QVERIFY(!presence.active()); // Fresh input required after resume.
        presence.noteInteraction();
        QVERIFY(presence.active());
        presence.setConnected(false);
        QVERIFY(!presence.active());
        presence.setConnected(true);
        window.hide();
        QTRY_VERIFY(!presence.active());
        QVERIFY(!reports.last().at(2).toBool());
        QVERIFY(reports.last().at(1).toULongLong() > firstSequence);
    }
    void unknownSessionNeverSuppresses() {
        QQuickWindow window;
        DesktopPresence presence(&window, nullptr, false);
        presence.setConnected(true);
        window.show(); window.requestActivate();
        QTRY_VERIFY(window.isActive());
        presence.noteInteraction();
        QVERIFY(!presence.active());
    }
};
int main(int argc, char** argv) {
    QGuiApplication app(argc, argv);
    if (app.arguments().contains(QStringLiteral("--probe-session"))) {
        QQuickWindow window;
        DesktopPresence presence(&window);
        QTimer::singleShot(1500, &app, [&] {
            qInfo("Session detected=%d unlocked=%d suppression=%d", presence.sessionAvailable(), presence.sessionUnlocked(), presence.active());
            app.exit(presence.sessionAvailable() ? 0 : 1);
        });
        return app.exec();
    }
    DesktopPresenceTest test;
    return QTest::qExec(&test, argc, argv);
}
#include "tst_desktop_presence.moc"
