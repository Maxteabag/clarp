#include "app/AppController.h"
#include "app/CredentialStore.h"
#include "media/WavEncoder.h"
#include "models/AgentListModel.h"
#include "models/ContactListModel.h"
#include "models/ConversationModel.h"
#include "models/PaneTreeModel.h"
#include "network/SseParser.h"
#include "protocol/ProtocolTypes.h"

#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QSignalSpy>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTest>
#include <QTimer>
#include <QUrl>
#include <QUuid>

using namespace clarp;

namespace {

class FakeClarpServer final : public QTcpServer {
  public:
    explicit FakeClarpServer(QObject* parent = nullptr) : QTcpServer(parent) {
        connect(this, &QTcpServer::newConnection, this, [this] {
            while (hasPendingConnections()) {
                QTcpSocket* socket = nextPendingConnection();
                connect(socket, &QTcpSocket::readyRead, this, [this, socket] {
                    QByteArray buffer = socket->property("requestBuffer").toByteArray();
                    buffer.append(socket->readAll());
                    socket->setProperty("requestBuffer", buffer);
                    const qsizetype headerEnd = buffer.indexOf("\r\n\r\n");
                    if (headerEnd < 0) {
                        return;
                    }
                    const QByteArray headers = buffer.first(headerEnd + 4);
                    qsizetype contentLength = 0;
                    for (const QByteArray& line : headers.split('\n')) {
                        if (line.toLower().startsWith("content-length:")) {
                            contentLength =
                                line.sliced(line.indexOf(':') + 1).trimmed().toLongLong();
                        }
                    }
                    if (buffer.size() < headerEnd + 4 + contentLength) {
                        return;
                    }
                    disconnect(socket, &QTcpSocket::readyRead, this, nullptr);
                    handle(socket, buffer.first(headerEnd + 4 + contentLength));
                });
            }
        });
    }

    bool listenLocal() { return listen(QHostAddress::LocalHost, 0); }

    [[nodiscard]] QString baseUrl() const {
        return QStringLiteral("http://127.0.0.1:%1").arg(serverPort());
    }

    [[nodiscard]] bool sawAuthorization() const { return m_sawAuthorization; }

    [[nodiscard]] bool receivedSend() const { return !m_sentId.isEmpty(); }

    [[nodiscard]] bool scheduleEnabled() const { return m_scheduleEnabled; }

  private:
    static void respond(QTcpSocket* socket, int status, const QJsonObject& object) {
        const QByteArray body = QJsonDocument(object).toJson(QJsonDocument::Compact);
        const QByteArray reason = status == 201 ? QByteArray("Created") : QByteArray("OK");
        QByteArray response = "HTTP/1.1 " + QByteArray::number(status) + ' ' + reason +
                              "\r\nContent-Type: application/json\r\nContent-Length: " +
                              QByteArray::number(body.size()) + "\r\nConnection: close\r\n\r\n" +
                              body;
        socket->write(response);
        socket->disconnectFromHost();
    }

