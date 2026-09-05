#include "app/AppController.h"
#include "app/CredentialStore.h"
#include "app/TranscriptCache.h"
#include "media/WavEncoder.h"
#include "models/AgentListModel.h"
#include "models/ContactListModel.h"
#include "models/ConversationModel.h"
#include "models/PaneTreeModel.h"
#include "network/ApiClient.h"
#include "network/SseParser.h"
#include "protocol/ProtocolTypes.h"

#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QJsonArray>
#include <QJsonDocument>
#include <QSignalSpy>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTest>
#include <QTimer>
#include <QTemporaryFile>
#include <QTemporaryDir>
#include <QUrl>
#include <QUuid>
#include <cmath>

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

    [[nodiscard]] bool receivedRequest(const QString& method, const QString& path) const {
        return m_requests.contains(method + u' ' + path);
    }

    [[nodiscard]] QJsonObject requestJson(const QString& method, const QString& path) const {
        return QJsonDocument::fromJson(m_requestBodies.value(method + u' ' + path)).object();
    }

    void holdLogRequests(bool hold) { m_holdLogs = hold; }

    void holdUploadRequests(bool hold) { m_holdUploads = hold; }

    [[nodiscard]] bool hasHeldLogRequest() const { return m_heldLogSocket != nullptr; }

    void releaseHeldLogRequest() {
        m_holdLogs = false;
        if (m_heldLogSocket != nullptr) {
            respond(m_heldLogSocket, 200,
                    {{QStringLiteral("conversation_id"), QStringLiteral("c1")},
                     {QStringLiteral("turns"), QJsonArray{}},
                     {QStringLiteral("latest_revision"), 0},
                     {QStringLiteral("has_more"), false}});
            m_heldLogSocket = nullptr;
        }
    }

    [[nodiscard]] bool hasHeldUploadRequest() const { return m_heldUploadSocket != nullptr; }

    void releaseHeldUploadRequest() {
        m_holdUploads = false;
        if (m_heldUploadSocket != nullptr) {
            respond(m_heldUploadSocket, 200,
                    {{QStringLiteral("path"), QStringLiteral("/remote/uploads/file.txt")},
                     {QStringLiteral("name"), QStringLiteral("file.txt")}});
            m_heldUploadSocket = nullptr;
        }
    }

    void sendEvent(const QJsonObject& event) {
        if (m_eventSocket != nullptr) {
            const QByteArray data = QJsonDocument(event).toJson(QJsonDocument::Compact);
            m_eventSocket->write("data: " + data + "\n\n");
            m_eventSocket->flush();
        }
    }

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

    static void respondBytes(QTcpSocket* socket, const QByteArray& body,
                             const QByteArray& contentType) {
        const QByteArray response = "HTTP/1.1 200 OK\r\nContent-Type: " + contentType +
                                    "\r\nContent-Length: " + QByteArray::number(body.size()) +
                                    "\r\nConnection: close\r\n\r\n" + body;
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
        const QString requestKey = QString::fromUtf8(requestLine.at(0)) + u' ' +
                                   QString::fromUtf8(path).section(u'?', 0, 0);
        m_requests.append(requestKey);
        const qsizetype requestBodyStart = request.indexOf("\r\n\r\n") + 4;
        m_requestBodies.insert(requestKey, request.sliced(requestBodyStart));
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
                          {QStringLiteral("mcp_servers"),
                           QJsonArray{QStringLiteral("github")}},
                          {QStringLiteral("team_ids"),
                           QJsonArray{QStringLiteral("team-1")}},
                          {QStringLiteral("schedules"),
                           QJsonArray{QJsonObject{
                               {QStringLiteral("schedule_id"), QStringLiteral("sched-test")},
                               {QStringLiteral("name"), QStringLiteral("Daily summary")},
                               {QStringLiteral("cron_expression"), QStringLiteral("0 9 * * *")},
                               {QStringLiteral("prompt"), QStringLiteral("Summarize the day")},
                               {QStringLiteral("enabled"), m_scheduleEnabled},
                           }}},
                      }}},
                     {QStringLiteral("focus"), QStringLiteral("agent-rachel")},
                     {QStringLiteral("available_mcp_servers"),
                      QJsonArray{QStringLiteral("github"), QStringLiteral("figma")}}});
            return;
        }
        if (path.startsWith("/static/avatars/rachel.png")) {
            respondBytes(socket, QByteArray("\x89PNG\r\n", 6), QByteArray("image/png"));
            return;
        }
        if (path == "/transcribe") {
            ++m_transcribeCount;
            respond(socket, 200,
                    {{QStringLiteral("text"),
                      QStringLiteral("Transcript %1").arg(m_transcribeCount)},
                     {QStringLiteral("trace_id"),
                      QStringLiteral("voice-trace-%1").arg(m_transcribeCount)},
                     {QStringLiteral("transcription_id"),
                      QStringLiteral("transcription-%1").arg(m_transcribeCount)},
                     {QStringLiteral("hands_free"), false}});
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
        if (path == "/upload") {
            if (m_holdUploads) {
                m_heldUploadSocket = socket;
            } else {
                respond(socket, 200,
                        {{QStringLiteral("path"), QStringLiteral("/remote/uploads/file.txt")},
                         {QStringLiteral("name"), QStringLiteral("file.txt")}});
            }
            return;
        }
        if (path == "/turn-queue" || path.startsWith("/turn-queue?")) {
            respond(socket, 200,
                    {{QStringLiteral("items"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("id"), QStringLiteral("queue-1")},
                          {QStringLiteral("text"), QStringLiteral("Follow up with tests")},
                          {QStringLiteral("enqueued_at"), 1'788'000'000'000.0},
                      }}},
                     {QStringLiteral("paused"), false},
                     {QStringLiteral("revision"), 2}});
            return;
        }
        if (path.startsWith("/task-plan")) {
            respond(socket, 200,
                    {{QStringLiteral("plan"),
                      QJsonObject{
                          {QStringLiteral("plan_id"), QStringLiteral("plan-1")},
                          {QStringLiteral("title"), QStringLiteral("Replicate iOS behavior")},
                          {QStringLiteral("completed_count"), 1},
                          {QStringLiteral("total_count"), 2},
                          {QStringLiteral("items"),
                           QJsonArray{
                               QJsonObject{{QStringLiteral("item_id"), QStringLiteral("item-1")},
                                           {QStringLiteral("title"), QStringLiteral("Stable streaming")},
                                           {QStringLiteral("status"), QStringLiteral("completed")}},
                               QJsonObject{{QStringLiteral("item_id"), QStringLiteral("item-2")},
                                           {QStringLiteral("title"), QStringLiteral("Visual QA")},
                                           {QStringLiteral("status"), QStringLiteral("in_progress")}},
                           }},
                      }}});
            return;
        }
        if (path.startsWith("/identity/prompt-history")) {
            respond(socket, 200,
                    {{QStringLiteral("prompts"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("turn_id"), QStringLiteral("prompt-1")},
                          {QStringLiteral("text"), QStringLiteral("Make the desktop intuitive")},
                          {QStringLiteral("preview"), QStringLiteral("Make the desktop intuitive")},
                          {QStringLiteral("content_status"), QStringLiteral("available")},
                          {QStringLiteral("created_at"), QStringLiteral("2026-09-04T18:00:00Z")},
                          {QStringLiteral("prompt_origin"),
                           QJsonObject{{QStringLiteral("channel"), QStringLiteral("chat")}}},
                      }}},
                     {QStringLiteral("page"),
                      QJsonObject{{QStringLiteral("has_more"), false},
                                  {QStringLiteral("next_before"), QJsonValue::Null}}}});
            return;
        }
        if (path.startsWith("/agent-heartbeat/status")) {
            respond(socket, 200,
                    {{QStringLiteral("session"), QStringLiteral("rachel")},
                     {QStringLiteral("schedule"),
                      QJsonObject{{QStringLiteral("enabled"), true},
                                  {QStringLiteral("dormant"), false},
                                  {QStringLiteral("next_heartbeat_at"), 1'788'000'060'000.0}}},
                     {QStringLiteral("history"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("id"), QStringLiteral("heartbeat-1")},
                          {QStringLiteral("text"), QStringLiteral("Checked the current work")},
                          {QStringLiteral("updated_at"), 1'788'000'000'000.0},
                      }}}});
            return;
        }
        if (path.startsWith("/diagnostics/health")) {
            respond(socket, 200,
                    {{QStringLiteral("ready"), true},
                     {QStringLiteral("checks"),
                      QJsonObject{{QStringLiteral("stt_ready"), true},
                                  {QStringLiteral("ffmpeg_ready"), true}}},
                     {QStringLiteral("tts_queue"),
                      QJsonObject{{QStringLiteral("pending"), 0},
                                  {QStringLiteral("in_flight"), 0}}}});
            return;
        }
        if (path.startsWith("/transcription-capabilities")) {
            respond(socket, 200,
                    {{QStringLiteral("available"), true},
                     {QStringLiteral("default_model"), QStringLiteral("faster-whisper:small.en")},
                     {QStringLiteral("models"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("id"), QStringLiteral("faster-whisper:small.en")},
                          {QStringLiteral("name"), QStringLiteral("Small English")},
                      }}}});
            return;
        }
        if (path.startsWith("/tts/providers")) {
            respond(socket, 200,
                    {{QStringLiteral("provider"), QStringLiteral("cartesia")},
                     {QStringLiteral("fallback"), QStringLiteral("elevenlabs")},
                     {QStringLiteral("providers"),
                      QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("cartesia")},
                                             {QStringLiteral("available"), true}}}}});
            return;
        }
        if (path == "/media" || path.startsWith("/media?")) {
            respond(socket, 200,
                    {{QStringLiteral("session"), QStringLiteral("rachel")},
                     {QStringLiteral("assets"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("asset_id"), QStringLiteral("asset-1")},
                          {QStringLiteral("session"), QStringLiteral("rachel")},
                          {QStringLiteral("source_name"), QStringLiteral("result.png")},
                          {QStringLiteral("mime_type"), QStringLiteral("image/png")},
                          {QStringLiteral("caption"), QStringLiteral("Rendered result")},
                          {QStringLiteral("url"), QStringLiteral("/media/asset-1")},
                          {QStringLiteral("width"), 1},
                          {QStringLiteral("height"), 1},
                      }}}});
            return;
        }
        if (path == "/media/asset-1") {
            respondBytes(socket, QByteArray("\x89PNG\r\n\x1a\nfixture", 15),
                         QByteArray("image/png"));
            return;
        }
        if (path.startsWith("/attention")) {
            respond(socket, 200,
                    {{QStringLiteral("items"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("decision_id"), QStringLiteral("decision-1")},
                          {QStringLiteral("revision"), 4},
                          {QStringLiteral("title"), QStringLiteral("Ship preview?")},
                          {QStringLiteral("question"), QStringLiteral("Promote the verified build?")},
                          {QStringLiteral("session"), QStringLiteral("rachel")},
                      }}},
                     {QStringLiteral("count"), 1}});
            return;
        }
        if (path.startsWith("/background-jobs")) {
            respond(socket, 200,
                    {{QStringLiteral("jobs"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("job_id"), QStringLiteral("job-1")},
                          {QStringLiteral("title"), QStringLiteral("Native verification")},
                          {QStringLiteral("status"), QStringLiteral("running")},
                          {QStringLiteral("can_cancel"), true},
                          {QStringLiteral("metadata"),
                           QJsonObject{{QStringLiteral("completed"), 3},
                                       {QStringLiteral("total"), 10}}},
                      }}}});
            return;
        }
        if (path.startsWith("/artifacts")) {
            respond(socket, 200,
                    {{QStringLiteral("artifacts"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("artifact_id"), QStringLiteral("artifact-1")},
                          {QStringLiteral("type"), QStringLiteral("document")},
                          {QStringLiteral("title"), QStringLiteral("Parity report")},
                          {QStringLiteral("session"), QStringLiteral("rachel")},
                      }}}});
            return;
        }
        if (path.startsWith("/teams/team-1/messages")) {
            respond(socket, 200,
                    {{QStringLiteral("team_id"), QStringLiteral("team-1")},
                     {QStringLiteral("messages"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("message_id"), QStringLiteral("team-message-1")},
                          {QStringLiteral("source_name"), QStringLiteral("Rachel")},
                          {QStringLiteral("source_session"), QStringLiteral("rachel")},
                          {QStringLiteral("text"), QStringLiteral("Desktop parity is ready to inspect.")},
                      }}}});
            return;
        }
        if (path == "/teams") {
            respond(socket, 200,
                    {{QStringLiteral("teams"),
                      QJsonArray{QJsonObject{
                          {QStringLiteral("team_id"), QStringLiteral("team-1")},
                          {QStringLiteral("name"), QStringLiteral("Desktop crew")},
                          {QStringLiteral("color"), QStringLiteral("#596083")},
                          {QStringLiteral("leader_agent_id"), QStringLiteral("agent-rachel")},
                          {QStringLiteral("nudge_enabled"), false},
                          {QStringLiteral("member_agent_ids"),
                           QJsonArray{QStringLiteral("agent-rachel")}},
                      }}}});
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
            if (m_holdLogs) {
                m_heldLogSocket = socket;
                return;
            }
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
    QStringList m_requests;
    QHash<QString, QByteArray> m_requestBodies;
    QPointer<QTcpSocket> m_heldLogSocket;
    QPointer<QTcpSocket> m_heldUploadSocket;
    bool m_holdLogs = false;
    bool m_holdUploads = false;
    int m_transcribeCount = 0;
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
    void sseCursorIsScopedToOneHost();
    void snapshotFiltersArchivedAgentsAndPatchesEvents();
    void agentSnapshotDiffsInPlaceAndRejectsStaleState();
    void tailThenDeltaMatchesGoldenFixture();
    void streamingRowsUpdateInPlaceAndRetireWhenFinalized();
    void activityRowsUpdateInPlaceBySemanticIdentity();
    void olderHistoryPrependsWithoutReorderingTheTail();
    void growingReplyRejectsStaleRevision();
    void optimisticDeliveryStaysVisibleUntilConfirmed();
    void conversationChangeRequestsReplacement();
    void clipSourcePrecedenceMatchesContract();
    void wavEncodingProducesAValidPcmHeader();
    void paneTreeSplitsClosesNavigatesAndZooms();
    void apiClientRejectsCrossOriginAuthenticatedMedia();
    void apiClientDropsRepliesFromPreviousEndpointGeneration();
    void paneDraftAndFocusSurviveLayoutStateChanges();
    void paneDraftIsDurableAndScopedToServerAndConversation();
    void transcriptCacheRestoresDurableRowsWithoutStaleRegression();
    void credentialStoreRoundTrip();
    void appControllerCompletesCoreProtocolFlow();
    void connectedControllerShutsDownWithoutLateSseCallbacks();
    void contactsExcludeActivePersonas();
    void microphoneCanCaptureNativePcm();
    void backgroundTranscriptionsKeepTheirChatOwnership();
    void markdownParagraphsBecomeVisibleDisplayBlocks();
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

void NativeCoreTest::markdownParagraphsBecomeVisibleDisplayBlocks() {
    QCOMPARE(markdownDisplayBlocks(QStringLiteral("First paragraph.\n\nSecond paragraph.")),
             QStringList({QStringLiteral("First paragraph."),
                          QStringLiteral("Second paragraph.")}));
    QCOMPARE(markdownDisplayBlocks(QStringLiteral("One visual line\nsoft continuation")),
             QStringList({QStringLiteral("One visual line\nsoft continuation")}));
    QCOMPARE(markdownDisplayBlocks(QStringLiteral("1. First item\n\n\n2. Second item")),
             QStringList({QStringLiteral("1. First item\n\n2. Second item")}));
    QCOMPARE(markdownDisplayBlocks(QStringLiteral("- First item\r\n\r\n- Second item")),
             QStringList({QStringLiteral("- First item\n\n- Second item")}));
    QCOMPARE(markdownDisplayBlocks(
                 QStringLiteral("```text\nfirst line\n\nsecond line\n```\n\nAfter code.")),
             QStringList({QStringLiteral("```text\nfirst line\n\nsecond line\n```"),
                          QStringLiteral("After code.")}));
}

void NativeCoreTest::sseCursorIsScopedToOneHost() {
    SseClient client;
    client.setEndpoint(QUrl(QStringLiteral("https://one.example")), QStringLiteral("token"));
    client.setLastEventId(QStringLiteral("42"));
    client.setEndpoint(QUrl(QStringLiteral("https://one.example")), QStringLiteral("new-token"));
    QCOMPARE(client.lastEventId(), QStringLiteral("42"));
    client.setEndpoint(QUrl(QStringLiteral("https://two.example")), QStringLiteral("new-token"));
    QVERIFY(client.lastEventId().isEmpty());
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
    model.markTransportUnavailable();
    QCOMPARE(model.data(model.index(0, 0), AgentListModel::StateRole).toString(),
             QStringLiteral("offline"));
    QVERIFY(!model.data(model.index(0, 0), AgentListModel::BusyRole).toBool());
}

void NativeCoreTest::agentSnapshotDiffsInPlaceAndRejectsStaleState() {
    const auto agent = [](const QString& id, const QString& session, qint64 activity,
                          qint64 stateTimestamp, const QString& state) {
        return QJsonObject{{QStringLiteral("agent_id"), id},
                           {QStringLiteral("session"), session},
                           {QStringLiteral("persona"), session.toUpper()},
                           {QStringLiteral("last_activity"), activity},
                           {QStringLiteral("latest_state_ts"), stateTimestamp},
                           {QStringLiteral("latest_state"), state}};
    };
    AgentListModel model;
    model.applySnapshot(
        {{QStringLiteral("agents"),
          QJsonArray{agent(QStringLiteral("a"), QStringLiteral("rachel"), 100, 100,
                           QStringLiteral("idle")),
                     agent(QStringLiteral("b"), QStringLiteral("mike"), 200, 100,
                           QStringLiteral("idle"))}}});
    QCOMPARE(model.data(model.index(0, 0), AgentListModel::SessionRole).toString(),
             QStringLiteral("mike"));

    model.applyStateEvent({{QStringLiteral("session"), QStringLiteral("rachel")},
                           {QStringLiteral("kind"), QStringLiteral("thinking")},
                           {QStringLiteral("status_text"), QStringLiteral("Working")},
                           {QStringLiteral("ts"), 500}});
    QSignalSpy resets(&model, &QAbstractItemModel::modelReset);
    QSignalSpy moves(&model, &QAbstractItemModel::rowsMoved);
    model.applySnapshot(
        {{QStringLiteral("agents"),
          QJsonArray{agent(QStringLiteral("a"), QStringLiteral("rachel"), 300, 400,
                           QStringLiteral("idle")),
                     agent(QStringLiteral("b"), QStringLiteral("mike"), 200, 100,
                           QStringLiteral("idle"))}}});

    QCOMPARE(resets.count(), 0);
    QCOMPARE(moves.count(), 1);
    QCOMPARE(model.data(model.index(0, 0), AgentListModel::SessionRole).toString(),
             QStringLiteral("rachel"));
    QCOMPARE(model.data(model.index(0, 0), AgentListModel::StateRole).toString(),
             QStringLiteral("thinking"));
    QCOMPARE(model.data(model.index(0, 0), AgentListModel::StatusTextRole).toString(),
             QStringLiteral("Working"));

    AgentListModel relaunched;
    relaunched.applySnapshot(
        {{QStringLiteral("agents"),
          QJsonArray{QJsonObject{{QStringLiteral("agent_id"), QStringLiteral("a")},
                                 {QStringLiteral("session"), QStringLiteral("rachel")},
                                 {QStringLiteral("conversation_id"), QStringLiteral("old")},
                                 {QStringLiteral("head_revision"), 8},
                                 {QStringLiteral("last_message"), QStringLiteral("Old answer")}}}}});
    relaunched.applySnapshot(
        {{QStringLiteral("agents"),
          QJsonArray{QJsonObject{{QStringLiteral("agent_id"), QStringLiteral("a")},
                                 {QStringLiteral("session"), QStringLiteral("rachel")},
                                 {QStringLiteral("conversation_id"), QString{}},
                                 {QStringLiteral("head_revision"), 0},
                                 {QStringLiteral("last_message"), QString{}}}}}});
    QCOMPARE(relaunched.data(relaunched.index(0, 0), AgentListModel::ConversationIdRole)
                 .toString(),
             QString{});
    QCOMPARE(relaunched.data(relaunched.index(0, 0), AgentListModel::HeadRevisionRole)
                 .toLongLong(),
             0);
    QCOMPARE(relaunched.data(relaunched.index(0, 0), AgentListModel::LastMessageRole)
                 .toString(),
             QString{});

    AgentListModel queues;
    queues.applyQueueEvent({{QStringLiteral("session"), QStringLiteral("rachel")},
                            {QStringLiteral("queue_depth"), 4},
                            {QStringLiteral("queue_revision"), 3}});
    queues.applySnapshot(
        {{QStringLiteral("agents"),
          QJsonArray{QJsonObject{{QStringLiteral("agent_id"), QStringLiteral("a")},
                                 {QStringLiteral("session"), QStringLiteral("rachel")},
                                 {QStringLiteral("queued_turn_count"), 0},
                                 {QStringLiteral("queue_revision"), 1}}}}});
    QCOMPARE(queues.data(queues.index(0, 0), AgentListModel::QueueCountRole).toInt(), 4);
    queues.applyQueueEvent({{QStringLiteral("session"), QStringLiteral("rachel")},
                            {QStringLiteral("queue_depth"), 1},
                            {QStringLiteral("queue_revision"), 2}});
    QCOMPARE(queues.data(queues.index(0, 0), AgentListModel::QueueCountRole).toInt(), 4);
    queues.applyQueueEvent({{QStringLiteral("session"), QStringLiteral("rachel")},
                            {QStringLiteral("queue_depth"), 5},
                            {QStringLiteral("queue_revision"), 4}});
    QCOMPARE(queues.data(queues.index(0, 0), AgentListModel::QueueCountRole).toInt(), 5);

    AgentListModel outgoing;
    outgoing.applySnapshot(
        {{QStringLiteral("agents"),
          QJsonArray{agent(QStringLiteral("a"), QStringLiteral("rachel"), 100, 100,
                           QStringLiteral("idle")),
                     agent(QStringLiteral("b"), QStringLiteral("mike"), 200, 100,
                           QStringLiteral("idle"))}}});
    QCOMPARE(outgoing.data(outgoing.index(0, 0), AgentListModel::SessionRole).toString(),
             QStringLiteral("mike"));
    QVERIFY(outgoing.recordOutgoingActivity(QStringLiteral("rachel")));
    QCOMPARE(outgoing.data(outgoing.index(0, 0), AgentListModel::SessionRole).toString(),
             QStringLiteral("rachel"));
    QCOMPARE(outgoing.data(outgoing.index(0, 0), AgentListModel::LastMessageRole).toString(),
             QString{});
    outgoing.applySnapshot(
        {{QStringLiteral("agents"),
          QJsonArray{agent(QStringLiteral("a"), QStringLiteral("rachel"), 250, 100,
                           QStringLiteral("idle")),
                     agent(QStringLiteral("b"), QStringLiteral("mike"), 300, 100,
                           QStringLiteral("idle"))}}});
    QCOMPARE(outgoing.data(outgoing.index(0, 0), AgentListModel::SessionRole).toString(),
             QStringLiteral("mike"));
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

void NativeCoreTest::streamingRowsUpdateInPlaceAndRetireWhenFinalized() {
    ConversationModel model;
    model.openSession(QStringLiteral("streaming"));
    model.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("conversation-1")},
                    {QStringLiteral("turns"),
                     QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("live-1")},
                                            {QStringLiteral("role"), QStringLiteral("assistant")},
                                            {QStringLiteral("kind"), QStringLiteral("live")},
                                            {QStringLiteral("text"), QStringLiteral("Hello")},
                                            {QStringLiteral("revision"), 1}}}},
                    {QStringLiteral("latest_revision"), 1}},
                   ConversationModel::LoadKind::Tail);

    QSignalSpy resets(&model, &QAbstractItemModel::modelReset);
    QSignalSpy layouts(&model, &QAbstractItemModel::layoutChanged);
    QSignalSpy inserts(&model, &QAbstractItemModel::rowsInserted);
    QSignalSpy removes(&model, &QAbstractItemModel::rowsRemoved);
    QSignalSpy changes(&model, &QAbstractItemModel::dataChanged);
    QSignalSpy counts(&model, &ConversationModel::countChanged);

    model.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation-1")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("live-1")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("kind"), QStringLiteral("live")},
                                 {QStringLiteral("text"), QStringLiteral("Hello world <spe")},
                                 {QStringLiteral("revision"), 2}}}},
         {QStringLiteral("latest_revision"), 2}},
        ConversationModel::LoadKind::Delta);

    QCOMPARE(model.rowCount(), 1);
    QCOMPARE(model.data(model.index(0, 0), ConversationModel::BodyRole).toString(),
             QStringLiteral("Hello world"));
    QCOMPARE(changes.count(), 1);
    QCOMPARE(resets.count(), 0);
    QCOMPARE(layouts.count(), 0);
    QCOMPARE(inserts.count(), 0);
    QCOMPARE(removes.count(), 0);
    QCOMPARE(counts.count(), 0);

    model.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("conversation-1")},
                    {QStringLiteral("turns"),
                     QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("final-1")},
                                            {QStringLiteral("role"), QStringLiteral("assistant")},
                                            {QStringLiteral("kind"), QStringLiteral("assistant")},
                                            {QStringLiteral("text"), QStringLiteral("Hello world")},
                                            {QStringLiteral("revision"), 3}}}},
                    {QStringLiteral("latest_revision"), 3}},
                   ConversationModel::LoadKind::Delta);

    QCOMPARE(model.rowCount(), 1);
    QCOMPARE(model.data(model.index(0, 0), ConversationModel::MessageIdRole).toString(),
             QStringLiteral("final-1"));

    ConversationModel repeated;
    repeated.openSession(QStringLiteral("repeated"));
    repeated.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation-2")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("old-final")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("Same opening")},
                                 {QStringLiteral("revision"), 1}}}},
         {QStringLiteral("latest_revision"), 1}},
        ConversationModel::LoadKind::Tail);
    repeated.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation-2")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("new-live")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("kind"), QStringLiteral("live")},
                                 {QStringLiteral("text"), QStringLiteral("Same")},
                                 {QStringLiteral("revision"), 2}}}},
         {QStringLiteral("latest_revision"), 2}},
        ConversationModel::LoadKind::Delta);
    QCOMPARE(repeated.rowCount(), 2);
    QCOMPARE(repeated.data(repeated.index(1, 0), ConversationModel::MessageIdRole).toString(),
             QStringLiteral("new-live"));
}

