#include "platform/DesktopPresence.h"
#include "app/PairSidebarSmokeCheck.h"
#include "app/ReadyReplySmokeCheck.h"
#include "app/KeyboardSmokeCheck.h"
#include "app/VoiceViewportSmokeCheck.h"
#include "app/LaunchKeyboardSmokeCheck.h"
#include "app/AppController.h"
#include "app/DesktopPalette.h"
#include "platform/DesktopIntegration.h"

#include <QApplication>
#include <QCommandLineParser>
#include <QSettings>
#include <QIcon>
#include <QFont>
#include <QImage>
#include <QJsonArray>
#include <QJsonObject>
#include <QJsonDocument>
#include <QSaveFile>
#include <QCryptographicHash>
#include <QKeyEvent>
#include <QJSValue>
#include <QQmlApplicationEngine>
#include <QQmlError>
#include <QQuickItem>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QSignalBlocker>
#include <QStyleHints>
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
    QCommandLineParser launchParser;
    launchParser.setApplicationDescription(QStringLiteral("Clarp desktop and agent launcher"));
    launchParser.addHelpOption();
    launchParser.addVersionOption();
    launchParser.addOption({QStringLiteral("anonymous"), QStringLiteral("Start anonymously, overriding Settings")});
    launchParser.addOption({QStringLiteral("contact"), QStringLiteral("Start with an available contact, overriding Settings")});
    launchParser.addOption({QStringLiteral("new-agent"), QStringLiteral("Start an agent; prompt for backend if omitted")});
    launchParser.addOption({QStringLiteral("no-new-agent"), QStringLiteral("Open the desktop without starting an agent, overriding Settings")});
    launchParser.addOption({QStringLiteral("backend"), QStringLiteral("Start with claude, codex, grok, agy, or opencode (implies --new-agent)"), QStringLiteral("backend")});
    launchParser.addOption({QStringLiteral("cwd"), QStringLiteral("Workspace directory; skip directory selection"), QStringLiteral("directory")});
    launchParser.addOption({QStringLiteral("model"), QStringLiteral("Use this backend model ID"), QStringLiteral("model")});
    launchParser.addOption({QStringLiteral("effort"), QStringLiteral("Use this model reasoning effort"), QStringLiteral("effort")});
    launchParser.addOption({QStringLiteral("preview-versions"), QStringLiteral("Manage saved preview versions")});
    launchParser.process(application);
    const QString launchBackend = launchParser.value(QStringLiteral("backend")).trimmed().toLower();
    const int anonymousMode = launchParser.isSet(QStringLiteral("anonymous")) ? 1 : launchParser.isSet(QStringLiteral("contact")) ? 0 : -1;
    const bool explicitAgentLaunch = launchParser.isSet(QStringLiteral("cwd")) || anonymousMode >= 0 || launchParser.isSet(QStringLiteral("new-agent"))
        || launchParser.isSet(QStringLiteral("backend")) || launchParser.isSet(QStringLiteral("model"))
        || launchParser.isSet(QStringLiteral("effort"));
    if ((launchParser.isSet(QStringLiteral("anonymous")) && launchParser.isSet(QStringLiteral("contact")))
        || (explicitAgentLaunch && launchParser.isSet(QStringLiteral("no-new-agent")))
        || (launchParser.isSet(QStringLiteral("backend")) && !QStringList{
            QStringLiteral("claude"), QStringLiteral("codex"), QStringLiteral("grok"),
            QStringLiteral("agy"), QStringLiteral("opencode")}.contains(launchBackend))) {
        qCritical("Invalid backend or conflicting agent launch flags. See --help.");
        return EXIT_FAILURE;
    }
    QFont uiFont = application.font();
    uiFont.setFamily(QStringLiteral("JetBrains Mono"));
    uiFont.setStyleHint(QFont::Monospace);
    QApplication::setFont(uiFont);
    application.styleHints()->setColorScheme(Qt::ColorScheme::Dark);
    QApplication::setPalette(clarp::desktopPalette(application.palette()));
    application.setWindowIcon(QIcon(QStringLiteral(":/qt/qml/Clarp/Desktop/resources/clarp.svg")));

    // Each launch owns its window and event loop; no process-wide activation lock.
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
    const bool versionManager = application.arguments().contains(QStringLiteral("--preview-versions"));
    const bool restoreDesktop = qEnvironmentVariable("CLARP_RESTORE_DESKTOP") == QStringLiteral("1");
    const QString restoreSession = qEnvironmentVariable("CLARP_RESTORE_SESSION");
    qunsetenv("CLARP_RESTORE_DESKTOP");
    qunsetenv("CLARP_RESTORE_SESSION");
    const bool launchOnStartup = !restoreDesktop && !versionManager && !launchParser.isSet(QStringLiteral("no-new-agent"))
        && (explicitAgentLaunch || (QSettings().value(QStringLiteral("launch/newAgentOnStartup"), true).toBool()
            && !qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PATH")));
    const bool emptyStartup = !restoreDesktop && !versionManager && launchParser.isSet(QStringLiteral("no-new-agent"));
    application.setProperty("clarpEmptyStartup", emptyStartup);
    application.setProperty("clarpLaunchMode", launchOnStartup);
    if (!versionManager) engine.setInitialProperties({{QStringLiteral("launchOnStartup"), launchOnStartup},
        {QStringLiteral("sidebarVisible"), emptyStartup}});
    if (versionManager) application.setApplicationName(QStringLiteral("ClarpPreviewVersionManager"));
    engine.loadFromModule("Clarp.Desktop", versionManager ? "PreviewVersionWindow" : "Main");

    std::unique_ptr<clarp::DesktopIntegration> desktopIntegration;
    std::unique_ptr<clarp::DesktopPresence> desktopPresence;
    QQuickWindow* rootWindow = nullptr;
    clarp::AppController* controller = nullptr;
    if (!engine.rootObjects().isEmpty()) {
        rootWindow = qobject_cast<QQuickWindow*>(engine.rootObjects().constFirst());
        controller = engine.rootObjects().constFirst()->findChild<clarp::AppController*>();
        if (rootWindow != nullptr && controller != nullptr) {
            if (restoreDesktop) controller->restoreDesktopSession(restoreSession);
            desktopIntegration =
                std::make_unique<clarp::DesktopIntegration>(rootWindow, controller, &application);
            if (!qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PATH")) {
                desktopPresence = std::make_unique<clarp::DesktopPresence>(rootWindow, &application);
                auto* presence = desktopPresence.get();
                QObject::connect(presence, &clarp::DesktopPresence::presenceReport, controller, &clarp::AppController::reportDesktopPresence);
                QObject::connect(presence, &clarp::DesktopPresence::applicationActivity, controller, &clarp::AppController::reportApplicationActivity);
                QObject::connect(controller, &clarp::AppController::pauseMobilePushChanged, presence,
                    [presence, controller] { presence->setEnabled(controller->pauseMobilePush()); });
                QObject::connect(controller, &clarp::AppController::connectedChanged, presence,
                    [presence, controller] { presence->setConnected(controller->connected()); });
                presence->setEnabled(controller->pauseMobilePush());
                presence->setConnected(controller->connected());
            }
        }
    }
    if (rootWindow != nullptr && controller != nullptr && launchOnStartup) {
        controller->setLaunchMode(true);
        const QString launchDirectory = launchParser.value(QStringLiteral("cwd"));
        const QString launchModel = launchParser.value(QStringLiteral("model"));
        const QString launchEffort = launchParser.value(QStringLiteral("effort"));
        QTimer::singleShot(0, rootWindow, [rootWindow, launchBackend, launchModel, launchEffort, anonymousMode, launchDirectory] {
            QMetaObject::invokeMethod(rootWindow, "openLaunchAgent",
                Q_ARG(QVariant, launchBackend), Q_ARG(QVariant, launchModel), Q_ARG(QVariant, launchEffort), Q_ARG(QVariant, anonymousMode), Q_ARG(QVariant, launchDirectory));
        });
    }
    const QString screenshotPath = qEnvironmentVariable("CLARP_SCREENSHOT_PATH");
    if (!screenshotPath.isEmpty() && rootWindow != nullptr) startLaunchKeyboardSmokeCheck(rootWindow);
    if (!screenshotPath.isEmpty() && controller != nullptr && qEnvironmentVariableIsSet("CLARP_SCREENSHOT_MINIMAL_UI"))
        controller->setMinimalUi(qEnvironmentVariableIntValue("CLARP_SCREENSHOT_MINIMAL_UI") != 0);
    const QString screenshotLayout = qEnvironmentVariable("CLARP_SCREENSHOT_LAYOUT");
    const QString screenshotView = qEnvironmentVariable("CLARP_SCREENSHOT_VIEW");
    const QString screenshotScenario = qEnvironmentVariable("CLARP_SCREENSHOT_SCENARIO");
    const QString screenshotPopup = qEnvironmentVariable("CLARP_SCREENSHOT_OPEN_POPUP");
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
    if (!screenshotPath.isEmpty() && rootWindow != nullptr && !screenshotPopup.isEmpty()) {
        QTimer::singleShot(1'500, &application, [rootWindow, screenshotPopup] {
            auto* control = rootWindow->findChild<QObject*>(screenshotPopup);
            auto* popup = control != nullptr ? control->property("popup").value<QObject*>() : nullptr;
            if (popup == nullptr || !QMetaObject::invokeMethod(popup, "open")) {
                qCritical("Requested screenshot popup could not be opened");
                QCoreApplication::exit(EXIT_FAILURE);
                return;
            }
            auto* palette = control->property("palette").value<QObject*>();
            auto* background = popup->property("background").value<QObject*>();
            if (palette == nullptr || background == nullptr ||
                palette->property("midlight").value<QColor>() != QColor(QStringLiteral("#41445a")) ||
                background->property("color").value<QColor>().lightnessF() >= 0.25) {
                qCritical("Light system colors leaked into the control or popup palette");
                QCoreApplication::exit(EXIT_FAILURE);
            }
        });
    }
    // A report fixture whose HTML deliberately contains every remote-fetch
    // vector Qt's rich text engine honours. CLARP_REPORT_TRACKER_ORIGIN lets a
    // CTest point those references at a listener and assert nothing is fetched.
    if (!screenshotPath.isEmpty() && controller != nullptr && rootWindow != nullptr
        && screenshotScenario == QStringLiteral("report")) {
        QTimer::singleShot(1'950, &application, [controller, rootWindow] {
            const QString tracker = qEnvironmentVariable("CLARP_REPORT_TRACKER_ORIGIN",
                                                         QStringLiteral("http://tracker.invalid"));
            const QString body = QStringLiteral(
                "<html><head><style>@import url(\"%1/imported.css\");"
                "body{background-image:url('%1/bg.png')}"
                ".card{background:url(%1/shorthand.png)}</style></head><body>"
                "<h1>Deployment review</h1>"
                "<p style=\"background-image:url(%1/inline.png)\">Three services were "
                "redeployed. Latency returned to baseline within four minutes.</p>"
                "<h2>Measured results</h2>"
                "<table border=\"1\" background=\"%1/tablebg.png\">"
                "<thead><tr><th>Service</th><th>p95 before</th><th>p95 after</th></tr></thead>"
                "<tbody><tr><td>api</td><td>1.8 s</td><td>240 ms</td></tr>"
                "<tr><td>worker</td><td>920 ms</td><td>310 ms</td></tr></tbody></table>"
                "<h2>Follow-ups</h2><ul><li>Re-run the load test"
                "<ul><li>include the cold-start path</li></ul></li>"
                "<li>Delete the temporary index</li></ul>"
                "<blockquote>The N+1 query was the whole regression.</blockquote>"
                "<p>Full detail: <a href=\"https://example.com/deploy/report\">the run log</a>"
                " and <img src=\"%1/pixel.png\" width=\"12\" height=\"12\"> a tracking pixel.</p>"
                "<script>fetch('%1/beacon')</script>"
                "</body></html>").arg(tracker);
            controller->seedScreenshotArtifacts({QVariantMap{
                {QStringLiteral("artifact_id"), QStringLiteral("report-fixture")},
                {QStringLiteral("type"), QStringLiteral("document")},
                {QStringLiteral("title"), QStringLiteral("Deployment review")},
                {QStringLiteral("summary"), QStringLiteral("What changed and what it measured")},
                {QStringLiteral("session"), controller->selectedSession()},
                {QStringLiteral("content"), body}}});
            if (QObject* view = rootWindow->findChild<QObject*>(QStringLiteral("reportView"))) {
                QMetaObject::invokeMethod(view, "open",
                                          Q_ARG(QVariant, QStringLiteral("report-fixture")));
            } else {
                qCritical("The report view is missing");
                QCoreApplication::exit(EXIT_FAILURE);
                return;
            }
            // A wrong reader parses the HTML into an empty document that still
            // reports a valid width, so assert real rendered height and the
            // absence of any reference that would leave this machine.
            QTimer::singleShot(900, qApp, [rootWindow] {
                auto* rendered = rootWindow->findChild<QQuickItem*>(QStringLiteral("reportBody"));
                if (rendered == nullptr || rendered->height() < 120) {
                    qCritical("The report body did not render (height %f)",
                              rendered == nullptr ? -1.0 : rendered->height());
                    QCoreApplication::exit(EXIT_FAILURE);
                }
            });
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
            if (screenshotView == QStringLiteral("pairConversations")) {
                if (QObject* list = rootWindow->findChild<QObject*>(QStringLiteral("chatList"))) {
                    list->setProperty("showingPairs", true);
                }
                return;
            }
            if (QObject* view = rootWindow->findChild<QObject*>(screenshotView)) {
                if (screenshotView == QStringLiteral("settingsPanel"))
                    rootWindow->setProperty("selectedSurface", QStringLiteral("settings"));
                else if (screenshotView == QStringLiteral("teamsPanel"))
                    rootWindow->setProperty("selectedSurface", QStringLiteral("teams"));
                else if (screenshotView == QStringLiteral("updatesPanel"))
                    rootWindow->setProperty("selectedSurface", QStringLiteral("updates"));
                // The rename dialog is normally opened with the chat it targets,
                // so a bare visible=true would capture an empty form.
                if (controller != nullptr && screenshotView == QStringLiteral("renameAgent")) {
                    const QString session = controller->selectedSession();
                    view->setProperty("session", session);
                    view->setProperty("currentName", controller->agentName(session));
                }
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
                if (screenshotView == QStringLiteral("quickSwitcher"))
                    view->setProperty("query", qEnvironmentVariable("CLARP_SCREENSHOT_QUERY"));
                view->setProperty("visible", true);
            }
        });
    }
    // Screenshot-only: open a specific chat, including an agent-conversation
    // projection that no fixture scenario can select on its own.
    if (!screenshotPath.isEmpty() && controller != nullptr
        && qEnvironmentVariableIsSet("CLARP_SCREENSHOT_SELECT_SESSION")) {
        const QString requested = qEnvironmentVariable("CLARP_SCREENSHOT_SELECT_SESSION");
        QTimer::singleShot(1'400, &application, [controller, requested] {
            if (!requested.isEmpty()) {
                controller->selectSession(requested);
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
                                      screenshotScenario == QStringLiteral("links") ||
                                      screenshotScenario == QStringLiteral("report") ||
                                      screenshotScenario == QStringLiteral("tool-spacing") ||
                                      screenshotScenario == QStringLiteral("preview-versions") ||
                                      screenshotScenario == QStringLiteral("team-messages"))) {
                session = QStringLiteral("markdown-fixture");
                controller->agents()->applySnapshot(
                    {{QStringLiteral("agents"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("agent_id"), QStringLiteral("fixture-agent")},
                          {QStringLiteral("session"), session},
                          {QStringLiteral("persona"), QStringLiteral("OPUS")},
                          {QStringLiteral("cwd"), qEnvironmentVariable("CLARP_SCREENSHOT_WORKSPACE")},
                          {QStringLiteral("model"), qEnvironmentVariable("CLARP_SCREENSHOT_MODEL")},
                          {QStringLiteral("effort"), qEnvironmentVariable("CLARP_SCREENSHOT_EFFORT")},
                          {QStringLiteral("status_text"), qEnvironmentVariable("CLARP_SCREENSHOT_STATUS")},
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
            } else if (screenshotScenario == QStringLiteral("team-messages")) {
                controller->setActivityDisplayMode(0);
                turns = {
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("team-user")}, {QStringLiteral("role"), QStringLiteral("user")},
                        {QStringLiteral("text"), QStringLiteral("Can you verify the combined desktop update?")}},
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("team-sender")}, {QStringLiteral("role"), QStringLiteral("user")},
                        {QStringLiteral("origin"), QStringLiteral("agent")}, {QStringLiteral("sender_name"), QStringLiteral("C++ Agent")},
                        {QStringLiteral("sender_agent_id"), QStringLiteral("fixture-agent")}, {QStringLiteral("sender_session"), session},
                        {QStringLiteral("text"), QStringLiteral("The combined client is ready. All desktop checks passed, and the updater owns installation.")}},
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("team-tools")}, {QStringLiteral("role"), QStringLiteral("assistant")},
                        {QStringLiteral("text"), QStringLiteral("I checked the installed build and the new settings.")},
                        {QStringLiteral("timestamp"), QStringLiteral("2020-01-01T10:00:00Z")}, {QStringLiteral("activity_count"), 21}},
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("team-done")}, {QStringLiteral("role"), QStringLiteral("assistant")},
                        {QStringLiteral("timestamp"), QStringLiteral("2020-01-01T10:01:23Z")},
                        {QStringLiteral("text"), QStringLiteral("Verified. Reopen the preview when you are ready.")}}
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
            } else if (screenshotScenario == QStringLiteral("links")) {
                controller->clearError();
                controller->setToolsVisible(true);
                turns = {
                    QJsonObject{{QStringLiteral("id"), QStringLiteral("links-user")},
                                {QStringLiteral("role"), QStringLiteral("user")},
                                {QStringLiteral("text"),
                                 QStringLiteral("Where do I read the release notes?")},
                                {QStringLiteral("revision"), 1}},
                    QJsonObject{
                        {QStringLiteral("id"), QStringLiteral("links-reply")},
                        {QStringLiteral("role"), QStringLiteral("assistant")},
                        {QStringLiteral("text"),
                         QStringLiteral(
                             "A bare URL autolinks: https://example.com/releases/v2?ref=chat\n\n"
                             "A labelled link works too: [the changelog](https://example.com/changelog)\n\n"
                             "So do host-only links (www.example.com) and mail (team@example.com).\n\n"
                             "`https://example.com/in-code` inside code must stay literal text.\n\n"
                             "Open: https://elitebook.tailf14237.ts.net:14443/final/")},
                        {QStringLiteral("revision"), 2},
                        {QStringLiteral("tools"), QJsonArray{QJsonObject{
                            {QStringLiteral("name"), QStringLiteral("Bash")},
                            {QStringLiteral("summary"), QStringLiteral("git push")},
                            {QStringLiteral("command"), QStringLiteral("git push -u origin HEAD")},
                            {QStringLiteral("status"), QStringLiteral("ok")},
                            // Verbatim output keeps its indentation while the URL anchors.
                            {QStringLiteral("result"), QStringLiteral(
                                "  branch published\n\tPR: https://example.com/pr/7 (review it)\n"
                                "  literal <tag> & \"quoted\" survive")}}}}},
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
        if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_VOICE_VIEWPORT"))
            startVoiceViewportSmokeCheck(application, rootWindow, controller);
        if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_OPEN_LINK")) {
            const QString fixtureLink = qEnvironmentVariable("CLARP_SCREENSHOT_OPEN_LINK");
            QTimer::singleShot(1800, &application, [controller, fixtureLink] {
                controller->openExternalLink(fixtureLink, controller->baseUrl());
            });
        }
        if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_READY_REPLY"))
            startReadyReplySmokeCheck(application, rootWindow, controller, screenshotPath);
        if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_CONTEXT_KEYBOARD"))
            startKeyboardSmokeCheck(application, rootWindow, controller);
        if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PAIR_SIDEBAR"))
            startPairSidebarSmokeCheck(application, rootWindow, controller);
        if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_SETTINGS_KEYBOARD") && rootWindow != nullptr) {
            // Deliver keys only to this isolated offscreen Qt window, never the desktop.
            if (QGuiApplication::platformName() != QStringLiteral("offscreen")) return EXIT_FAILURE;
            auto* keyboardCheck = new QTimer(&application);
            keyboardCheck->setInterval(80);
            QObject::connect(keyboardCheck, &QTimer::timeout, &application,
                [&application, rootWindow, keyboardCheck, step = 0]() mutable {
                const auto press = [rootWindow](Qt::Key key, Qt::KeyboardModifiers modifiers = {}) {
                    QKeyEvent down(QEvent::KeyPress, key, modifiers);
                    QKeyEvent up(QEvent::KeyRelease, key, modifiers);
                    QCoreApplication::sendEvent(rootWindow, &down);
                    QCoreApplication::sendEvent(rootWindow, &up);
                };
                const auto focused = [rootWindow](const QString& name) {
                    return rootWindow->activeFocusItem() != nullptr && rootWindow->activeFocusItem()->objectName() == name;
                };
                const auto require = [&application, step](bool condition) {
                    if (!condition) {
                        qCritical("Settings keyboard focus failed at step %d", step);
                        application.exit(EXIT_FAILURE);
                    }
                    return condition;
                };
                QObject* settings = rootWindow->findChild<QObject*>(QStringLiteral("settingsPanel"));
                if (!require(settings != nullptr)) return;
                switch (step++) {
                case 0:
                    press(Qt::Key_Comma, Qt::ControlModifier);
                    break;
                case 1:
                    if (!require(focused(QStringLiteral("setting-timestamps")))) return;
                    press(Qt::Key_End);
                    press(Qt::Key_Return);
                    break;
                case 2:
                    if (!require(settings->property("dialogOpen").toBool() && focused(QStringLiteral("settingsPrimaryProvider")))) return;
                    press(Qt::Key_Escape);
                    break;
                case 3:
                    if (!require(!settings->property("dialogOpen").toBool() && focused(QStringLiteral("setting-voice-routing")))) return;
                    press(Qt::Key_K, Qt::ControlModifier);
                    break;
                case 4: {
                    QObject* picker = rootWindow->findChild<QObject*>(QStringLiteral("quickSwitcher"));
                    if (!require(picker != nullptr && picker->property("visible").toBool())) return;
                    press(Qt::Key_Escape);
                    break;
                }
                case 5:
                    if (!require(focused(QStringLiteral("setting-voice-routing")))) return;
                    press(Qt::Key_Escape);
                    break;
                case 6:
                    if (!require(rootWindow->property("selectedSurface").toString() == QStringLiteral("chats")
                                 && focused(QStringLiteral("paneComposerEditor")))) return;
                    press(Qt::Key_Comma, Qt::ControlModifier);
                    break;
                case 7:
                    if (!require(focused(QStringLiteral("setting-voice-routing")))) return;
                    press(Qt::Key_Home);
                    break;
                default:
                    if (!require(focused(QStringLiteral("setting-timestamps")))) return;
                    rootWindow->setProperty("settingsKeyboardVerified", true);
                    keyboardCheck->stop();
                }
            });
            QTimer::singleShot(2'050, keyboardCheck, [keyboardCheck] { keyboardCheck->start(); });
        }
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
            : qEnvironmentVariableIsSet("CLARP_SCREENSHOT_READY_REPLY") ? 3'000
            : qEnvironmentVariableIsSet("CLARP_SCREENSHOT_CONTEXT_KEYBOARD") ? 4'600
            : qEnvironmentVariableIsSet("CLARP_SCREENSHOT_SETTINGS_KEYBOARD") ? 3'300
            : screenshotScenario.isEmpty() ? 2'000 : 2'400;
        QTimer::singleShot(captureDelay, &application, [&application, rootWindow, screenshotPath, sidebarToggles, sidebarWidth, controller, restoreDesktop, restoreSession] {
            if (rootWindow != nullptr) {
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_VOICE_VIEWPORT") && !rootWindow->property("voiceViewportVerified").toBool()) {
                    qCritical("Voice viewport verification did not complete"); application.exit(EXIT_FAILURE); return;
                }
                if (restoreDesktop && (!controller || controller->selectedSession() != restoreSession || application.property("clarpLaunchMode").toBool())) {
                    qCritical("Update relaunch did not retain the exact conversation or entered new-agent mode");
                    application.exit(EXIT_FAILURE); return;
                }
                if (restoreDesktop) qInfo("Update relaunch preserved session=%s without new-agent mode", qPrintable(restoreSession));
                if (restoreDesktop && qEnvironmentVariableIsSet("CLARP_ADOPTION_TRACE")) {
                    const QString pane = controller->panes()->activePaneId();
                    const QByteArray draft = controller->paneDraft(pane, restoreSession).toUtf8();
                    QSaveFile trace(qEnvironmentVariable("CLARP_ADOPTION_TRACE"));
                    if (trace.open(QIODevice::WriteOnly)) {
                        trace.write(QJsonDocument(QJsonObject{{"session", controller->selectedSession()},
                            {"host", controller->baseUrl()}, {"pane_session", controller->panes()->activeSession()},
                            {"draft_sha256", QString::fromLatin1(QCryptographicHash::hash(draft, QCryptographicHash::Sha256).toHex())},
                            {"attachments", controller->composerAttachments(pane, restoreSession).size()},
                            {"launch_mode", application.property("clarpLaunchMode").toBool()}}).toJson());
                        trace.commit();
                    }
                }
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_MINIMAL_UI")) {
                    auto* button = rootWindow->findChild<QQuickItem*>(QStringLiteral("sidebarHideButton"));
                    const bool minimal = qEnvironmentVariableIntValue("CLARP_SCREENSHOT_MINIMAL_UI") != 0;
                    if (button == nullptr || button->isVisible() == minimal) {
                        qCritical("Minimal UI did not control the sidebar chevron");
                        application.exit(EXIT_FAILURE); return;
                    }
                }
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_READY_REPLY") &&
                    !rootWindow->property("readyReplyVerified").toBool()) {
                    qCritical("Ready reply verification did not complete");
                    application.exit(EXIT_FAILURE); return;
                }
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_CONTEXT_KEYBOARD") &&
                    !rootWindow->property("contextKeyboardVerified").toBool()) {
                    qCritical("Context keyboard verification did not complete");
                    application.exit(EXIT_FAILURE);
                    return;
                }
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_SETTINGS_KEYBOARD") &&
                    !rootWindow->property("settingsKeyboardVerified").toBool()) {
                    qCritical("Settings keyboard verification did not complete");
                    application.exit(EXIT_FAILURE);
                    return;
                }
                if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_TOOL_NARRATION")) {
                    QList<QQuickItem*> remaining{rootWindow->contentItem()};
                    int translatedRows = 0;
                    while (!remaining.isEmpty()) {
                        QQuickItem* item = remaining.takeLast();
                        remaining.append(item->childItems());
                        if (item->objectName() == QStringLiteral("activityExplanationText") &&
                            item->isVisible()) {
                            const QString text = item->property("text").toString();
                            // A failed row now hides this item and shows its tool
                            // call instead, so only the waiting dots can appear here.
                            if (!text.isEmpty() && text != QStringLiteral(".") && text != QStringLiteral("..")
                                && text != QStringLiteral("…")) ++translatedRows;
                        }
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