    void handle(QTcpSocket* socket, const QByteArray& request) {
        const qsizetype firstLineEnd = request.indexOf("\r\n");
        const QList<QByteArray> requestLine = request.first(firstLineEnd).split(' ');
        if (requestLine.size() < 2) {
            socket->disconnectFromHost();
            return;
        }
        const QByteArray& path = requestLine.at(1);
        m_sawAuthorization = m_sawAuthorization ||
                             request.contains("Authorization: Bearer test-token") ||
                             request.contains("authorization: Bearer test-token");

        if (path.startsWith("/events")) {
            socket->write("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                          "Cache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n"
                          ": connected\n\nid: 7\ndata: {\"type\":\"agent-roster\"}\n\n");
            socket->flush();
            m_eventSocket = socket;
            return;
        }
        if (path.startsWith("/server-info")) {
            respond(socket, 200,
                    {{QStringLiteral("server_id"), QStringLiteral("test-server")},
                     {QStringLiteral("name"), QStringLiteral("Test Clarp")},
                     {QStringLiteral("deployment_mode"), QStringLiteral("native")},
                     {QStringLiteral("version"), QStringLiteral("1")},
                     {QStringLiteral("default_cwd"), QStringLiteral("/tmp")},
                     {QStringLiteral("clarp_version"), QStringLiteral("test")},
                     {QStringLiteral("min_app_version"), QStringLiteral("0.1.0")},
                     {QStringLiteral("capabilities"),
                      QJsonObject{{QStringLiteral("version"), 1},
                                  {QStringLiteral("features"), QJsonArray{}}}}});
            return;
        }
        if (path.startsWith("/agents/snapshot")) {
            respond(socket, 200,
                    {{QStringLiteral("agents"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("agent_id"), QStringLiteral("agent-rachel")},
                          {QStringLiteral("session"), QStringLiteral("rachel")},
                          {QStringLiteral("persona"), QStringLiteral("Rachel")},
                          {QStringLiteral("backend"), QStringLiteral("codex")},
                          {QStringLiteral("conversation_id"), QStringLiteral("c1")},
                          {QStringLiteral("head_revision"), m_sentId.isEmpty() ? 0 : 1},
                          {QStringLiteral("alive"), true},
                          {QStringLiteral("latest_state"), QStringLiteral("idle")},
                          {QStringLiteral("schedules"),
                           QJsonArray{QJsonObject{
                               {QStringLiteral("schedule_id"), QStringLiteral("sched-test")},
                               {QStringLiteral("name"), QStringLiteral("Daily summary")},
                               {QStringLiteral("cron_expression"), QStringLiteral("0 9 * * *")},
                               {QStringLiteral("prompt"), QStringLiteral("Summarize the day")},
                               {QStringLiteral("enabled"), m_scheduleEnabled},
                           }}},
                      }}},
                     {QStringLiteral("focus"), QStringLiteral("agent-rachel")}});
            return;
        }
        if (path.startsWith("/agent-model-options")) {
            respond(socket, 200,
                    {{QStringLiteral("providers"),
                      QJsonObject{
                          {QStringLiteral("codex"),
                           QJsonObject{
                               {QStringLiteral("label"), QStringLiteral("Codex")},
                               {QStringLiteral("installed"), true},
                               {QStringLiteral("sort_index"), 20},
                               {QStringLiteral("supports_resume"), true},
                               {QStringLiteral("supports_fork"), true},
                               {QStringLiteral("models"),
                                QJsonArray{QJsonObject{
                                    {QStringLiteral("id"), QStringLiteral("gpt-test")},
                                    {QStringLiteral("label"), QStringLiteral("GPT Test")},
                                    {QStringLiteral("supported_efforts"),
                                     QJsonArray{QStringLiteral("low"), QStringLiteral("high")}},
                                }}},
                           }},
                          {QStringLiteral("claude"),
                           QJsonObject{{QStringLiteral("label"), QStringLiteral("Claude")},
                                       {QStringLiteral("installed"), true},
                                       {QStringLiteral("sort_index"), 10}}},
                      }}});
            return;
        }
        if (path.startsWith("/past-sessions")) {
            respond(socket, 200,
                    {{QStringLiteral("sessions"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("id"), QStringLiteral("old-session")},
                          {QStringLiteral("title"), QStringLiteral("Previous work")},
                          {QStringLiteral("cwd"), QStringLiteral("/tmp")},
                      }}}});
            return;
        }
        if (path.startsWith("/dirs")) {
            respond(socket, 200,
                    {{QStringLiteral("matches"),
                      QJsonArray{QStringLiteral("/tmp/Clarp"), QStringLiteral("/tmp/clarp-ios")}}});
            return;
        }
        if (path.startsWith("/favorite-paths")) {
            respond(socket, 200,
                    {{QStringLiteral("paths"),
                      QJsonArray{QJsonObject{{QStringLiteral("path"), QStringLiteral("/tmp/Clarp")},
                                             {QStringLiteral("use_count"), 3}}}}});
            return;
        }
        if (path.startsWith("/agent-schedules/toggle")) {
            const qsizetype bodyStart = request.indexOf("\r\n\r\n") + 4;
            const QJsonObject body = QJsonDocument::fromJson(request.sliced(bodyStart)).object();
            m_scheduleEnabled = body.value(QStringLiteral("enabled")).toBool();
            respond(socket, 200,
                    {{QStringLiteral("ok"), true},
                     {QStringLiteral("schedule"),
                      QJsonObject{{QStringLiteral("schedule_id"), QStringLiteral("sched-test")},
                                  {QStringLiteral("enabled"), m_scheduleEnabled}}}});
            return;
        }
        if (path.startsWith("/log")) {
            QJsonArray turns;
            if (!m_sentId.isEmpty()) {
                turns.append(QJsonObject{
                    {QStringLiteral("id"), QStringLiteral("u-") + m_sentId},
                    {QStringLiteral("role"), QStringLiteral("user")},
                    {QStringLiteral("text"), m_sentText},
                    {QStringLiteral("revision"), 1},
                });
            }
            respond(socket, 200,
                    {{QStringLiteral("conversation_id"), QStringLiteral("c1")},
                     {QStringLiteral("turns"), turns},
                     {QStringLiteral("latest_revision"), m_sentId.isEmpty() ? 0 : 1},
                     {QStringLiteral("has_more"), false},
                     {QStringLiteral("replace_required"), false},
                     {QStringLiteral("missing"), false}});
            return;
        }
        if (path.startsWith("/send")) {
            const qsizetype bodyStart = request.indexOf("\r\n\r\n") + 4;
            const QJsonObject body = QJsonDocument::fromJson(request.sliced(bodyStart)).object();
            m_sentId = body.value(QStringLiteral("client_msg_id")).toString();
            m_sentText = body.value(QStringLiteral("text")).toString();
            respond(socket, 200,
                    {{QStringLiteral("ok"), true},
                     {QStringLiteral("session"), QStringLiteral("rachel")},
                     {QStringLiteral("dispatch"), QStringLiteral("test")},
                     {QStringLiteral("trace_id"), QStringLiteral("trace")}});
            QTimer::singleShot(10, this, [this] {
                if (m_eventSocket != nullptr) {
                    m_eventSocket->write("id: 8\ndata: {\"type\":\"transcript-updated\","
                                         "\"session\":\"rachel\"}\n\n");
                    m_eventSocket->flush();
                }
            });
            return;
        }
        if (path.startsWith("/select")) {
            respond(socket, 200,
                    {{QStringLiteral("ok"), true},
                     {QStringLiteral("session"), QStringLiteral("rachel")}});
            return;
        }
        respond(socket, 200, {{QStringLiteral("ok"), true}});
    }

    QPointer<QTcpSocket> m_eventSocket;
    QString m_sentId;
    QString m_sentText;
    bool m_sawAuthorization = false;
    bool m_scheduleEnabled = true;
};