void NativeCoreTest::activityRowsUpdateInPlaceBySemanticIdentity() {
    ConversationModel model;
    model.openSession(QStringLiteral("activity"));
    const auto event = [](const QString& status, const QString& summary) {
        return QJsonObject{{QStringLiteral("activity_status"), status},
                           {QStringLiteral("activity_action"), QStringLiteral("run")},
                           {QStringLiteral("activity_tool"), QStringLiteral("Bash")},
                           {QStringLiteral("activity_file_path"), QStringLiteral("/tmp")},
                           {QStringLiteral("activity_summary"), summary}};
    };

    model.applyActivityEvent(event(QStringLiteral("running"), QStringLiteral("first")));
    QCOMPARE(model.rowCount(), 1);
    const QString id = model.data(model.index(0, 0), ConversationModel::MessageIdRole).toString();
    QSignalSpy inserts(&model, &QAbstractItemModel::rowsInserted);
    QSignalSpy removes(&model, &QAbstractItemModel::rowsRemoved);
    QSignalSpy changes(&model, &QAbstractItemModel::dataChanged);

    model.applyActivityEvent(event(QStringLiteral("running"), QStringLiteral("second")));
    model.applyActivityEvent(event(QStringLiteral("ok"), QStringLiteral("complete")));
    model.applyActivityEvent(event(QStringLiteral("ok"), QStringLiteral("duplicate")));

    QCOMPARE(model.rowCount(), 1);
    QCOMPARE(model.data(model.index(0, 0), ConversationModel::MessageIdRole).toString(), id);
    QCOMPARE(model.data(model.index(0, 0), ConversationModel::BodyRole).toString(),
             QStringLiteral("complete"));
    QCOMPARE(model.data(model.index(0, 0), ConversationModel::ActivityStatusRole).toString(),
             QStringLiteral("ok"));
    QCOMPARE(changes.count(), 2);
    QCOMPARE(inserts.count(), 0);
    QCOMPARE(removes.count(), 0);

    ConversationModel details;
    details.openSession(QStringLiteral("details"));
    details.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("message-1")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("Done")},
                                 {QStringLiteral("tool_details_available"), true},
                                 {QStringLiteral("activity_count"), 1},
                                 {QStringLiteral("revision"), 1}}}},
         {QStringLiteral("latest_revision"), 1}},
        ConversationModel::LoadKind::Tail);
    details.applyToolDetails(
        QStringLiteral("message-1"),
        {{QStringLiteral("tools"),
          QJsonArray{QJsonObject{{QStringLiteral("name"), QStringLiteral("Read")},
                                 {QStringLiteral("summary"), QStringLiteral("Loaded file")}}}},
         {QStringLiteral("display_cells"), QJsonArray{}}});
    QCOMPARE(details.data(details.index(0, 0), ConversationModel::ToolsRole).toList().size(), 1);
}

