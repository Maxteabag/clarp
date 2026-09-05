#include "app/AppController.h"
#include "platform/DesktopIntegration.h"

#include <QApplication>
#include <QDir>
#include <QIcon>
#include <QImage>
#include <QJsonArray>
#include <QJsonObject>
#include <QJSValue>
#include <QLocalServer>
#include <QLocalSocket>
#include <QLockFile>
#include <QQmlApplicationEngine>
#include <QQmlError>
#include <QQuickItem>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QSignalBlocker>
#include <QStandardPaths>
#include <QTimer>
#include <algorithm>

int main(int argc, char* argv[]) {
    // The application owns its Qt Quick style. Host-only QWidget themes such
    // as Kvantum are often absent from Flatpak/AppImage runtimes and should not
    // make a portable launch noisy or fail plugin discovery.
    qunsetenv("QT_STYLE_OVERRIDE");
    QApplication::setApplicationName(QStringLiteral("Clarp"));
    // Screenshot runs must never read or mutate the user's persisted pane
    // tree, drafts, or view preferences. QSettings keys its storage by the
    // application name, so use an isolated namespace before AppController is
    // constructed.
    if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PATH")) {
        QApplication::setApplicationName(QStringLiteral("ClarpScreenshot"));
    }
    QApplication::setApplicationDisplayName(QStringLiteral("Clarp"));
    QApplication::setOrganizationName(QStringLiteral("MaxTeaBag"));
    QApplication::setOrganizationDomain(QStringLiteral("maxteabag.com"));
    QApplication::setApplicationVersion(QStringLiteral(CLARP_DESKTOP_VERSION));
    QQuickStyle::setStyle(QStringLiteral("Basic"));

    QApplication application(argc, argv);
    application.setWindowIcon(QIcon(QStringLiteral(":/qt/qml/Clarp/Desktop/resources/clarp.svg")));

    QString runtimeDirectory = QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation);
    if (runtimeDirectory.isEmpty()) {
        runtimeDirectory = QStandardPaths::writableLocation(QStandardPaths::TempLocation);
    }
    // Development/test builds can coexist with the installed client without
    // changing XDG_RUNTIME_DIR (which would also hide the Wayland socket).
    const QString instanceName =
        qEnvironmentVariable("CLARP_INSTANCE_NAME", QStringLiteral("com.maxteabag.Clarp"));
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
                     [&activationServer, rootWindow, controller] {
                         while (activationServer.hasPendingConnections()) {
                             QLocalSocket* socket = activationServer.nextPendingConnection();
                             socket->deleteLater();
                         }
                        if (rootWindow != nullptr) {
                            rootWindow->show();
                            rootWindow->raise();
                            rootWindow->requestActivate();
                            if (controller != nullptr) {
                                controller->requestComposerFocus(
                                    controller->panes()->activePaneId());
                            }
                        }
                    });

    const QString screenshotPath = qEnvironmentVariable("CLARP_SCREENSHOT_PATH");
    const QString screenshotLayout = qEnvironmentVariable("CLARP_SCREENSHOT_LAYOUT");
    const QString screenshotView = qEnvironmentVariable("CLARP_SCREENSHOT_VIEW");
    const QString screenshotScenario = qEnvironmentVariable("CLARP_SCREENSHOT_SCENARIO");
    if (!screenshotPath.isEmpty() && controller != nullptr && !screenshotLayout.isEmpty()) {
        QTimer::singleShot(1'000, &application, [controller, screenshotLayout] {
            controller->panes()->splitActive(QStringLiteral("vertical"),
                                             controller->selectedSession());
            if (screenshotLayout == QStringLiteral("nested")) {
                controller->panes()->splitActive(QStringLiteral("horizontal"),
                                                 controller->selectedSession());
            } else if (screenshotLayout == QStringLiteral("zoomed")) {
                const QString paneId = controller->panes()->activePaneId();
                controller->setPaneDraft(
                    paneId, controller->selectedSession(),
                    QStringLiteral("Draft preserved while this pane is zoomed"));
                controller->requestComposerFocus(paneId);
                controller->panes()->toggleZoom();
            } else if (screenshotLayout == QStringLiteral("grid4")) {
                controller->panes()->splitActive(QStringLiteral("horizontal"),
                                                 controller->selectedSession());
                controller->panes()->navigate(QStringLiteral("left"));
                controller->panes()->splitActive(QStringLiteral("horizontal"),
                                                 controller->selectedSession());
                const QVariantList fourPanes = controller->panes()->paneLayout();
                for (const QVariant& value : fourPanes) {
                    controller->panes()->focusPane(
                        value.toMap().value(QStringLiteral("id")).toString());
                    controller->panes()->splitActive(QStringLiteral("vertical"),
                                                     controller->selectedSession());
                }
                const QVariantList eightPanes = controller->panes()->paneLayout();
                for (const QVariant& value : eightPanes) {
                    controller->panes()->focusPane(
                        value.toMap().value(QStringLiteral("id")).toString());
                    controller->panes()->splitActive(QStringLiteral("horizontal"),
                                                     controller->selectedSession());
                }
            }
        });
    }
    if (!screenshotPath.isEmpty() && rootWindow != nullptr && !screenshotView.isEmpty()) {
        QTimer::singleShot(1'000, &application, [rootWindow, controller, screenshotView] {
            if (screenshotView == QStringLiteral("idleContacts")) {
                if (QObject* picker = rootWindow->findChild<QObject*>(QStringLiteral("quickSwitcher"))) {
                    QMetaObject::invokeMethod(picker, "openContacts", Q_ARG(QVariant, false));
                }
                return;
            }
            if (QObject* view = rootWindow->findChild<QObject*>(screenshotView)) {
                if (controller != nullptr &&
                    (screenshotView == QStringLiteral("agentProfilePanel") ||
                     screenshotView == QStringLiteral("queueDialog"))) {
                    const QString session = controller->selectedSession();
                    view->setProperty("session", session);
                    if (screenshotView == QStringLiteral("agentProfilePanel")) {
                        controller->loadAgentProfile(session);
                    } else {
                        controller->loadTurnQueue(session);
                    }
                }
                view->setProperty("visible", true);
            }
        });
    }
    if (!screenshotPath.isEmpty() && rootWindow != nullptr) {
        const QStringList size = qEnvironmentVariable("CLARP_SCREENSHOT_SIZE").split(u'x');
        if (size.size() == 2) {
            rootWindow->resize(std::max(760, size.at(0).toInt()),
                               std::max(520, size.at(1).toInt()));
        }
    }
    if (!screenshotPath.isEmpty() && controller != nullptr && !screenshotScenario.isEmpty()) {
        QTimer::singleShot(1'900, &application,
                           [controller, screenshotScenario] {
            QString session = controller->selectedSession();
            if (session.isEmpty() && (screenshotScenario == QStringLiteral("markdown") ||
                                      screenshotScenario == QStringLiteral("tool-spacing"))) {
                session = QStringLiteral("markdown-fixture");
                controller->agents()->applySnapshot(
                    {{QStringLiteral("agents"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("agent_id"), QStringLiteral("fixture-agent")},
                          {QStringLiteral("session"), session},
                          {QStringLiteral("persona"), QStringLiteral("OPUS")},
                          {QStringLiteral("backend"), QStringLiteral("local")},
                          {QStringLiteral("latest_state"), QStringLiteral("idle")},
                      }}}});
                {
                    const QSignalBlocker blockPaneSelection(controller->panes());
                    controller->panes()->setActiveSession(session);
                }
                emit controller->panes()->treeChanged();
                controller->requestComposerFocus(controller->panes()->activePaneId());
            }
            clarp::ConversationModel* model = controller->conversationForSession(session);
            if (session.isEmpty() || model == nullptr) {
                return;
            }
            if (screenshotScenario == QStringLiteral("loading")) {
                model->applyLog({{QStringLiteral("conversation_id"),
                                  QStringLiteral("screenshot-loading")},
                                 {QStringLiteral("turns"), QJsonArray{}},
                                 {QStringLiteral("latest_revision"), 0}},
                                clarp::ConversationModel::LoadKind::Tail);
                model->setLoading(true);
                return;
            }
            QJsonArray turns;
            if (screenshotScenario == QStringLiteral("streaming")) {
                turns = {
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-user")},
                                {QStringLiteral("role"), QStringLiteral("user")},
                                {QStringLiteral("text"), QStringLiteral("Explain the release state clearly.")},
                                {QStringLiteral("revision"), 1}},
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-live")},
                                {QStringLiteral("role"), QStringLiteral("assistant")},
                                {QStringLiteral("kind"), QStringLiteral("live")},
                                {QStringLiteral("text"), QStringLiteral("I checked the build, tests, and live preview. The current result is <spe")},
                                {QStringLiteral("revision"), 2}},
                };
            } else if (screenshotScenario == QStringLiteral("activity")) {
                turns = {
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-answer")},
                                {QStringLiteral("role"), QStringLiteral("assistant")},
                                {QStringLiteral("text"), QString{}},
                                {QStringLiteral("revision"), 3},
                                {QStringLiteral("activity_count"), 2},
                                {QStringLiteral("display_cells"),
                                 QJsonArray{
                                     QJsonObject{{QStringLiteral("title"), QStringLiteral("Read")},
                                                 {QStringLiteral("summary"), QStringLiteral("ConversationTimeline.swift")},
                                                 {QStringLiteral("status"), QStringLiteral("ok")}},
                                     QJsonObject{{QStringLiteral("title"), QStringLiteral("Test")},
                                                 {QStringLiteral("summary"), QStringLiteral("Native core and QML lint")},
                                                 {QStringLiteral("status"), QStringLiteral("ok")}},
                                 }}},
                };
            } else if (screenshotScenario == QStringLiteral("tool-spacing")) {
                controller->clearError();
                controller->setToolsVisible(true);
                turns = {
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-user")},
                                {QStringLiteral("role"), QStringLiteral("user")},
                                {QStringLiteral("text"), QStringLiteral("Make this easier to read and keep the tool calls compact.")},
                                {QStringLiteral("revision"), 1}},
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-tools")},
                                {QStringLiteral("role"), QStringLiteral("assistant")},
                                {QStringLiteral("text"), QStringLiteral("I found the extra padding around each tool row.")},
                                {QStringLiteral("revision"), 2},
                                {QStringLiteral("activity_count"), 3},
                                {QStringLiteral("display_cells"), QJsonArray{
                                    QJsonObject{{QStringLiteral("title"), QStringLiteral("Read")},
                                                {QStringLiteral("summary"), QStringLiteral("desktop/qml/components/MessageDelegate.qml")},
                                                {QStringLiteral("status"), QStringLiteral("ok")}},
                                    QJsonObject{{QStringLiteral("title"), QStringLiteral("Edit")},
                                                {QStringLiteral("summary"), QStringLiteral("Compact activity rows and larger text")},
                                                {QStringLiteral("status"), QStringLiteral("ok")},
                                                {QStringLiteral("lines"), QJsonArray{
                                                    QJsonObject{{QStringLiteral("kind"), QStringLiteral("diff_old")},
                                                                {QStringLiteral("text"), QStringLiteral("- implicitHeight: content.implicitHeight + 14")}},
                                                    QJsonObject{{QStringLiteral("kind"), QStringLiteral("diff_new")},
                                                                {QStringLiteral("text"), QStringLiteral("+ implicitHeight: content.implicitHeight + 6")}},
                                                }}},
                                    QJsonObject{{QStringLiteral("title"), QStringLiteral("Test")},
                                                {QStringLiteral("summary"), QStringLiteral("Sidebar visibility, composer focus, and layout")},
                                                {QStringLiteral("status"), QStringLiteral("ok")}},
                                }}},
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-command")},
                                {QStringLiteral("role"), QStringLiteral("assistant")},
                                {QStringLiteral("text"), QString{}},
                                {QStringLiteral("revision"), 3},
                                {QStringLiteral("tools"), QJsonArray{QJsonObject{
                                    {QStringLiteral("name"), QStringLiteral("Bash")},
                                    {QStringLiteral("summary"), QStringLiteral("Build the preview")},
                                    {QStringLiteral("command"), QStringLiteral("cmake --build desktop/build/dev --parallel 4")},
                                    {QStringLiteral("result"), QStringLiteral("Build complete.")},
                                    {QStringLiteral("status"), QStringLiteral("ok")},
                                }}}},
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-result")},
                                {QStringLiteral("role"), QStringLiteral("assistant")},
                                {QStringLiteral("text"), QStringLiteral("The sidebar can be fully hidden with **Ctrl+B**. Text is larger, and tool calls stay close to the reply.\n\nParagraphs still have breathing room. **Ctrl+K** opens commands from anywhere.")},
                                {QStringLiteral("revision"), 4}},
                };
            } else if (screenshotScenario == QStringLiteral("markdown")) {
                turns = {
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("fixture-user")},
                                {QStringLiteral("role"), QStringLiteral("user")},
                                {QStringLiteral("text"),
                                 QStringLiteral("Why are the paragraphs hard to scan?")},
                                {QStringLiteral("revision"), 1}},
                    QJsonObject{
                        {QStringLiteral("id"), QStringLiteral("fixture-markdown")},
                        {QStringLiteral("role"), QStringLiteral("assistant")},
                        {QStringLiteral("text"),
                         QStringLiteral(
                             "The first paragraph should read as one complete thought. It can "
                             "wrap naturally when the pane is narrow.\n\n"
                             "The second paragraph is a new thought, so the blank Markdown line "
                             "must create visible breathing room.\n\n"
                             "A third paragraph makes the rhythm obvious. **Emphasis** and "
                             "`inline code` should still render correctly.\n\n"
                             "1. Lists remain structurally intact\n\n"
                             "2. Their numbering must not restart")},
                        {QStringLiteral("revision"), 2}},
                };
            } else if (screenshotScenario == QStringLiteral("long")) {
                for (int index = 0; index < 60; ++index) {
                    turns.append(QJsonObject{
                        {QStringLiteral("id"), QStringLiteral("fixture-%1").arg(index)},
                        {QStringLiteral("role"), index % 2 == 0 ? QStringLiteral("user")
                                                               : QStringLiteral("assistant")},
                        {QStringLiteral("text"),
                         QStringLiteral("Message %1 keeps a stable identity while history grows and the composer remains anchored.").arg(index + 1)},
                        {QStringLiteral("timestamp"), QStringLiteral("2026-09-04T17:%1:00Z").arg(index % 60, 2, 10, QLatin1Char('0'))},
                        {QStringLiteral("revision"), index + 1},
                    });
                }
            }
            if (!turns.isEmpty()) {
                model->applyLog({{QStringLiteral("conversation_id"),
                                  QStringLiteral("screenshot-fixture")},
                                 {QStringLiteral("turns"), turns},
                                 {QStringLiteral("latest_revision"), turns.size()},
                                 {QStringLiteral("has_more"),
                                  screenshotScenario == QStringLiteral("long")}},
                                clarp::ConversationModel::LoadKind::Tail);
                // Explicit opt-in only: normal screenshot/tests never call a model.
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_TOOL_NARRATION"))
                    controller->toolNarrator()->setEnabled(true);
            }
        });
    }
    if (!screenshotPath.isEmpty()) {
        const int sidebarWidth = qEnvironmentVariableIntValue("CLARP_SCREENSHOT_SIDEBAR_WIDTH");
        if (sidebarWidth > 0 && rootWindow != nullptr) {
            QTimer::singleShot(1'950, &application, [rootWindow, sidebarWidth] {
                rootWindow->setProperty("sidebarExpandedWidth", sidebarWidth);
            });
        }
        const int sidebarToggles = qEnvironmentVariableIntValue("CLARP_SCREENSHOT_SIDEBAR_TOGGLES");
        if (sidebarToggles > 0 && rootWindow != nullptr) {
            QTimer::singleShot(2'000, &application, [rootWindow, sidebarToggles] {
                rootWindow->setProperty("sidebarVisible", true);
                for (int index = 0; index < sidebarToggles; ++index) {
                    if (sidebarToggles == 2 && index == 0) {
                        // Hide from the header, then restore with the same
                        // command used by Ctrl+B. Both must share one state.
                        QObject* button = rootWindow->findChild<QObject*>(
                            QStringLiteral("sidebarHideButton"));
                        if (button != nullptr) {
                            QMetaObject::invokeMethod(button, "clicked");
                        }
                        continue;
                    }
                    QMetaObject::invokeMethod(rootWindow, "runCommand",
                                              Q_ARG(QVariant, QStringLiteral("sidebar")));
                }
            });
        }
        if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_EXPAND_TOOLS") && rootWindow != nullptr) {
            QTimer::singleShot(2'100, &application, [&application, rootWindow, screenshotScenario] {
                QList<QQuickItem*> pending{rootWindow->contentItem()};
                while (!pending.isEmpty()) {
                    QQuickItem* item = pending.takeLast();
                    pending.append(item->childItems());
                    if (item->objectName() == QStringLiteral("toolCard") ||
                        item->objectName() == QStringLiteral("displayCellCard")) {
                        item->setProperty("expanded", true);
                        if (screenshotScenario == QStringLiteral("tool-spacing") &&
                            item->objectName() == QStringLiteral("displayCellCard") &&
                            item->property("title").toString() == QStringLiteral("Edit") &&
                            item->property("lines").value<QJSValue>()
                                    .property(QStringLiteral("length")).toInt() != 2) {
                            qCritical("Native activity details were lost during QML conversion");
                            application.exit(EXIT_FAILURE);
                        }
                    }
                }
            });
        }
        const int requestedDelay = qEnvironmentVariableIntValue("CLARP_SCREENSHOT_DELAY_MS");
        const int captureDelay = requestedDelay > 0 ? std::clamp(requestedDelay, 2'400, 60'000)
            : screenshotScenario.isEmpty() ? 2'000 : 2'400;
        QTimer::singleShot(captureDelay, &application, [&application, rootWindow, screenshotPath, sidebarToggles, sidebarWidth] {
            if (rootWindow != nullptr) {
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_TOOL_NARRATION")) {
                    QList<QQuickItem*> remaining{rootWindow->contentItem()};
                    int translatedRows = 0;
                    while (!remaining.isEmpty()) {
                        QQuickItem* item = remaining.takeLast();
                        remaining.append(item->childItems());
                        if (item->objectName() == QStringLiteral("activityExplanationText") &&
                            item->isVisible() && !item->property("text").toString().isEmpty()) ++translatedRows;
                    }
                    if (translatedRows < 4) {
                        qCritical("Expected four real translated activity rows before capture");
                        application.exit(EXIT_FAILURE);
                        return;
                    }
                }
                if (sidebarToggles > 0) {
                    const auto* rail = rootWindow->findChild<QQuickItem*>(QStringLiteral("sidebarRail"));
                    const auto* surface = rootWindow->findChild<QQuickItem*>(QStringLiteral("workspaceSurface"));
                    const bool shown = sidebarToggles % 2 == 0;
                    if (rail == nullptr || surface == nullptr || rail->isVisible() != shown ||
                        (shown && rail->width() < 208) ||
                        (shown && sidebarWidth > 0 && qAbs(rail->width() - sidebarWidth) > 1) ||
                        (!shown && (surface->x() != 0 ||
                                    qAbs(surface->width() - surface->parentItem()->width()) > 1))) {
                        qCritical("Sidebar toggle did not restore visibility and workspace geometry");
                        application.exit(EXIT_FAILURE);
                        return;
                    }
                }
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_REQUIRE_COMPOSER_FOCUS") &&
                    (rootWindow->activeFocusItem() == nullptr ||
                     rootWindow->activeFocusItem()->objectName() !=
                         QStringLiteral("paneComposerEditor"))) {
                    qCritical("The active pane composer did not own focus");
                    application.exit(EXIT_FAILURE);
                    return;
                }
                rootWindow->grabWindow().save(screenshotPath);
            }
            application.quit();
        });
    }

    return application.exec();
}