QJsonObject loadFixture(const QString& relativePath) {
    QFile file(QStringLiteral(CLARP_CONTRACT_DIR) + QStringLiteral("/fixtures/") + relativePath);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    return QJsonDocument::fromJson(file.readAll()).object();
}

QVector<QJsonObject> logSteps(const QJsonObject& fixture) {
    QVector<QJsonObject> logs;
    const QJsonArray steps = fixture.value(QStringLiteral("steps")).toArray();
    for (const auto& step : steps) {
        const QJsonObject log = step.toObject().value(QStringLiteral("log")).toObject();
        if (!log.isEmpty()) {
            logs.append(log);
        }
    }
    return logs;
}

QStringList messageIds(const ConversationModel& model) {
    QStringList ids;
    for (int row = 0; row < model.rowCount(); ++row) {
        ids.append(model.data(model.index(row, 0), ConversationModel::MessageIdRole).toString());
    }
    return ids;
}

QStringList messageBodies(const ConversationModel& model) {
    QStringList bodies;
    for (int row = 0; row < model.rowCount(); ++row) {
        bodies.append(model.data(model.index(row, 0), ConversationModel::BodyRole).toString());
    }
    return bodies;
}

} // namespace

class NativeCoreTest final : public QObject {
    Q_OBJECT

