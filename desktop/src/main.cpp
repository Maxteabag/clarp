#include "app/AppController.h"
#include "platform/DesktopIntegration.h"

#include <QApplication>
#include <QDir>
#include <QIcon>
#include <QImage>
#include <QLocalServer>
#include <QLocalSocket>
#include <QLockFile>
#include <QQmlApplicationEngine>
#include <QQmlError>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QStandardPaths>
#include <QTimer>

int main(int argc, char* argv[]) {
    // The application owns its Qt Quick style. Host-only QWidget themes such
    // as Kvantum are often absent from Flatpak/AppImage runtimes and should not
    // make a portable launch noisy or fail plugin discovery.
    qunsetenv("QT_STYLE_OVERRIDE");
    QApplication::setApplicationName(QStringLiteral("Clarp"));
    QApplication::setApplicationDisplayName(QStringLiteral("Clarp"));
    QApplication::setOrganizationName(QStringLiteral("MaxTeaBag"));
    QApplication::setOrganizationDomain(QStringLiteral("maxteabag.com"));
    QApplication::setApplicationVersion(QStringLiteral("0.1.0"));
    QQuickStyle::setStyle(QStringLiteral("Basic"));

    QApplication application(argc, argv);
    application.setWindowIcon(QIcon(QStringLiteral(":/qt/qml/Clarp/Desktop/resources/clarp.svg")));

    QString runtimeDirectory = QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation);
    if (runtimeDirectory.isEmpty()) {
        runtimeDirectory = QStandardPaths::writableLocation(QStandardPaths::TempLocation);
    }
    const QString instanceName = QStringLiteral("com.maxteabag.Clarp");
    QLockFile instanceLock(QDir(runtimeDirectory).filePath(instanceName + QStringLiteral(".lock")));
    if (!instanceLock.tryLock()) {
        QLocalSocket existing;
        existing.connectToServer(instanceName);
        if (existing.waitForConnected(500)) {
            existing.write("activate\n");
            existing.waitForBytesWritten(200);
        }
        return EXIT_SUCCESS;
    }
    QLocalServer::removeServer(instanceName);
    QLocalServer activationServer;
    activationServer.listen(instanceName);

    QQmlApplicationEngine engine;
    QObject::connect(&engine, &QQmlApplicationEngine::warnings, &application,
                     [](const auto& warnings) {
                         for (const QQmlError& warning : warnings) {
                             qCritical().noquote() << warning.toString();
                         }
                     });
    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreationFailed, &application,
        [] { QCoreApplication::exit(EXIT_FAILURE); }, Qt::QueuedConnection);
    engine.loadFromModule("Clarp.Desktop", "Main");

    std::unique_ptr<clarp::DesktopIntegration> desktopIntegration;
    QQuickWindow* rootWindow = nullptr;
    clarp::AppController* controller = nullptr;
    if (!engine.rootObjects().isEmpty()) {
        rootWindow = qobject_cast<QQuickWindow*>(engine.rootObjects().constFirst());
        controller = engine.rootObjects().constFirst()->findChild<clarp::AppController*>();
        if (rootWindow != nullptr && controller != nullptr) {
            desktopIntegration =
                std::make_unique<clarp::DesktopIntegration>(rootWindow, controller, &application);
        }
    }
    QObject::connect(&activationServer, &QLocalServer::newConnection, &application,
                     [&activationServer, rootWindow] {
                         while (activationServer.hasPendingConnections()) {
                             QLocalSocket* socket = activationServer.nextPendingConnection();
                             socket->deleteLater();
                         }
                         if (rootWindow != nullptr) {
                             rootWindow->show();
                             rootWindow->raise();
                             rootWindow->requestActivate();
                         }
                     });

    const QString screenshotPath = qEnvironmentVariable("CLARP_SCREENSHOT_PATH");
    const QString screenshotLayout = qEnvironmentVariable("CLARP_SCREENSHOT_LAYOUT");
    const QString screenshotView = qEnvironmentVariable("CLARP_SCREENSHOT_VIEW");
    if (!screenshotPath.isEmpty() && controller != nullptr && !screenshotLayout.isEmpty()) {
        QTimer::singleShot(1'000, &application, [controller, screenshotLayout] {
            controller->panes()->splitActive(QStringLiteral("vertical"),
                                             controller->selectedSession());
            if (screenshotLayout == QStringLiteral("nested")) {
                controller->panes()->splitActive(QStringLiteral("horizontal"),
                                                 controller->selectedSession());
            }
        });
    }
    if (!screenshotPath.isEmpty() && rootWindow != nullptr && !screenshotView.isEmpty()) {
        QTimer::singleShot(1'000, &application, [rootWindow, screenshotView] {
            if (QObject* view = rootWindow->findChild<QObject*>(screenshotView)) {
                view->setProperty("visible", true);
            }
        });
    }
    if (!screenshotPath.isEmpty()) {
        QTimer::singleShot(2'000, &application, [&application, rootWindow, screenshotPath] {
            if (rootWindow != nullptr) {
                rootWindow->grabWindow().save(screenshotPath);
            }
            application.quit();
        });
    }

    return application.exec();
}