void NativeCoreTest::olderHistoryPrependsWithoutReorderingTheTail() {
    ConversationModel model;
    model.openSession(QStringLiteral("history"));
    const auto turn = [](const QString& id, int revision) {
        return QJsonObject{{QStringLiteral("id"), id},
                           {QStringLiteral("role"), QStringLiteral("assistant")},
                           {QStringLiteral("text"), id},
                           {QStringLiteral("revision"), revision}};
    };
    model.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("conversation-1")},
                    {QStringLiteral("turns"),
                     QJsonArray{turn(QStringLiteral("a"), 2), turn(QStringLiteral("b"), 3)}}},
                   ConversationModel::LoadKind::Tail);
    QSignalSpy prepended(&model, &ConversationModel::rowsPrepended);
    model.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("conversation-1")},
                    {QStringLiteral("turns"), QJsonArray{turn(QStringLiteral("old"), 1)}}},
                   ConversationModel::LoadKind::Older);

    QCOMPARE(messageIds(model),
             QStringList({QStringLiteral("old"), QStringLiteral("a"), QStringLiteral("b")}));
    QCOMPARE(prepended.count(), 1);
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

    ConversationModel cached;
    cached.openSession(QStringLiteral("cached"));
    cached.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("one")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("One")},
                                 {QStringLiteral("revision"), 1}}}},
         {QStringLiteral("latest_revision"), 1}},
        ConversationModel::LoadKind::Tail);
    cached.addOptimistic(QStringLiteral("pending"), QStringLiteral("Newest"));
    cached.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("one")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("One")},
                                 {QStringLiteral("revision"), 1}},
                     QJsonObject{{QStringLiteral("id"), QStringLiteral("two")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("Two")},
                                 {QStringLiteral("revision"), 2}}}},
         {QStringLiteral("latest_revision"), 2}},
        ConversationModel::LoadKind::Tail);
    QCOMPARE(messageIds(cached),
             QStringList({QStringLiteral("one"), QStringLiteral("two"),
                          QStringLiteral("u-pending")}));

    ConversationModel retry;
    retry.openSession(QStringLiteral("retry"));
    retry.addOptimistic(QStringLiteral("failed"), QStringLiteral("Try this again"));
    retry.markDeliveryFailed(QStringLiteral("failed"));
    QCOMPARE(retry.takeFailedMessageForRetry(QStringLiteral("u-failed")),
             QStringLiteral("Try this again"));
    QCOMPARE(retry.rowCount(), 0);
    QVERIFY(retry.takeFailedMessageForRetry(QStringLiteral("u-failed")).isEmpty());
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

    ConversationModel authoritative;
    authoritative.openSession(QStringLiteral("same-id"));
    authoritative.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("keep")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("Keep")},
                                 {QStringLiteral("revision"), 1}},
                     QJsonObject{{QStringLiteral("id"), QStringLiteral("remove")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("Remove")},
                                 {QStringLiteral("revision"), 2}}}},
         {QStringLiteral("latest_revision"), 2}},
        ConversationModel::LoadKind::Tail);
    authoritative.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("keep")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("Keep")},
                                 {QStringLiteral("revision"), 1}}}},
         {QStringLiteral("latest_revision"), 1}},
        ConversationModel::LoadKind::Replace);
    QCOMPARE(messageIds(authoritative), QStringList{QStringLiteral("keep")});
    QCOMPARE(authoritative.latestRevision(), 1);
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
    QCOMPARE(voiceDeliverySession(QStringLiteral("rachel"), QStringLiteral("bella")),
             QStringLiteral("rachel"));
    QCOMPARE(voiceDeliverySession({}, QStringLiteral("bella")), QStringLiteral("bella"));
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
    QCOMPARE(panes.activeSession(), QStringLiteral("rachel"));

    // Complete a 2x2 grid. Directional movement follows the rendered
    // rectangles and stops at the outer edge rather than traversing/wrapping.
    panes.splitActive(QStringLiteral("horizontal"), QStringLiteral("omar"));
    QCOMPARE(panes.paneCount(), 4);
    QCOMPARE(panes.activeSession(), QStringLiteral("omar"));
    panes.navigate(QStringLiteral("right"));
    QCOMPARE(panes.activeSession(), QStringLiteral("adam"));
    panes.navigate(QStringLiteral("up"));
    QCOMPARE(panes.activeSession(), QStringLiteral("bella"));
    panes.navigate(QStringLiteral("left"));
    QCOMPARE(panes.activeSession(), QStringLiteral("rachel"));
    panes.navigate(QStringLiteral("left"));
    QCOMPARE(panes.activeSession(), QStringLiteral("rachel"));

    panes.toggleZoom();
    QCOMPARE(panes.zoomedPaneId(), panes.activePaneId());
    QCOMPARE(panes.displayRoot().value(QStringLiteral("kind")).toString(), QStringLiteral("leaf"));
    panes.navigate(QStringLiteral("down"));
    QCOMPARE(panes.activeSession(), QStringLiteral("omar"));
    QCOMPARE(panes.zoomedPaneId(), panes.activePaneId());
    panes.toggleZoom();
    QVERIFY(panes.zoomedPaneId().isEmpty());

    panes.closePane(firstId);
    QCOMPARE(panes.paneCount(), 3);
    panes.equalize();
    panes.resizeActive(0.1);
    const QVariantMap root = panes.rootNode();
    QVERIFY(root.value(QStringLiteral("ratio")).toDouble() >= 0.15);
    QVERIFY(root.value(QStringLiteral("ratio")).toDouble() <= 0.85);

    PaneTreeModel grid;
    grid.setActiveSession(QStringLiteral("root"));
    grid.splitActive(QStringLiteral("vertical"), QStringLiteral("right"));
    grid.splitActive(QStringLiteral("horizontal"), QStringLiteral("right-bottom"));
    grid.navigate(QStringLiteral("left"));
    grid.splitActive(QStringLiteral("horizontal"), QStringLiteral("left-bottom"));
    const QVariantList fourPanes = grid.paneLayout();
    for (const QVariant& value : fourPanes) {
        grid.focusPane(value.toMap().value(QStringLiteral("id")).toString());
        grid.splitActive(QStringLiteral("vertical"), QStringLiteral("column"));
    }
    const QVariantList eightPanes = grid.paneLayout();
    for (const QVariant& value : eightPanes) {
        grid.focusPane(value.toMap().value(QStringLiteral("id")).toString());
        grid.splitActive(QStringLiteral("horizontal"), QStringLiteral("row"));
    }
    QCOMPARE(grid.paneCount(), 16);

    const auto paneAt = [&grid](int column, int row) {
        const double targetX = (static_cast<double>(column) + 0.5) / 4.0;
        const double targetY = (static_cast<double>(row) + 0.5) / 4.0;
        QString best;
        double bestDistance = 10.0;
        for (const QVariant& value : grid.paneLayout()) {
            const QVariantMap pane = value.toMap();
            const double centerX = pane.value(QStringLiteral("x")).toDouble() +
                                   pane.value(QStringLiteral("width")).toDouble() / 2.0;
            const double centerY = pane.value(QStringLiteral("y")).toDouble() +
                                   pane.value(QStringLiteral("height")).toDouble() / 2.0;
            const double distance = std::abs(centerX - targetX) + std::abs(centerY - targetY);
            if (distance < bestDistance) {
                bestDistance = distance;
                best = pane.value(QStringLiteral("id")).toString();
            }
        }
        return best;
    };
    grid.focusPane(paneAt(1, 1));
    grid.navigate(QStringLiteral("right"));
    QCOMPARE(grid.activePaneId(), paneAt(2, 1));
    grid.navigate(QStringLiteral("down"));
    QCOMPARE(grid.activePaneId(), paneAt(2, 2));
    grid.navigate(QStringLiteral("left"));
    QCOMPARE(grid.activePaneId(), paneAt(1, 2));

    PaneTreeModel irregular;
    irregular.setActiveSession(QStringLiteral("top-left"));
    irregular.splitActive(QStringLiteral("horizontal"), QStringLiteral("bottom"));
    const QString bottomId = irregular.activePaneId();
    irregular.navigate(QStringLiteral("up"));
    irregular.splitActive(QStringLiteral("vertical"), QStringLiteral("top-right"));
    irregular.focusPane(bottomId);
    irregular.navigate(QStringLiteral("right"));
    QCOMPARE(irregular.activePaneId(), bottomId);
    irregular.navigate(QStringLiteral("left"));
    QCOMPARE(irregular.activePaneId(), bottomId);
}