  private slots:
    void sseParserHandlesChunksCommentsAndReplayIds();
    void snapshotFiltersArchivedAgentsAndPatchesEvents();
    void tailThenDeltaMatchesGoldenFixture();
    void growingReplyRejectsStaleRevision();
    void optimisticDeliveryStaysVisibleUntilConfirmed();
    void conversationChangeRequestsReplacement();
    void clipSourcePrecedenceMatchesContract();
    void wavEncodingProducesAValidPcmHeader();
    void paneTreeSplitsClosesNavigatesAndZooms();
    void credentialStoreRoundTrip();
    void appControllerCompletesCoreProtocolFlow();
    void contactsExcludeActivePersonas();
    void microphoneCanCaptureNativePcm();
};

void NativeCoreTest::sseParserHandlesChunksCommentsAndReplayIds() {
    SseParser parser;
    QVERIFY(parser.feed(": connected\r\n\r\nid: 41\r\ndata: {\"type\":\"agent-").isEmpty());
    const QList<SseMessage> messages = parser.feed("roster\"}\r\n\r\n");
    QCOMPARE(messages.size(), 1);
    QCOMPARE(messages.first().id, QStringLiteral("41"));
    QCOMPARE(messages.first().data.value(QStringLiteral("type")).toString(),
             QStringLiteral("agent-roster"));
}

void NativeCoreTest::snapshotFiltersArchivedAgentsAndPatchesEvents() {
    AgentListModel model;
    model.applySnapshot({
        {QStringLiteral("agents"),
         QJsonArray{
             QJsonObject{{QStringLiteral("agent_id"), QStringLiteral("a")},
                         {QStringLiteral("session"), QStringLiteral("rachel")},
                         {QStringLiteral("persona"), QStringLiteral("Rachel")},
                         {QStringLiteral("latest_state"), QStringLiteral("idle")},
                         {QStringLiteral("schedules"),
                          QJsonArray{QJsonObject{
                              {QStringLiteral("schedule_id"), QStringLiteral("sched-one")},
                              {QStringLiteral("enabled"), true}}}}},
             QJsonObject{{QStringLiteral("agent_id"), QStringLiteral("b")},
                         {QStringLiteral("session"), QStringLiteral("old")},
                         {QStringLiteral("archived_at"), 1}},
         }},
    });
    QCOMPARE(model.rowCount(), 1);
    QCOMPARE(model.data(model.index(0, 0), AgentListModel::NameRole).toString(),
             QStringLiteral("Rachel"));
    QCOMPARE(model.data(model.index(0, 0), AgentListModel::SchedulesRole).toList().size(), 1);

    model.applyStateEvent({{QStringLiteral("session"), QStringLiteral("rachel")},
                           {QStringLiteral("kind"), QStringLiteral("thinking")}});
    QVERIFY(model.data(model.index(0, 0), AgentListModel::BusyRole).toBool());

    model.applyNotificationEvent(
        {{QStringLiteral("session"), QStringLiteral("rachel")}, {QStringLiteral("unread"), true}});
    QVERIFY(model.data(model.index(0, 0), AgentListModel::UnreadRole).toBool());
    model.clearUnread(QStringLiteral("rachel"));
    QVERIFY(!model.data(model.index(0, 0), AgentListModel::UnreadRole).toBool());
}

void NativeCoreTest::tailThenDeltaMatchesGoldenFixture() {
    const QJsonObject fixture = loadFixture(QStringLiteral("sync/tail-then-delta.json"));
    const QVector<QJsonObject> logs = logSteps(fixture);
    QCOMPARE(logs.size(), 2);

    ConversationModel model;
    model.openSession(QStringLiteral("rachel"));
    model.applyLog(logs.at(0).value(QStringLiteral("response")).toObject(),
                   ConversationModel::LoadKind::Tail);
    model.applyLog(logs.at(1).value(QStringLiteral("response")).toObject(),
                   ConversationModel::LoadKind::Delta);

    QCOMPARE(model.latestRevision(), 3);
    QCOMPARE(messageIds(model),
             QStringList({QStringLiteral("u-a"), QStringLiteral("m1"), QStringLiteral("m2")}));
}

void NativeCoreTest::growingReplyRejectsStaleRevision() {
    const QVector<QJsonObject> logs =
        logSteps(loadFixture(QStringLiteral("sync/growing-assistant-reply.json")));
    QCOMPARE(logs.size(), 3);

    ConversationModel model;
    model.openSession(QStringLiteral("rachel"));
    model.applyLog(logs.at(0).value(QStringLiteral("response")).toObject(),
                   ConversationModel::LoadKind::Tail);
    model.applyLog(logs.at(1).value(QStringLiteral("response")).toObject(),
                   ConversationModel::LoadKind::Delta);
    model.applyLog(logs.at(2).value(QStringLiteral("response")).toObject(),
                   ConversationModel::LoadKind::Delta);

    QCOMPARE(messageBodies(model), QStringList({QStringLiteral("go"), QStringLiteral("Hello")}));
}

void NativeCoreTest::optimisticDeliveryStaysVisibleUntilConfirmed() {
    const QVector<QJsonObject> logs =
        logSteps(loadFixture(QStringLiteral("delivery/optimistic-bubble-until-filed.json")));
    QCOMPARE(logs.size(), 2);

    ConversationModel model;
    model.openSession(QStringLiteral("rachel"));
    model.applyLog(logs.at(0).value(QStringLiteral("response")).toObject(),
                   ConversationModel::LoadKind::Tail);
    model.addOptimistic(QStringLiteral("a"), QStringLiteral("question"));
    model.addOptimistic(QStringLiteral("b"), QStringLiteral("second"));
    QSignalSpy confirmed(&model, &ConversationModel::deliveryConfirmed);

    model.applyLog(logs.at(1).value(QStringLiteral("response")).toObject(),
                   ConversationModel::LoadKind::Delta);

    QCOMPARE(messageIds(model),
             QStringList({QStringLiteral("m0"), QStringLiteral("u-a"), QStringLiteral("u-b")}));
    QCOMPARE(confirmed.count(), 1);
    QCOMPARE(confirmed.first().first().toString(), QStringLiteral("a"));
    QVERIFY(model.data(model.index(2, 0), ConversationModel::PendingRole).toBool());
}

void NativeCoreTest::conversationChangeRequestsReplacement() {
    ConversationModel model;
    model.openSession(QStringLiteral("rachel"));
    model.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("one")},
                    {QStringLiteral("turns"), QJsonArray{}},
                    {QStringLiteral("latest_revision"), 0}},
                   ConversationModel::LoadKind::Tail);
    QSignalSpy replacement(&model, &ConversationModel::replacementRequired);
    model.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("two")},
                    {QStringLiteral("turns"), QJsonArray{}},
                    {QStringLiteral("latest_revision"), 1}},
                   ConversationModel::LoadKind::Delta);
    QCOMPARE(replacement.count(), 1);
}

void NativeCoreTest::clipSourcePrecedenceMatchesContract() {
    const QJsonArray steps = loadFixture(QStringLiteral("audio/clip-precedence.json"))
                                 .value(QStringLiteral("steps"))
                                 .toArray();
    QCOMPARE(AudioClip::fromJson(steps.at(0).toObject().value(QStringLiteral("clip")).toObject())
                 .preferredSource(),
             QStringLiteral("/audio/a.mp3"));
    QCOMPARE(AudioClip::fromJson(steps.at(1).toObject().value(QStringLiteral("clip")).toObject())
                 .preferredSource(),
             QStringLiteral("/clips/2/stream"));
    QCOMPARE(AudioClip::fromJson(steps.at(2).toObject().value(QStringLiteral("clip")).toObject())
                 .preferredSource(),
             QStringLiteral("/clips/3/list.m3u8"));
    QVERIFY(AudioClip::fromJson(steps.at(3).toObject().value(QStringLiteral("clip")).toObject())
                .preferredSource()
                .isEmpty());
}