void NativeCoreTest::apiClientRejectsCrossOriginAuthenticatedMedia() {
    ApiClient client;
    client.setEndpoint(QUrl(QStringLiteral("https://clarp.example.test")),
                       QStringLiteral("secret-token"));
    QSignalSpy failures(&client, &ApiClient::requestFailed);

    client.getBytes(QStringLiteral("avatar:external"),
                    QStringLiteral("https://evil.example.test/portrait.png"));

    QCOMPARE(failures.count(), 1);
    QCOMPARE(failures.first().at(0).toString(), QStringLiteral("avatar:external"));
    QVERIFY(failures.first().at(1).toString().contains(QStringLiteral("cross-origin")));

    QTcpServer redirectServer;
    QTcpServer foreignServer;
    QVERIFY(redirectServer.listen(QHostAddress::LocalHost, 0));
    QVERIFY(foreignServer.listen(QHostAddress::LocalHost, 0));
    int foreignConnections = 0;
    connect(&foreignServer, &QTcpServer::newConnection, &foreignServer, [&] {
        ++foreignConnections;
        if (QTcpSocket* socket = foreignServer.nextPendingConnection()) {
            socket->disconnectFromHost();
            socket->deleteLater();
        }
    });
    connect(&redirectServer, &QTcpServer::newConnection, &redirectServer, [&] {
        QTcpSocket* socket = redirectServer.nextPendingConnection();
        connect(socket, &QTcpSocket::readyRead, socket, [&, socket] {
            socket->readAll();
            const QByteArray location = QStringLiteral("http://127.0.0.1:%1/portrait.png")
                                            .arg(foreignServer.serverPort())
                                            .toUtf8();
            socket->write("HTTP/1.1 302 Found\r\nLocation: " + location +
                          "\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            socket->disconnectFromHost();
        });
    });

    ApiClient redirected;
    redirected.setEndpoint(
        QUrl(redirectServer.isListening()
                 ? QStringLiteral("http://127.0.0.1:%1").arg(redirectServer.serverPort())
                 : QString{}),
        QStringLiteral("secret-token"));
    QSignalSpy redirectFailures(&redirected, &ApiClient::requestFailed);
    redirected.getBytes(QStringLiteral("avatar:redirect"), QStringLiteral("/avatar.png"));
    QTRY_COMPARE_WITH_TIMEOUT(redirectFailures.count(), 1, 2'000);
    QTest::qWait(100);
    QCOMPARE(foreignConnections, 0);
}

void NativeCoreTest::apiClientDropsRepliesFromPreviousEndpointGeneration() {
    QTcpServer oldServer;
    QTcpServer currentServer;
    QVERIFY(oldServer.listen(QHostAddress::LocalHost, 0));
    QVERIFY(currentServer.listen(QHostAddress::LocalHost, 0));
    QPointer<QTcpSocket> oldSocket;
    connect(&oldServer, &QTcpServer::newConnection, &oldServer, [&] {
        oldSocket = oldServer.nextPendingConnection();
        connect(oldSocket, &QTcpSocket::readyRead, oldSocket, [oldSocket] {
            if (oldSocket)
                oldSocket->readAll();
        });
    });
    connect(&currentServer, &QTcpServer::newConnection, &currentServer, [&] {
        QTcpSocket* socket = currentServer.nextPendingConnection();
        connect(socket, &QTcpSocket::readyRead, socket, [socket] {
            socket->readAll();
            const QByteArray body = "{\"source\":\"current\"}";
            socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                          + QByteArray::number(body.size())
                          + "\r\nConnection: close\r\n\r\n" + body);
            socket->disconnectFromHost();
        });
    });

    ApiClient client;
    QSignalSpy received(&client, &ApiClient::jsonReceived);
    client.setEndpoint(
        QUrl(QStringLiteral("http://127.0.0.1:%1").arg(oldServer.serverPort())), {});
    client.get(QStringLiteral("old"), QStringLiteral("/slow"));
    QTRY_VERIFY_WITH_TIMEOUT(oldSocket != nullptr, 2'000);
    client.setEndpoint(
        QUrl(QStringLiteral("http://127.0.0.1:%1").arg(currentServer.serverPort())), {});

    const QByteArray staleBody = "{\"source\":\"stale\"}";
    oldSocket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                     + QByteArray::number(staleBody.size())
                     + "\r\nConnection: close\r\n\r\n" + staleBody);
    oldSocket->disconnectFromHost();
    QTest::qWait(100);
    QCOMPARE(received.count(), 0);

    client.get(QStringLiteral("current"), QStringLiteral("/now"));
    QTRY_COMPARE_WITH_TIMEOUT(received.count(), 1, 2'000);
    QCOMPARE(received.first().at(0).toString(), QStringLiteral("current"));
    QCOMPARE(received.first().at(1).toJsonObject().value(QStringLiteral("source")).toString(),
             QStringLiteral("current"));
}

void NativeCoreTest::paneDraftAndFocusSurviveLayoutStateChanges() {
    const QByteArray previousBaseUrl = qgetenv("CLARP_BASE_URL");
    const QByteArray previousToken = qgetenv("CLARP_TOKEN");
    qputenv("CLARP_BASE_URL", "http://layout-draft-test.invalid");
    qputenv("CLARP_TOKEN", "offline-layout-test");
    AppController controller;
    const QString paneId = controller.panes()->activePaneId();
    controller.setPaneDraft(paneId, QStringLiteral("first"), QStringLiteral("Unsent thought"));
    controller.setPaneDraft(paneId, QStringLiteral("second"), QStringLiteral("Other recipient"));
    controller.requestComposerFocus(paneId);

    controller.panes()->splitActive(QStringLiteral("vertical"), QStringLiteral("other"));
    controller.panes()->focusPane(paneId);
    controller.panes()->toggleZoom();

    QCOMPARE(controller.paneDraft(paneId, QStringLiteral("first")),
             QStringLiteral("Unsent thought"));
    QCOMPARE(controller.paneDraft(paneId, QStringLiteral("second")),
             QStringLiteral("Other recipient"));
    QCOMPARE(controller.composerFocusPane(), paneId);
    QCOMPARE(controller.panes()->zoomedPaneId(), paneId);
    controller.setPaneDraft(paneId, QStringLiteral("first"), {});
    controller.setPaneDraft(paneId, QStringLiteral("second"), {});
    if (previousBaseUrl.isEmpty())
        qunsetenv("CLARP_BASE_URL");
    else
        qputenv("CLARP_BASE_URL", previousBaseUrl);
    if (previousToken.isEmpty())
        qunsetenv("CLARP_TOKEN");
    else
        qputenv("CLARP_TOKEN", previousToken);
}

void NativeCoreTest::paneDraftIsDurableAndScopedToServerAndConversation() {
    const QString previousBaseUrl = qEnvironmentVariable("CLARP_BASE_URL");
    const QString previousToken = qEnvironmentVariable("CLARP_TOKEN");
    const QByteArray previousSharedFilesystem = qgetenv("CLARP_SHARED_FILESYSTEM_HOST");
    const QString uniqueBase = QStringLiteral("http://draft-test.invalid/")
                               + QUuid::createUuid().toString(QUuid::WithoutBraces);
    qputenv("CLARP_BASE_URL", uniqueBase.toUtf8());
    qputenv("CLARP_TOKEN", "offline-draft-test");

    {
        AppController first;
        first.setPaneDraft(QStringLiteral("pane-a"), QStringLiteral("rachel"),
                           QStringLiteral("durable thought"));
        QCOMPARE(first.paneDraft(QStringLiteral("pane-b"), QStringLiteral("rachel")),
                 QStringLiteral("durable thought"));
        QCOMPARE(first.paneDraft(QStringLiteral("pane-a"), QStringLiteral("bella")), QString{});
    }
    {
        AppController relaunched;
        QCOMPARE(relaunched.paneDraft(QStringLiteral("new-pane"), QStringLiteral("rachel")),
                 QStringLiteral("durable thought"));
        relaunched.setPaneDraft(QStringLiteral("new-pane"), QStringLiteral("rachel"), {});
        QCOMPARE(relaunched.paneDraft(QStringLiteral("new-pane"), QStringLiteral("rachel")),
                 QString{});
    }

    const QString localBase = QStringLiteral("http://127.0.0.1:1/")
                              + QUuid::createUuid().toString(QUuid::WithoutBraces);
    qputenv("CLARP_BASE_URL", localBase.toUtf8());
    qputenv("CLARP_SHARED_FILESYSTEM_HOST", localBase.toUtf8());
    QTemporaryFile attachment(QDir::temp().filePath(QStringLiteral("clarp-XXXXXX.png")));
    QVERIFY(attachment.open());
    const QByteArray pngBytes("\x89PNG\r\n\x1a\nfixture", 15);
    QCOMPARE(attachment.write(pngBytes), pngBytes.size());
    attachment.flush();
    {
        AppController first;
        first.attachLocalFile(QStringLiteral("pane-a"), QStringLiteral("rachel"),
                              QUrl::fromLocalFile(attachment.fileName()));
        first.setPaneDraft(QStringLiteral("pane-a"), QStringLiteral("rachel"),
                           QStringLiteral("caption"));
        first.setPaneDraft(QStringLiteral("pane-a"), QStringLiteral("rachel"), {});
        QCOMPARE(first.composerAttachments(QStringLiteral("pane-a"),
                                            QStringLiteral("rachel")).size(),
                 1);
        QCOMPARE(first.composerAttachments(QStringLiteral("pane-a"),
                                            QStringLiteral("rachel"))
                     .first()
                     .toMap()
                     .value(QStringLiteral("content_type"))
                     .toString(),
                 QStringLiteral("image/png"));
    }
    {
        AppController relaunched;
        const QVariantList restored = relaunched.composerAttachments(
            QStringLiteral("another-pane"), QStringLiteral("rachel"));
        QCOMPARE(restored.size(), 1);
        QCOMPARE(restored.first().toMap().value(QStringLiteral("path")).toString(),
                 QFileInfo(attachment.fileName()).canonicalFilePath());
        relaunched.removeComposerAttachment(
            QStringLiteral("another-pane"), QStringLiteral("rachel"),
            restored.first().toMap().value(QStringLiteral("id")).toString());
        QVERIFY(relaunched.composerAttachments(QStringLiteral("another-pane"),
                                                QStringLiteral("rachel")).isEmpty());
    }
    {
        AppController scoped;
        QVERIFY(scoped.sharedFilesystem());
        scoped.setBaseUrl(QStringLiteral("http://different-host.invalid"));
        QVERIFY(!scoped.sharedFilesystem());
    }

    if (previousBaseUrl.isEmpty())
        qunsetenv("CLARP_BASE_URL");
    else
        qputenv("CLARP_BASE_URL", previousBaseUrl.toUtf8());
    if (previousToken.isEmpty())
        qunsetenv("CLARP_TOKEN");
    else
        qputenv("CLARP_TOKEN", previousToken.toUtf8());
    if (previousSharedFilesystem.isEmpty())
        qunsetenv("CLARP_SHARED_FILESYSTEM_HOST");
    else
        qputenv("CLARP_SHARED_FILESYSTEM_HOST", previousSharedFilesystem);
}

void NativeCoreTest::transcriptCacheRestoresDurableRowsWithoutStaleRegression() {
    QTemporaryDir directory;
    QVERIFY(directory.isValid());
    TranscriptCache cache(directory.path());
    ConversationModel original;
    original.openSession(QStringLiteral("rachel"));
    original.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation-1")},
         {QStringLiteral("turns"),
          QJsonArray{
              QJsonObject{{QStringLiteral("id"), QStringLiteral("one")},
                          {QStringLiteral("role"), QStringLiteral("assistant")},
                          {QStringLiteral("text"), QStringLiteral("First")},
                          {QStringLiteral("revision"), 1}},
              QJsonObject{{QStringLiteral("id"), QStringLiteral("two")},
                          {QStringLiteral("role"), QStringLiteral("assistant")},
                          {QStringLiteral("text"), QStringLiteral("Second")},
                          {QStringLiteral("revision"), 2}},
          }},
         {QStringLiteral("latest_revision"), 2},
         {QStringLiteral("has_more"), true}},
        ConversationModel::LoadKind::Tail);
    original.addOptimistic(QStringLiteral("pending"), QStringLiteral("Do not persist"));
    original.markDeliveryFailed(QStringLiteral("pending"));
    original.showTransientThinking(QStringLiteral("Rachel"));

    const QJsonObject snapshot = original.cacheSnapshot();
    QCOMPARE(snapshot.value(QStringLiteral("turns")).toArray().size(), 3);
    QVERIFY(cache.save(QStringLiteral("https://host-a.example"), QStringLiteral("rachel"),
                       snapshot));
    QVERIFY(cache.load(QStringLiteral("https://host-b.example"), QStringLiteral("rachel"))
                .isEmpty());

    ConversationModel restored;
    restored.openSession(QStringLiteral("rachel"));
    QVERIFY(restored.restoreCacheSnapshot(
        cache.load(QStringLiteral("https://host-a.example"), QStringLiteral("rachel"))));
    QCOMPARE(messageIds(restored),
             QStringList({QStringLiteral("one"), QStringLiteral("two"),
                          QStringLiteral("u-pending")}));
    QVERIFY(restored.data(restored.index(2, 0), ConversationModel::DeliveryFailedRole).toBool());
    QCOMPARE(restored.latestRevision(), 2);
    QVERIFY(restored.hasMore());

    // A partial or older tail may refresh fields, but must not truncate a
    // fuller cache or move the revision cursor backwards.
    restored.applyLog(
        {{QStringLiteral("conversation_id"), QStringLiteral("conversation-1")},
         {QStringLiteral("turns"),
          QJsonArray{QJsonObject{{QStringLiteral("id"), QStringLiteral("one")},
                                 {QStringLiteral("role"), QStringLiteral("assistant")},
                                 {QStringLiteral("text"), QStringLiteral("First")},
                                 {QStringLiteral("revision"), 1}}}},
         {QStringLiteral("latest_revision"), 1}},
        ConversationModel::LoadKind::Tail);
    QCOMPARE(messageIds(restored),
             QStringList({QStringLiteral("one"), QStringLiteral("two"),
                          QStringLiteral("u-pending")}));
    QCOMPARE(restored.latestRevision(), 2);

    restored.applyLog({{QStringLiteral("conversation_id"), QString{}},
                       {QStringLiteral("turns"), QJsonArray{}},
                       {QStringLiteral("latest_revision"), 0}},
                      ConversationModel::LoadKind::Tail);
    QCOMPARE(restored.rowCount(), 0);
    QCOMPARE(restored.conversationId(), QString{});
    QCOMPARE(restored.latestRevision(), 0);
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
    const QByteArray previousSharedFilesystem = qgetenv("CLARP_SHARED_FILESYSTEM_HOST");
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8());
    qputenv("CLARP_TOKEN", "test-token");
    qputenv("CLARP_SHARED_FILESYSTEM_HOST", "http://not-this-test-host.invalid");

    AppController controller;
    QTRY_COMPARE_WITH_TIMEOUT(controller.agents()->rowCount(), 1, 3'000);
    QTRY_VERIFY_WITH_TIMEOUT(controller.connected(), 3'000);
    QCOMPARE(controller.selectedSession(), QStringLiteral("rachel"));
    QTRY_VERIFY_WITH_TIMEOUT(!controller.avatarSource(QStringLiteral("rachel")).isEmpty(), 3'000);
    QVERIFY(controller.avatarSource(QStringLiteral("rachel"))
                .toString()
                .startsWith(QStringLiteral("data:image/png;base64,")));

    server.holdLogRequests(true);
    controller.refreshConversation();
    QTRY_VERIFY_WITH_TIMEOUT(server.hasHeldLogRequest(), 3'000);
    QVERIFY(controller.conversation()->loading());
    controller.reconnect();
    QVERIFY(!controller.conversation()->loading());
    server.releaseHeldLogRequest();
    QTRY_VERIFY_WITH_TIMEOUT(controller.connected(), 3'000);
    controller.refreshConversation();
    QTRY_VERIFY_WITH_TIMEOUT(!controller.conversation()->loading(), 3'000);
    server.sendEvent({{QStringLiteral("type"), QStringLiteral("agent-state")},
                      {QStringLiteral("session"), QStringLiteral("rachel")},
                      {QStringLiteral("kind"), QStringLiteral("thinking")},
                      {QStringLiteral("ts"), 900}});
    QTRY_COMPARE_WITH_TIMEOUT(controller.conversation()->rowCount(), 1, 3'000);
    QVERIFY(controller.conversation()
                ->data(controller.conversation()->index(0, 0), ConversationModel::ActivityRole)
                .toBool());
    server.sendEvent({{QStringLiteral("type"), QStringLiteral("agent-state")},
                      {QStringLiteral("session"), QStringLiteral("rachel")},
                      {QStringLiteral("kind"), QStringLiteral("idle")},
                      {QStringLiteral("ts"), 901}});
    QTRY_COMPARE_WITH_TIMEOUT(controller.conversation()->rowCount(), 0, 3'000);
    server.sendEvent({{QStringLiteral("type"), QStringLiteral("user-notification")},
                      {QStringLiteral("session"), QStringLiteral("rachel")},
                      {QStringLiteral("unread"), true},
                      {QStringLiteral("preview"), QStringLiteral("Visible reply")}});
    QTest::qWait(50);
    QCOMPARE(controller.agents()
                 ->data(controller.agents()->index(0, 0), AgentListModel::UnreadRole)
                 .toBool(),
             false);
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

    server.holdUploadRequests(true);
    QTemporaryFile remoteAttachment;
    QVERIFY(remoteAttachment.open());
    QCOMPARE(remoteAttachment.write("remote attachment"), 17);
    remoteAttachment.flush();
    controller.attachLocalFile(QStringLiteral("pane-upload"), QStringLiteral("rachel"),
                               QUrl::fromLocalFile(remoteAttachment.fileName()));
    QTRY_VERIFY_WITH_TIMEOUT(server.hasHeldUploadRequest(), 3'000);
    QCOMPARE(controller.composerAttachments(QStringLiteral("pane-upload"),
                                             QStringLiteral("rachel")).size(),
             1);
    QCOMPARE(controller.composerAttachments(QStringLiteral("pane-upload"),
                                             QStringLiteral("rachel"))
                 .first()
                 .toMap()
                 .value(QStringLiteral("status"))
                 .toString(),
             QStringLiteral("uploading"));
    QVERIFY(!controller.composerCanSend(QStringLiteral("pane-upload"),
                                        QStringLiteral("rachel")));
    controller.setPaneDraft(QStringLiteral("pane-upload"), QStringLiteral("rachel"),
                            QStringLiteral("Keep this text"));
    QVERIFY(!controller.sendComposerMessage(QStringLiteral("pane-upload"),
                                             QStringLiteral("rachel"),
                                             QStringLiteral("Keep this text")));
    QCOMPARE(controller.paneDraft(QStringLiteral("pane-upload"), QStringLiteral("rachel")),
             QStringLiteral("Keep this text"));
    server.releaseHeldUploadRequest();
    QTRY_VERIFY_WITH_TIMEOUT(controller.composerCanSend(QStringLiteral("pane-upload"),
                                                        QStringLiteral("rachel")),
                             3'000);
    const QVariantList uploaded = controller.composerAttachments(
        QStringLiteral("pane-upload"), QStringLiteral("rachel"));
    QCOMPARE(uploaded.first().toMap().value(QStringLiteral("path")).toString(),
             QStringLiteral("/remote/uploads/file.txt"));
    controller.removeComposerAttachment(
        QStringLiteral("pane-upload"), QStringLiteral("rachel"),
        uploaded.first().toMap().value(QStringLiteral("id")).toString());
    controller.setPaneDraft(QStringLiteral("pane-upload"), QStringLiteral("rachel"), {});

    server.holdUploadRequests(true);
    controller.attachLocalFile(QStringLiteral("pane-upload"), QStringLiteral("rachel"),
                               QUrl::fromLocalFile(remoteAttachment.fileName()));
    QTRY_VERIFY_WITH_TIMEOUT(server.hasHeldUploadRequest(), 3'000);
    const QVariantList removable = controller.composerAttachments(
        QStringLiteral("pane-upload"), QStringLiteral("rachel"));
    QCOMPARE(removable.size(), 1);
    controller.removeComposerAttachment(
        QStringLiteral("pane-upload"), QStringLiteral("rachel"),
        removable.first().toMap().value(QStringLiteral("id")).toString());
    server.releaseHeldUploadRequest();
    QTest::qWait(100);
    QVERIFY(controller.composerAttachments(QStringLiteral("pane-upload"),
                                            QStringLiteral("rachel")).isEmpty());

    controller.loadUpdates();
    QTRY_COMPARE_WITH_TIMEOUT(controller.attentionItems().size(), 1, 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.backgroundJobs().size(), 1, 3'000);
    QCOMPARE(controller.backgroundJobProgress(controller.backgroundJobs().first().toMap()), 0.3);
    QTRY_COMPARE_WITH_TIMEOUT(controller.updateArtifacts().size(), 1, 3'000);
    QTRY_VERIFY_WITH_TIMEOUT(!controller.updatesLoading(), 3'000);
    QCOMPARE(controller.artifactsForSession(QStringLiteral("rachel")).size(), 1);
    QVERIFY(controller.artifactsForSession(QStringLiteral("bella")).isEmpty());
    controller.resolveDecision(QStringLiteral("decision-1"), QStringLiteral("yes"), 4);
    QVERIFY(controller.updateActionPending(QStringLiteral("decision"),
                                           QStringLiteral("decision-1")));
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedRequest(
                                 QStringLiteral("POST"),
                                 QStringLiteral("/decisions/decision-1/resolve")),
                             3'000);
    const QJsonObject decisionRequest = server.requestJson(
        QStringLiteral("POST"), QStringLiteral("/decisions/decision-1/resolve"));
    QCOMPARE(decisionRequest.value(QStringLiteral("choice")).toString(),
             QStringLiteral("accepted"));
    QCOMPARE(decisionRequest.value(QStringLiteral("expected_revision")).toInt(), 4);
    QVERIFY(!decisionRequest.contains(QStringLiteral("revision")));
    QTRY_VERIFY_WITH_TIMEOUT(!controller.updateActionPending(
                                 QStringLiteral("decision"), QStringLiteral("decision-1")),
                             3'000);

    controller.loadTeams();
    QTRY_COMPARE_WITH_TIMEOUT(controller.teams().size(), 1, 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.selectedTeamId(), QStringLiteral("team-1"), 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.teamMessages().size(), 1, 3'000);
    QCOMPARE(controller.agentNameById(QStringLiteral("agent-rachel")), QStringLiteral("Rachel"));
    QCOMPARE(controller.teamAgentChoices().size(), 1);
    QCOMPARE(controller.agentDetails(QStringLiteral("rachel"))
                 .value(QStringLiteral("team_ids"))
                 .toList(),
             QVariantList{QStringLiteral("team-1")});
    QCOMPARE(controller.availableMcpServers().size(), 2);
    QCOMPARE(controller.agentDetails(QStringLiteral("rachel"))
                 .value(QStringLiteral("mcp_servers"))
                 .toList()
                 .size(),
             1);
    QTRY_VERIFY_WITH_TIMEOUT(!controller.teamsLoading(), 3'000);

    controller.loadTurnQueue(QStringLiteral("rachel"));
    QTRY_COMPARE_WITH_TIMEOUT(controller.turnQueueItems().size(), 1, 3'000);
    QCOMPARE(controller.turnQueueSession(), QStringLiteral("rachel"));
    QVERIFY(!controller.turnQueuePaused());
    QTRY_VERIFY_WITH_TIMEOUT(!controller.turnQueueLoading(), 3'000);
    controller.loadTurnQueue(QStringLiteral("bella"));
    QCOMPARE(controller.turnQueueSession(), QStringLiteral("bella"));
    QVERIFY(controller.turnQueueItems().isEmpty());
    QVERIFY(!controller.turnQueuePaused());
    controller.loadTurnQueue(QStringLiteral("rachel"));
    QTRY_COMPARE_WITH_TIMEOUT(controller.turnQueueItems().size(), 1, 3'000);

    controller.updateQueuedTurn(QStringLiteral("queue-1"), QStringLiteral("Edited"));
    QTRY_VERIFY_WITH_TIMEOUT(
        server.receivedRequest(QStringLiteral("PUT"), QStringLiteral("/turn-queue/queue-1")),
        3'000);
    controller.sendQueuedTurn(QStringLiteral("queue-1"));
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedRequest(
                                 QStringLiteral("POST"),
                                 QStringLiteral("/turn-queue/queue-1/send")),
                             3'000);
    controller.deleteQueuedTurn(QStringLiteral("queue-1"));
    QTRY_VERIFY_WITH_TIMEOUT(
        server.receivedRequest(QStringLiteral("DELETE"), QStringLiteral("/turn-queue/queue-1")),
        3'000);

    controller.loadAgentProfile(QStringLiteral("rachel"));
    QTRY_COMPARE_WITH_TIMEOUT(controller.profileSession(), QStringLiteral("rachel"), 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(
        controller.profileTaskPlan().value(QStringLiteral("plan_id")).toString(),
        QStringLiteral("plan-1"), 3'000);
    QTRY_VERIFY_WITH_TIMEOUT(!controller.profileLoading(), 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(
        controller.profileHeartbeat().value(QStringLiteral("history")).toList().size(), 1,
        3'000);
    controller.loadMedia(QStringLiteral("rachel"));
    QTRY_COMPARE_WITH_TIMEOUT(controller.mediaForSession(QStringLiteral("rachel")).size(), 1,
                              3'000);
    QTRY_VERIFY_WITH_TIMEOUT(!controller.mediaSource(QStringLiteral("asset-1")).isEmpty(), 3'000);
    QVERIFY(controller.mediaSource(QStringLiteral("asset-1"))
                .toString()
                .startsWith(QStringLiteral("data:image/png;base64,")));
    const QString renderedMedia = controller.resolveMediaMarkdown(
        QStringLiteral("![Rendered result](clarp-media://asset/asset-1)"));
    QVERIFY(renderedMedia.startsWith(QStringLiteral("![Rendered result](data:image/png;base64,")));
    QVERIFY(renderedMedia.endsWith(u')'));
    controller.loadPromptHistory(QStringLiteral("rachel"));
    QTRY_COMPARE_WITH_TIMEOUT(controller.profilePrompts().size(), 1, 3'000);
    QCOMPARE(controller.profilePrompts().first().toMap().value(QStringLiteral("turn_id")).toString(),
             QStringLiteral("prompt-1"));
    QVERIFY(!controller.profilePromptsHaveMore());
    controller.loadSettingsStatus();
    QTRY_VERIFY_WITH_TIMEOUT(controller.diagnosticsHealth()
                                 .value(QStringLiteral("ready"))
                                 .toBool(),
                             3'000);
    QTRY_VERIFY_WITH_TIMEOUT(controller.transcriptionCapabilities()
                                 .value(QStringLiteral("available"))
                                 .toBool(),
                             3'000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.ttsProviderStatus()
                                  .value(QStringLiteral("provider"))
                                  .toString(),
                              QStringLiteral("cartesia"), 3'000);
    QTRY_VERIFY_WITH_TIMEOUT(!controller.settingsStatusLoading(), 3'000);
    controller.setTtsProviders(QStringLiteral("cartesia"), QStringLiteral("elevenlabs"), {});
    QTRY_VERIFY_WITH_TIMEOUT(
        server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/tts/providers")), 3'000);
    const QJsonObject ttsRequest = server.requestJson(
        QStringLiteral("POST"), QStringLiteral("/tts/providers"));
    QCOMPARE(ttsRequest.value(QStringLiteral("provider")).toString(),
             QStringLiteral("cartesia"));
    QCOMPARE(ttsRequest.value(QStringLiteral("fallback")).toString(),
             QStringLiteral("elevenlabs"));
    controller.updateTeam(QStringLiteral("team-1"), QStringLiteral("Renamed"),
                          QStringLiteral("#123456"), QStringLiteral("agent-rachel"));
    QTRY_VERIFY_WITH_TIMEOUT(
        server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/teams/team-1")), 3'000);
    controller.addTeamMember(QStringLiteral("team-1"), QStringLiteral("agent-rachel"));
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedRequest(
                                 QStringLiteral("POST"),
                                 QStringLiteral("/teams/team-1/members")),
                             3'000);
    controller.removeTeamMember(QStringLiteral("team-1"), QStringLiteral("agent-rachel"));
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedRequest(
                                 QStringLiteral("DELETE"),
                                 QStringLiteral("/teams/team-1/members/agent-rachel")),
                             3'000);
    controller.setTeamNudging(QStringLiteral("team-1"), true);
    QTRY_VERIFY_WITH_TIMEOUT(
        server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/team-nudging")), 3'000);
    const QJsonObject nudgeRequest = server.requestJson(
        QStringLiteral("POST"), QStringLiteral("/team-nudging"));
    QCOMPARE(nudgeRequest.value(QStringLiteral("team_id")).toString(),
             QStringLiteral("team-1"));
    QVERIFY(nudgeRequest.value(QStringLiteral("nudge_enabled")).toBool());
    controller.setAgentLlm(QStringLiteral("rachel"), QStringLiteral("gpt-test"),
                           QStringLiteral("high"));
    QTRY_VERIFY_WITH_TIMEOUT(
        server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/agent-llm")), 3'000);
    controller.setAgentMcp(QStringLiteral("rachel"),
                           QVariantList{QStringLiteral("github")});
    QTRY_VERIFY_WITH_TIMEOUT(
        server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/agent-mcp")), 3'000);
    controller.createAgent(QStringLiteral("New Agent"), QStringLiteral("/tmp"),
                           QStringLiteral("claude"), {}, {}, {}, QStringLiteral("fresh"), {},
                           QVariantList{QStringLiteral("github")});
    QTRY_VERIFY_WITH_TIMEOUT(
        server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/agents")), 3'000);
    QCOMPARE(server.requestJson(QStringLiteral("POST"), QStringLiteral("/agents"))
                 .value(QStringLiteral("mcp_servers"))
                 .toArray(),
             QJsonArray{QStringLiteral("github")});
    controller.releaseAgent(QStringLiteral("mike"));
    QVERIFY(controller.errorMessage().contains(QStringLiteral("protected")));
    QVERIFY(!server.receivedRequest(QStringLiteral("DELETE"), QStringLiteral("/agents/mike")));
    controller.clearError();

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

    controller.setSharedFilesystem(true);
    QVERIFY(controller.sharedFilesystem());
    controller.setBaseUrl(QStringLiteral("http://another-host.invalid"));
    QVERIFY(!controller.sharedFilesystem());
    QVERIFY(controller.attentionItems().isEmpty());
    QVERIFY(controller.backgroundJobs().isEmpty());
    QVERIFY(controller.updateArtifacts().isEmpty());
    QVERIFY(controller.teams().isEmpty());
    QVERIFY(controller.teamMessages().isEmpty());
    QVERIFY(controller.selectedTeamId().isEmpty());
    QVERIFY(controller.turnQueueItems().isEmpty());
    QVERIFY(controller.turnQueueSession().isEmpty());
    QVERIFY(controller.profileTaskPlan().isEmpty());
    QVERIFY(controller.profileSession().isEmpty());
    QVERIFY(controller.selectedSession().isEmpty());
    controller.setBaseUrl(server.baseUrl());
    QVERIFY(controller.sharedFilesystem());
    controller.setSharedFilesystem(false);

    qunsetenv("CLARP_BASE_URL");
    qunsetenv("CLARP_TOKEN");
    if (previousSharedFilesystem.isEmpty())
        qunsetenv("CLARP_SHARED_FILESYSTEM_HOST");
    else
        qputenv("CLARP_SHARED_FILESYSTEM_HOST", previousSharedFilesystem);
}

void NativeCoreTest::connectedControllerShutsDownWithoutLateSseCallbacks() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8());
    qputenv("CLARP_TOKEN", "test-token");
    auto controller = std::make_unique<AppController>();
    QTRY_VERIFY_WITH_TIMEOUT(controller->connected(), 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(controller->agents()->rowCount(), 1, 3'000);
    controller.reset();
    qunsetenv("CLARP_BASE_URL");
    qunsetenv("CLARP_TOKEN");
    QVERIFY(true);
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

void NativeCoreTest::backgroundTranscriptionsKeepTheirChatOwnership() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    AudioController audio;
    audio.setEndpoint(QUrl(server.baseUrl()), QStringLiteral("test-token"));
    QSignalSpy ready(&audio, &AudioController::transcriptionReady);

    audio.transcribeRecording(QByteArray(2'000, 'a'), QStringLiteral("rachel"));
    audio.transcribeRecording(QByteArray(2'000, 'b'), QStringLiteral("bella"));
    QCOMPARE(audio.transcriptionsInFlight(), 2);
    QCOMPARE(audio.transcriptionsForSession(QStringLiteral("rachel")), 1);
    QCOMPARE(audio.transcriptionsForSession(QStringLiteral("bella")), 1);
    QTRY_COMPARE_WITH_TIMEOUT(ready.count(), 2, 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(audio.transcriptionsInFlight(), 0, 3'000);

    QSet<QString> targets;
    for (const QList<QVariant>& arguments : ready) {
        targets.insert(arguments.at(4).toString());
    }
    QCOMPARE(targets, QSet<QString>({QStringLiteral("rachel"), QStringLiteral("bella")}));

    AudioController cancelled;
    cancelled.setEndpoint(QUrl(server.baseUrl()), QStringLiteral("test-token"));
    QSignalSpy cancelledReady(&cancelled, &AudioController::transcriptionReady);
    cancelled.transcribeRecording(QByteArray(2'000, 'c'), QStringLiteral("rachel"));
    QCOMPARE(cancelled.transcriptionsForSession(QStringLiteral("rachel")), 1);
    cancelled.cancelTranscriptionsForSession(QStringLiteral("rachel"));
    QTRY_COMPARE_WITH_TIMEOUT(cancelled.transcriptionsInFlight(), 0, 3'000);
    QTest::qWait(50);
    QCOMPARE(cancelledReady.count(), 0);
}

QTEST_MAIN(NativeCoreTest)

#include "tst_native_core.moc"