void NativeCoreTest::wavEncodingProducesAValidPcmHeader() {
    QAudioFormat format;
    format.setSampleRate(16'000);
    format.setChannelCount(1);
    format.setSampleFormat(QAudioFormat::Int16);
    const QByteArray pcm(3'200, '\0');
    const QByteArray wav = encodeWav(pcm, format);

    QCOMPARE(wav.size(), pcm.size() + 44);
    QCOMPARE(wav.first(4), QByteArray("RIFF"));
    QCOMPARE(wav.sliced(8, 4), QByteArray("WAVE"));
    QCOMPARE(wav.sliced(12, 4), QByteArray("fmt "));
    QCOMPARE(wav.sliced(36, 4), QByteArray("data"));
}

void NativeCoreTest::paneTreeSplitsClosesNavigatesAndZooms() {
    PaneTreeModel panes;
    panes.setActiveSession(QStringLiteral("rachel"));
    const QString firstId = panes.activePaneId();
    QCOMPARE(panes.paneCount(), 1);
    QCOMPARE(panes.activeSession(), QStringLiteral("rachel"));

    panes.splitActive(QStringLiteral("vertical"), QStringLiteral("bella"));
    QCOMPARE(panes.paneCount(), 2);
    QCOMPARE(panes.activeSession(), QStringLiteral("bella"));
    QCOMPARE(panes.rootNode().value(QStringLiteral("direction")).toString(),
             QStringLiteral("vertical"));

    panes.splitActive(QStringLiteral("horizontal"), QStringLiteral("adam"));
    QCOMPARE(panes.paneCount(), 3);
    QCOMPARE(panes.activeSession(), QStringLiteral("adam"));
    panes.navigate(QStringLiteral("left"));
    QCOMPARE(panes.activeSession(), QStringLiteral("bella"));

    panes.toggleZoom();
    QCOMPARE(panes.zoomedPaneId(), panes.activePaneId());
    QCOMPARE(panes.displayRoot().value(QStringLiteral("kind")).toString(), QStringLiteral("leaf"));
    panes.toggleZoom();
    QVERIFY(panes.zoomedPaneId().isEmpty());

    panes.closePane(firstId);
    QCOMPARE(panes.paneCount(), 2);
    panes.equalize();
    panes.resizeActive(0.1);
    const QVariantMap root = panes.rootNode();
    QVERIFY(root.value(QStringLiteral("ratio")).toDouble() >= 0.15);
    QVERIFY(root.value(QStringLiteral("ratio")).toDouble() <= 0.85);
}

void NativeCoreTest::credentialStoreRoundTrip() {
    if (!qEnvironmentVariableIsSet("CLARP_TEST_SECRET_SERVICE")) {
        QSKIP("Set CLARP_TEST_SECRET_SERVICE=1 to exercise the desktop keyring");
    }
    CredentialStore credentials;
    const QString serverUrl = QStringLiteral("https://credential-test.invalid/") +
                              QUuid::createUuid().toString(QUuid::WithoutBraces);
    const QString token = QStringLiteral("cld_test_native_desktop_credential");

    QSignalSpy stored(&credentials, &CredentialStore::storeFinished);
    credentials.store(serverUrl, token);
    QVERIFY(stored.wait(3'000));

    QSignalSpy lookedUp(&credentials, &CredentialStore::lookupFinished);
    credentials.lookup(serverUrl);
    QVERIFY(lookedUp.wait(3'000));
    QCOMPARE(lookedUp.first().at(0).toString(), serverUrl);
    QCOMPARE(lookedUp.first().at(1).toString(), token);

    QSignalSpy removed(&credentials, &CredentialStore::removeFinished);
    credentials.remove(serverUrl);
    QVERIFY(removed.wait(3'000));
}

void NativeCoreTest::appControllerCompletesCoreProtocolFlow() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8());
    qputenv("CLARP_TOKEN", "test-token");

    AppController controller;
    QTRY_COMPARE_WITH_TIMEOUT(controller.agents()->rowCount(), 1, 3'000);
    QTRY_VERIFY_WITH_TIMEOUT(controller.connected(), 3'000);
    QCOMPARE(controller.selectedSession(), QStringLiteral("rachel"));
    QTRY_COMPARE_WITH_TIMEOUT(controller.backendOptions().size(), 2, 3'000);
    QCOMPARE(controller.backendOptions().first().toMap().value(QStringLiteral("id")).toString(),
             QStringLiteral("claude"));
    QVERIFY(controller.backendSupportsResume(QStringLiteral("codex")));
    QCOMPARE(controller.modelsForBackend(QStringLiteral("codex")).size(), 2);
    QCOMPARE(controller.effortsForModel(QStringLiteral("codex"), QStringLiteral("gpt-test")).size(),
             3);

    controller.loadPastSessions(QStringLiteral("/tmp"), QStringLiteral("codex"));
    QTRY_COMPARE_WITH_TIMEOUT(controller.pastSessions().size(), 1, 3'000);
    controller.loadDirectorySuggestions(QStringLiteral("/tmp/c"));
    controller.loadFavoritePaths();
    QTRY_COMPARE_WITH_TIMEOUT(controller.directorySuggestions().size(), 2, 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.favoritePaths().size(), 1, 3'000);

    controller.setScheduleEnabled(QStringLiteral("sched-test"), false);
    QTRY_VERIFY_WITH_TIMEOUT(!server.scheduleEnabled(), 3'000);

    controller.sendMessage(QStringLiteral("hello from native integration"));
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedSend(), 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.conversation()->rowCount(), 1, 3'000);
    QTRY_VERIFY_WITH_TIMEOUT(!controller.sending(), 3'000);
    QCOMPARE(controller.conversation()
                 ->data(controller.conversation()->index(0, 0), ConversationModel::BodyRole)
                 .toString(),
             QStringLiteral("hello from native integration"));
    QVERIFY(server.sawAuthorization());

    qunsetenv("CLARP_BASE_URL");
    qunsetenv("CLARP_TOKEN");
}

void NativeCoreTest::contactsExcludeActivePersonas() {
    ContactListModel contacts;
    contacts.applySnapshot({{QStringLiteral("personas"),
                             QJsonArray{
                                 QJsonObject{{QStringLiteral("id"), QStringLiteral("one")},
                                             {QStringLiteral("name"), QStringLiteral("Rachel")}},
                                 QJsonObject{{QStringLiteral("id"), QStringLiteral("two")},
                                             {QStringLiteral("name"), QStringLiteral("Bella")},
                                             {QStringLiteral("personality"),
                                              QStringLiteral("Personality: Thoughtful")}},
                             }}},
                           {QStringLiteral("rachel")});
    QCOMPARE(contacts.rowCount(), 1);
    QCOMPARE(contacts.data(contacts.index(0, 0), ContactListModel::NameRole).toString(),
             QStringLiteral("Bella"));
    QCOMPARE(contacts.data(contacts.index(0, 0), ContactListModel::DescriptionRole).toString(),
             QStringLiteral("Thoughtful"));
}

void NativeCoreTest::microphoneCanCaptureNativePcm() {
    if (!qEnvironmentVariableIsSet("CLARP_TEST_AUDIO_DEVICE")) {
        QSKIP("Set CLARP_TEST_AUDIO_DEVICE=1 to exercise the default microphone");
    }
    AudioController audio;
    QSignalSpy errors(&audio, &AudioController::mediaError);
    audio.startRecording();
    QTRY_VERIFY_WITH_TIMEOUT(audio.recording(), 2'000);
    QTest::qWait(250);
    audio.cancelRecording();
    QVERIFY(!audio.recording());
    QCOMPARE(errors.count(), 0);
}

QTEST_MAIN(NativeCoreTest)

#include "tst_native_core.moc"
