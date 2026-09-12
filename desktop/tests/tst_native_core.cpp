#include "models/ConversationPresentationModel.h"
#include <QTextDocument>
#include <QDesktopServices>
#include <QClipboard>
#include <QMimeData>
#include "app/PreviewRelaunch.h"
#include "app/PreviewVersions.h"
#include "app/LocalReport.h"
#include <QStandardItemModel>
#include "app/AppController.h"
#include "app/CredentialStore.h"
#include "app/TranscriptCache.h"
#include "app/TimeFormat.h"
#include "media/PortraitImage.h"
#include "models/AgentFilterModel.h"
#include <QBuffer>
#include <QImage>
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
#include <QSettings>
#include <QSignalSpy>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTest>
#include <QTimer>
#include <QTemporaryFile>
#include <QTemporaryDir>
#include <QUrl>
#include <QUuid>
#include <QScopeGuard>
#include <cmath>

using namespace clarp;
class ReportUrlCapture : public QObject {
    Q_OBJECT
public:
    QList<QUrl> opened;
    Q_SLOT void capture(const QUrl& url) { opened.append(url); }
};


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
    [[nodiscard]] qsizetype requestCount(const QString& method, const QString& path) const {
        return m_requests.count(method + u' ' + path);
    }

    void setJsonResponse(const QString& method, const QString& path, int status,
                         const QJsonObject& body) {
        m_jsonResponses.insert(method + u' ' + path, {status, body});
    }

    [[nodiscard]] QJsonObject requestJson(const QString& method, const QString& path) const {
        return QJsonDocument::fromJson(m_requestBodies.value(method + u' ' + path)).object();
    }

    void holdNextSnapshot() { m_holdSnapshot = true; }
    [[nodiscard]] bool hasHeldSnapshot() const { return m_heldSnapshot != nullptr; }
    void releaseHeldSnapshot(const QJsonObject& body) {
        if (m_heldSnapshot) { respond(m_heldSnapshot, 200, body); m_heldSnapshot = nullptr; }
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

        if (requestKey == QStringLiteral("GET /agents/snapshot") && m_holdSnapshot) {
            m_holdSnapshot = false; m_heldSnapshot = socket; return;
        }
        if (const auto response = m_jsonResponses.constFind(requestKey);
            response != m_jsonResponses.cend()) {
            respond(socket, response->first, response->second);
            return;
        }

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
    QHash<QString, QPair<int, QJsonObject>> m_jsonResponses;
    QPointer<QTcpSocket> m_heldLogSocket;
    QPointer<QTcpSocket> m_heldUploadSocket;
    bool m_holdSnapshot = false;
    QPointer<QTcpSocket> m_heldSnapshot;
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
    void spawnedLifecycleNeverBecomesTranscriptTool();
    void voiceErrorsStayInTheirSession();
    void clipboardImageBecomesAttachmentWithoutSending();
    void relaunchPreservesHostSessionAndDraft();
    void previewRestartCapturesContextAndRejectsBusy();
    void restoredSessionDoesNotFallBackToAnotherAgent();
    void localReportsRequireOriginAndSafeReadableFiles();
    void leadingDayTracksVisibleHistory();
    void oldActivityGroupsAreLazyAndVisitScoped();
    void consecutiveExplanationsCollapseWithoutChangingTranscript();
    void attachedToolElapsedUsesAssistantBoundaryAndPreservesSender();
    void readyModePreservesActivityAndHidesOnlyProvisionalBody();
    void idleContactStartsFreshWithSavedDefaults();
    void newAgentWaitsForOwnRosterAndRejectsLateSnapshots();
    void fastLaunchOpensWithoutWaitingForFleet();
    void resumeLaunchOpensExactSessionWithoutFleet();
    void launchPoolCarriesBackendModelAndHandlesEmpty();
    void redesignedRosterFiltersWithoutMutatingSource();
    void rosterLookupIsConsistentDuringStructuralSignals();
    void circularPortraitsAreBoundedAndAntialiased();
    void agentTerminalLaunchesNativeCliThroughDefaultTerminal();
    void sseParserHandlesChunksCommentsAndReplayIds();
    void nextAttentionCyclesWaitingUnreadAndPending();
    void readyPresentationRetainsCanonicalStreamAndRevealsFinal();
    void sseCursorIsScopedToOneHost();
    void snapshotFiltersArchivedAgentsAndPatchesEvents();
    void agentSnapshotDiffsInPlaceAndRejectsStaleState();
    void tailThenDeltaMatchesGoldenFixture();
    void streamingRowsUpdateInPlaceAndRetireWhenFinalized();
    void activityRowsUpdateInPlaceBySemanticIdentity();
    void olderHistoryPrependsWithoutReorderingTheTail();
    void growingReplyRejectsStaleRevision();
    void optimisticDeliveryStaysVisibleUntilConfirmed();
    void emptyStartupWaitsForExplicitChoiceAndRetryTargetsLatestFailure();
    void conversationChangeRequestsReplacement();
    void clipSourcePrecedenceMatchesContract();
    void wavEncodingProducesAValidPcmHeader();
    void paneTreeSplitsClosesNavigatesAndZooms();
    void apiClientRejectsCrossOriginAuthenticatedMedia();
    void apiClientDropsRepliesFromPreviousEndpointGeneration();
    void paneDraftAndFocusSurviveLayoutStateChanges();
    void paneActivationAlwaysTargetsItsComposer();
    void paneDraftIsDurableAndScopedToServerAndConversation();
    void transcriptCacheRestoresDurableRowsWithoutStaleRegression();
    void credentialStoreRoundTrip();
    void appControllerCompletesCoreProtocolFlow();
    void connectedControllerShutsDownWithoutLateSseCallbacks();
    void contactsExcludeActivePersonas();
    void microphoneCanCaptureNativePcm();
    void backgroundTranscriptionsKeepTheirChatOwnership();
    void sharedPlaybackDoesNotDuplicateDownloads();
    void markdownParagraphsBecomeVisibleDisplayBlocks();
    void agentReplyKeepsItsAuthorAndNamesTheAnsweredAgent();
    void pairConversationRoomsAreReadOnlyProjections();
    void onlyWebAndMailLinksAreOpenable();
    void toolOutputLinksAreAnchoredWithoutChangingTheText();
    void reportHtmlCannotFetchRemoteResources();
    void reportHtmlKeepsStructureButNeverFetchesRemoteResources();
    void reportForArtifactExposesSanitizedBody();
    void portedUrlsBecomeLinksWithoutChangingVisibleText();
};

void NativeCoreTest::attachedToolElapsedUsesAssistantBoundaryAndPreservesSender() {
    ConversationModel source;
    source.openSession(QStringLiteral("timing"));
    QJsonObject tools{{QStringLiteral("id"), QStringLiteral("tools")}, {QStringLiteral("role"), QStringLiteral("assistant")},
        {QStringLiteral("text"), QStringLiteral("Checking the build.")}, {QStringLiteral("activity_count"), 21},
        {QStringLiteral("timestamp"), QStringLiteral("2020-01-01T10:00:00Z")}};
    QJsonObject done{{QStringLiteral("id"), QStringLiteral("done")}, {QStringLiteral("role"), QStringLiteral("assistant")},
        {QStringLiteral("text"), QStringLiteral("Done.")}, {QStringLiteral("timestamp"), QStringLiteral("2020-01-01T10:01:23Z")}};
    const auto load = [&] {
        source.applyLog({{QStringLiteral("turns"), QJsonArray{tools, done}}}, ConversationModel::LoadKind::Replace);
    };
    load();
    ConversationPresentationModel view;
    view.setSourceModel(&source);
    view.setActivityMode(0);
    QCOMPARE(view.index(0, 0).data(ConversationPresentationModel::ActivityLabelRole).toString(),
             QStringLiteral("21 tool calls · 1m 23s elapsed"));
    QVERIFY(view.index(0, 0).data(ConversationPresentationModel::GroupLabelRole).toString().isEmpty());
    QSignalSpy labels(&view, &QAbstractItemModel::dataChanged);
    done.insert(QStringLiteral("timestamp"), QStringLiteral("2020-01-01T10:02:00Z"));
    source.applyLog({{QStringLiteral("turns"), QJsonArray{done}}}, ConversationModel::LoadKind::Delta);
    QCOMPARE(view.index(0, 0).data(ConversationPresentationModel::ActivityLabelRole).toString(),
             QStringLiteral("21 tool calls · 2m 0s elapsed"));
    bool updatedPrecedingLabel = false;
    for (const auto& signal : labels) {
        if (signal.at(0).value<QModelIndex>().row() == 0
            && signal.at(2).value<QList<int>>().contains(ConversationPresentationModel::ActivityLabelRole))
            updatedPrecedingLabel = true;
    }
    QVERIFY(updatedPrecedingLabel);
    done.insert(QStringLiteral("role"), QStringLiteral("user"));
    done.insert(QStringLiteral("origin"), QStringLiteral("agent"));
    done.insert(QStringLiteral("sender_agent_id"), QStringLiteral("sender-id"));
    done.insert(QStringLiteral("sender_session"), QStringLiteral("sender-session"));
    load();
    QCOMPARE(view.index(0, 0).data(ConversationPresentationModel::ActivityLabelRole).toString(), QStringLiteral("21 tool calls"));
    QCOMPARE(source.index(1, 0).data(ConversationModel::SenderAgentIdRole).toString(), QStringLiteral("sender-id"));
    QCOMPARE(source.index(1, 0).data(ConversationModel::SenderSessionRole).toString(), QStringLiteral("sender-session"));
    ConversationModel restored;
    QVERIFY(restored.restoreCacheSnapshot(source.cacheSnapshot()));
    QCOMPARE(restored.index(1, 0).data(ConversationModel::SenderAgentIdRole).toString(), QStringLiteral("sender-id"));
    tools.insert(QStringLiteral("timestamp"), QStringLiteral("invalid"));
    load();
    QCOMPARE(view.index(0, 0).data(ConversationPresentationModel::ActivityLabelRole).toString(), QStringLiteral("21 tool calls"));
}

void NativeCoreTest::consecutiveExplanationsCollapseWithoutChangingTranscript() {
    QStandardItemModel source;
    const auto append = [&source](const QString& id, const QString& text, const QString& body = QString{}) {
        auto* row = new QStandardItem;
        row->setData(id, ConversationModel::MessageIdRole);
        row->setData(QStringLiteral("assistant"), ConversationModel::AuthorRole);
        row->setData(body, ConversationModel::BodyRole);
        row->setData(QVariantList{QVariantMap{{QStringLiteral("name"), id},
            {QStringLiteral("summary"), text}}}, ConversationModel::ToolsRole);
        source.appendRow(row);
        return row;
    };
    append(QStringLiteral("a"), QStringLiteral("Check files"));
    append(QStringLiteral("b"), QStringLiteral("Check files"));
    ConversationPresentationModel view;
    view.setSourceModel(&source);
    const auto lookup = [](const QVariantMap& value) { return value.value(QStringLiteral("summary")).toString(); };
    view.setExplanationLookup(lookup);
    const auto repeat = [&view](int row) {
        return view.index(row, 0).data(ConversationModel::ToolsRole).toList().first().toMap()
            .value(QStringLiteral("_explanationRepeat"), 1).toInt();
    };
    QCOMPARE(view.rowCount(), 1);
    QCOMPARE(repeat(0), 2);
    append(QStringLiteral("c"), QStringLiteral("Check files"));
    QCOMPARE(view.rowCount(), 1);
    QCOMPARE(repeat(0), 3);
    QCOMPARE(source.rowCount(), 3);
    QCOMPARE(source.index(1, 0).data(ConversationModel::ToolsRole).toList().size(), 1);
    auto* pending = append(QStringLiteral("d"), QString{});
    append(QStringLiteral("e"), QStringLiteral("Check files"));
    QCOMPARE(view.rowCount(), 3); // Pending explanations break the run.
    pending->setData(QVariantList{QVariantMap{{QStringLiteral("summary"), QStringLiteral("Check files")}}}, ConversationModel::ToolsRole);
    QCOMPARE(view.rowCount(), 1); // Late completion extends the first row.
    QCOMPARE(repeat(0), 5);
    const auto original = source.index(2, 0).data(ConversationModel::ToolsRole);
    source.item(2)->setData(QVariantList{QVariantMap{{QStringLiteral("summary"), QStringLiteral("Different")}}}, ConversationModel::ToolsRole);
    QCOMPARE(view.rowCount(), 3);
    QCOMPARE(repeat(0), 2);
    QCOMPARE(repeat(2), 2);
    source.item(2)->setData(QVariantList{QVariantMap{{QStringLiteral("summary"), QStringLiteral("Check files")},
        {QStringLiteral("status"), QStringLiteral("error")}}}, ConversationModel::ToolsRole);
    QCOMPARE(view.rowCount(), 3); // Equal words must not hide a different status.
    source.item(2)->setData(original, ConversationModel::ToolsRole);
    QCOMPARE(repeat(0), 5);
    append(QStringLiteral("f"), QStringLiteral("Check files"), QStringLiteral("A message boundary"));
    QCOMPARE(view.rowCount(), 2);
    QCOMPARE(repeat(1), 1);
    view.setExplanationLookup({});
    QCOMPARE(view.rowCount(), 6); // Developer mode restores every original row.
    view.setExplanationLookup(lookup);
    source.removeRow(1);
    QCOMPARE(repeat(0), 4);
    view.setActivityMode(0);
    QCOMPARE(view.rowCount(), 2);
    QVERIFY(view.index(0, 0).data(ConversationModel::ToolsRole).toList().isEmpty());
    view.toggleGroup(QStringLiteral("a"));
    const auto tools = view.index(0, 0).data(ConversationModel::ToolsRole).toList();
    QCOMPARE(tools.size(), 4); // Details retained; delegates suppress repeats.
    QCOMPARE(tools.first().toMap().value(QStringLiteral("_explanationRepeat")).toInt(), 4);
    QCOMPARE(tools.last().toMap().value(QStringLiteral("_explanationRepeat")).toInt(), 0);
}

void NativeCoreTest::oldActivityGroupsAreLazyAndVisitScoped() {
    QStandardItemModel source;
    const auto append = [&source](const QString& id, const QString& time, const QString& kind) {
        auto* row = new QStandardItem;
        row->setData(id, ConversationModel::MessageIdRole);
        row->setData(QStringLiteral("assistant"), ConversationModel::AuthorRole);
        row->setData(QString{}, ConversationModel::BodyRole);
        row->setData(time, ConversationModel::TimestampRole);
        row->setData(kind, ConversationModel::KindRole);
        // The Host stamps ordinary rows "user"; grouping must not read that as
        // a foreign dispatcher and split every activity into its own group.
        row->setData(QStringLiteral("user"), ConversationModel::OriginRole);
        row->setData(1, ConversationModel::ActivityCountRole);
        row->setData(QVariantList{QVariantMap{{QStringLiteral("name"), QStringLiteral("Read")}}}, ConversationModel::ToolsRole);
        source.appendRow(row);
        return row;
    };
    append(QStringLiteral("a"), QStringLiteral("2020-01-01T00:00:00Z"), QString{});
    append(QStringLiteral("b"), QStringLiteral("2020-01-01T01:32:23Z"), QString{});
    ConversationPresentationModel view;
    view.setSourceModel(&source);
    view.setActivityMode(2);
    QCOMPARE(view.rowCount(), 1);
    QCOMPARE(view.data(view.index(0, 0), ConversationPresentationModel::GroupLabelRole).toString(), QStringLiteral("2 tool calls · 1h 32m 23s elapsed"));
    QVERIFY(view.data(view.index(0, 0), ConversationModel::ToolsRole).toList().isEmpty());
    view.toggleGroup(QStringLiteral("a"));
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::ToolsRole).toList().size(), 2);
    auto* live = append(QStringLiteral("c"), QStringLiteral("2020-01-01T02:00:00Z"), QStringLiteral("live"));
    QCOMPARE(view.rowCount(), 2);
    live->setData(QStringLiteral("assistant"), ConversationModel::KindRole);
    QCOMPARE(view.rowCount(), 2); // Completion must not collapse a witnessed live row.
    view.beginVisit();
    QCOMPARE(view.rowCount(), 1);
    QVERIFY(view.data(view.index(0, 0), ConversationModel::ToolsRole).toList().isEmpty());
    view.setActivityMode(1);
    QCOMPARE(view.rowCount(), 3);
    // A row dispatched by a teammate or a scheduler is not part of this turn's
    // activity and must stay outside the group.
    view.setActivityMode(2);
    view.beginVisit();
    QCOMPARE(view.rowCount(), 1);
    append(QStringLiteral("teammate"), QStringLiteral("2020-01-01T03:00:00Z"), QString{})
        ->setData(QStringLiteral("agent"), ConversationModel::OriginRole);
    QCOMPARE(view.rowCount(), 2);
    QVERIFY(view.data(view.index(1, 0), ConversationPresentationModel::GroupLabelRole).toString().isEmpty());
}

void NativeCoreTest::readyPresentationRetainsCanonicalStreamAndRevealsFinal() {
    ConversationModel source;
    source.openSession(QStringLiteral("ready"));
    ConversationPresentationModel view;
    view.setSourceModel(&source);
    const auto row = [](const QString& id, const QString& role, const QString& kind, const QString& text, int revision) {
        return QJsonObject{{QStringLiteral("id"), id}, {QStringLiteral("role"), role},
            {QStringLiteral("kind"), kind}, {QStringLiteral("text"), text}, {QStringLiteral("revision"), revision}};
    };
    source.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("ready-thread")},
        {QStringLiteral("turns"), QJsonArray{row(QStringLiteral("u"), QStringLiteral("user"), QString{}, QStringLiteral("Question"), 1),
            row(QStringLiteral("live"), QStringLiteral("assistant"), QStringLiteral("live"), QStringLiteral("Partial"), 2)}},
        {QStringLiteral("latest_revision"), 2}}, ConversationModel::LoadKind::Tail);
    QCOMPARE(view.rowCount(), 2);
    view.setShowWhenReady(true);
    QCOMPARE(view.rowCount(), 1);
    QCOMPARE(source.rowCount(), 2);
    QCOMPARE(view.indexOfMessage(QStringLiteral("live")), -1);
    QSignalSpy updates(&view, &QAbstractItemModel::dataChanged);
    source.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("ready-thread")},
        {QStringLiteral("turns"), QJsonArray{row(QStringLiteral("live"), QStringLiteral("assistant"), QStringLiteral("live"), QStringLiteral("Partial updated"), 3)}},
        {QStringLiteral("latest_revision"), 3}}, ConversationModel::LoadKind::Delta);
    QCOMPARE(view.rowCount(), 1);
    QCOMPARE(updates.count(), 0);
    source.showTransientThinking(QStringLiteral("Agent"));
    QCOMPARE(view.rowCount(), 1);
    view.setShowWhenReady(false);
    QCOMPARE(view.rowCount(), source.rowCount());
    QCOMPARE(view.indexOfMessage(QStringLiteral("live")), 1);
    view.setShowWhenReady(true);
    source.applyLog({{QStringLiteral("conversation_id"), QStringLiteral("ready-thread")},
        {QStringLiteral("turns"), QJsonArray{row(QStringLiteral("final"), QStringLiteral("assistant"), QStringLiteral("assistant"), QStringLiteral("Partial updated complete"), 4)}},
        {QStringLiteral("latest_revision"), 4}}, ConversationModel::LoadKind::Delta);
    QCOMPARE(view.rowCount(), 2);
    QCOMPARE(view.data(view.index(1, 0), ConversationModel::BodyRole).toString(), QStringLiteral("Partial updated complete"));
    QCOMPARE(view.data(view.index(1, 0), ConversationModel::DisplayCellsRole).toList().size(), 0);
    const bool original = QSettings().value(QStringLiteral("conversation/showWhenReady"), false).toBool();
    {
        AppController controller;
        controller.setShowWhenReady(true);
        AppController restored;
        QVERIFY(restored.showWhenReady());
        controller.setShowWhenReady(original);
    }
}

void NativeCoreTest::readyModePreservesActivityAndHidesOnlyProvisionalBody() {
    QStandardItemModel source;
    const QVariantList tools{QVariantMap{{QStringLiteral("name"), QStringLiteral("Bash")}}};
    const QVariantList cells{QVariantMap{{QStringLiteral("kind"), QStringLiteral("command")}}};
    auto* live = new QStandardItem;
    live->setData(QStringLiteral("assistant"), ConversationModel::AuthorRole);
    live->setData(QStringLiteral("live"), ConversationModel::KindRole);
    live->setData(QStringLiteral("Unfinished answer"), ConversationModel::BodyRole);
    live->setData(tools, ConversationModel::ToolsRole);
    live->setData(cells, ConversationModel::DisplayCellsRole);
    live->setData(1, ConversationModel::ActivityCountRole);
    live->setData(true, ConversationModel::ToolDetailsAvailableRole);
    source.appendRow(live);
    auto* activity = new QStandardItem;
    activity->setData(true, ConversationModel::ActivityRole);
    activity->setData(QStringLiteral("Bash"), ConversationModel::ToolNameRole);
    activity->setData(QStringLiteral("Search project files"), ConversationModel::BodyRole);
    source.appendRow(activity);
    ConversationPresentationModel view;
    view.setSourceModel(&source);
    view.setShowWhenReady(true);
    QCOMPARE(view.rowCount(), 2);
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::BodyRole).toString(), QString{});
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::ToolsRole).toList(), tools);
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::DisplayCellsRole).toList(), cells);
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::ActivityCountRole).toInt(), 1);
    QVERIFY(view.data(view.index(0, 0), ConversationModel::ToolDetailsAvailableRole).toBool());
    QCOMPARE(view.data(view.index(1, 0), ConversationModel::BodyRole).toString(), QStringLiteral("Search project files"));
    view.setShowWhenReady(false);
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::BodyRole).toString(), QStringLiteral("Unfinished answer"));
    view.setShowWhenReady(true);
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::BodyRole).toString(), QString{});
    live->setData(QStringLiteral("assistant"), ConversationModel::KindRole);
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::BodyRole).toString(), QStringLiteral("Unfinished answer"));
    QCOMPARE(view.data(view.index(0, 0), ConversationModel::ToolsRole).toList(), tools);
    live->setData(QString{}, ConversationModel::BodyRole);
    live->setData(QVariantList{}, ConversationModel::ToolsRole);
    live->setData(QVariantList{}, ConversationModel::DisplayCellsRole);
    QCOMPARE(view.rowCount(), 2); // Lazy activity details remain discoverable without answer text.
    QVERIFY(view.data(view.index(0, 0), ConversationModel::ToolDetailsAvailableRole).toBool());
    view.setShowWhenReady(false);
    QCOMPARE(view.rowCount(), 2);
}

void NativeCoreTest::nextAttentionCyclesWaitingUnreadAndPending() {
    AgentListModel model;
    QJsonArray rows;
    for (const QString& name : {QStringLiteral("a"), QStringLiteral("b"), QStringLiteral("c"), QStringLiteral("d")})
        rows.append(QJsonObject{{QStringLiteral("session"), name}, {QStringLiteral("persona"), name},
            {QStringLiteral("latest_state"), name == QStringLiteral("c") ? QStringLiteral("waiting") : QStringLiteral("idle")}});
    model.applySnapshot({{QStringLiteral("agents"), rows}});
    model.applyNotificationEvent({{QStringLiteral("session"), QStringLiteral("b")}});
    QCOMPARE(model.nextAttentionSession(QStringLiteral("a")), QStringLiteral("b"));
    QCOMPARE(model.nextAttentionSession(QStringLiteral("b")), QStringLiteral("c"));
    QCOMPARE(model.nextAttentionSession(QStringLiteral("c")), QStringLiteral("b"));
    QCOMPARE(model.nextAttentionSession(QStringLiteral("c"), {QStringLiteral("d")}), QStringLiteral("d"));
    model.clearUnread(QStringLiteral("b"));
    QCOMPARE(model.nextAttentionSession(QStringLiteral("a")), QStringLiteral("c"));
    QVERIFY(model.nextAttentionSession(QStringLiteral("c")).isEmpty());
    QCOMPARE(model.nextAttentionSession(QString{}, {QStringLiteral("a")}), QStringLiteral("a"));
    model.applySnapshot({{QStringLiteral("agents"), QJsonArray{}}});
    QVERIFY(model.nextAttentionSession(QString{}, {QStringLiteral("missing")}).isEmpty());
}

void NativeCoreTest::sseParserHandlesChunksCommentsAndReplayIds() {
    SseParser parser;
    QVERIFY(parser.feed(": connected\r\n\r\nid: 41\r\ndata: {\"type\":\"agent-").isEmpty());
    const QList<SseMessage> messages = parser.feed("roster\"}\r\n\r\n");
    QCOMPARE(messages.size(), 1);
    QCOMPARE(messages.first().id, QStringLiteral("41"));
    QCOMPARE(messages.first().data.value(QStringLiteral("type")).toString(),
             QStringLiteral("agent-roster"));
}

void NativeCoreTest::rosterLookupIsConsistentDuringStructuralSignals() {
    AgentListModel model;
    const auto snapshot = [](const QStringList& names) {
        QJsonArray rows;
        for (const QString& name : names)
            rows.append(QJsonObject{{QStringLiteral("session"), name}, {QStringLiteral("persona"), name}});
        return QJsonObject{{QStringLiteral("agents"), rows}};
    };
    model.applySnapshot(snapshot({QStringLiteral("a"), QStringLiteral("b")}));
    int checked = 0;
    const auto validate = [&] {
        ++checked;
        const auto sessions = model.sessions();
        for (const QString& name : {QStringLiteral("a"), QStringLiteral("b"), QStringLiteral("c")})
            QCOMPARE(model.indexOfSession(name), sessions.indexOf(name));
    };
    connect(&model, &QAbstractItemModel::rowsRemoved, &model, validate);
    connect(&model, &QAbstractItemModel::rowsInserted, &model, validate);
    connect(&model, &QAbstractItemModel::rowsMoved, &model, validate);
    model.applySnapshot(snapshot({QStringLiteral("b"), QStringLiteral("c")}));
    QVERIFY(model.recordOutgoingActivity(QStringLiteral("c")));
    model.applySnapshot(snapshot({}));
    QVERIFY(checked >= 4);
}

void NativeCoreTest::redesignedRosterFiltersWithoutMutatingSource() {
    AgentListModel source;
    source.applySnapshot({{QStringLiteral("agents"), QJsonArray{
        QJsonObject{{QStringLiteral("session"), QStringLiteral("alpha")}, {QStringLiteral("persona"), QStringLiteral("Alpha")},
            {QStringLiteral("backend"), QStringLiteral("codex")}, {QStringLiteral("cwd"), QStringLiteral("/work/one")},
            {QStringLiteral("last_activity"), 1'788'000'000'000.0},
            {QStringLiteral("last_completed_message"), QStringLiteral("Completed preview")}},
        QJsonObject{{QStringLiteral("session"), QStringLiteral("beta")}, {QStringLiteral("persona"), QStringLiteral("Beta")},
            {QStringLiteral("backend"), QStringLiteral("claude")}, {QStringLiteral("cwd"), QStringLiteral("/work/two")}}
    }}});
    AgentFilterModel filtered;
    filtered.setSourceModel(&source);
    QCOMPARE(filtered.rowCount(), 2);
    QCOMPARE(filtered.index(0, 0).data(AgentListModel::LastCompletedMessageRole).toString(), QStringLiteral("Completed preview"));
    QCOMPARE(filtered.indexOfSession(QStringLiteral("beta")), 1);
    filtered.setQuery(QStringLiteral("WORK/TWO"));
    QCOMPARE(filtered.rowCount(), 1);
    QCOMPARE(filtered.index(0, 0).data(AgentListModel::SessionRole).toString(), QStringLiteral("beta"));
    QCOMPARE(source.rowCount(), 2);
    QCOMPARE(filtered.indexOfSession(QStringLiteral("beta")), 0);
    QCOMPARE(filtered.indexOfSession(QStringLiteral("alpha")), -1);
    filtered.setQuery({});
    filtered.setUnreadOnly(true);
    QCOMPARE(filtered.rowCount(), 0);
    source.applyNotificationEvent({{QStringLiteral("session"), QStringLiteral("alpha")}});
    QCOMPARE(filtered.rowCount(), 1);
    source.clearUnread(QStringLiteral("alpha"));
    QCOMPARE(filtered.rowCount(), 0);
    QVERIFY(!clarp::chatStamp(1'788'000'000'000).isEmpty());
}

void NativeCoreTest::circularPortraitsAreBoundedAndAntialiased() {
    QImage original(400, 240, QImage::Format_ARGB32);
    original.fill(Qt::red);
    QByteArray bytes;
    QBuffer buffer(&bytes);
    QVERIFY(buffer.open(QIODevice::WriteOnly));
    QVERIFY(original.save(&buffer, "PNG"));
    const QImage portrait = QImage::fromData(roundedPortrait(bytes));
    QCOMPARE(portrait.size(), QSize(192, 192));
    QCOMPARE(portrait.pixelColor(0, 0).alpha(), 0);
    QCOMPARE(portrait.pixelColor(96, 96), QColor(Qt::red));
    bool antialiased = false;
    for (int x = 0; x < 192; ++x) {
        const int alpha = portrait.pixelColor(x, 20).alpha();
        if (alpha > 0 && alpha < 255) antialiased = true;
    }
    QVERIFY(antialiased);
    QVERIFY(roundedPortrait(QByteArray("not an image")).isEmpty());
}

void NativeCoreTest::idleContactStartsFreshWithSavedDefaults() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    QSettings settings;
    const QVariant oldBackend = settings.value(QStringLiteral("launch/backend"));
    const QVariant oldFolder = settings.value(QStringLiteral("launch/workingDirectory"));
    const auto restore = qScopeGuard([&] {
        qputenv("CLARP_BASE_URL", oldBase); qputenv("CLARP_TOKEN", oldToken);
        settings.setValue(QStringLiteral("launch/backend"), oldBackend);
        settings.setValue(QStringLiteral("launch/workingDirectory"), oldFolder);
    });
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8());
    qputenv("CLARP_TOKEN", "test-token");
    settings.setValue(QStringLiteral("launch/backend"), QStringLiteral("codex"));
    settings.setValue(QStringLiteral("launch/workingDirectory"), QStringLiteral("/tmp/contact-workspace"));
    AppController controller;
    QTRY_VERIFY_WITH_TIMEOUT(controller.connected(), 3000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.agents()->rowCount(), 1, 3000);
    const QString freshSession = QStringLiteral("bella-server-generated-id");
    server.setJsonResponse(QStringLiteral("POST"), QStringLiteral("/agents"), 201,
        {{QStringLiteral("session"), freshSession}});
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agents/snapshot"), 200,
        {{QStringLiteral("agents"), QJsonArray{
            QJsonObject{{QStringLiteral("session"), QStringLiteral("rachel")},
                        {QStringLiteral("persona"), QStringLiteral("Rachel")},
                        {QStringLiteral("backend"), QStringLiteral("codex")}},
            QJsonObject{{QStringLiteral("session"), freshSession},
                        {QStringLiteral("persona"), QStringLiteral("Bella")},
                        {QStringLiteral("backend"), QStringLiteral("codex")}}
        }}});
    QSignalSpy focusRequested(&controller, &AppController::composerFocusPaneChanged);
    controller.contacts()->applySnapshot({{QStringLiteral("personas"), QJsonArray{
        QJsonObject{{QStringLiteral("name"), QStringLiteral("Bella")}},
        QJsonObject{{QStringLiteral("name"), QStringLiteral("Rachel")}},
    }}}, QSet<QString>{QStringLiteral("rachel")});
    QCOMPARE(controller.matchingContacts(QStringLiteral("bell")).size(), 1);
    QVERIFY(!controller.quickStartContact(QStringLiteral("Rachel")));
    QVERIFY(controller.quickStartContact(QStringLiteral("bella")));
    QCOMPARE(controller.startingContact(), QStringLiteral("Bella"));
    QVERIFY(!controller.quickStartContact(QStringLiteral("Bella")));
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/agents")), 3000);
    const QJsonObject request = server.requestJson(QStringLiteral("POST"), QStringLiteral("/agents"));
    QCOMPARE(request.value(QStringLiteral("name")).toString(), QStringLiteral("Bella"));
    QCOMPARE(request.value(QStringLiteral("cwd")).toString(), QStringLiteral("/tmp/contact-workspace"));
    QCOMPARE(request.value(QStringLiteral("backend")).toString(), QStringLiteral("codex"));
    QVERIFY(!request.contains(QStringLiteral("session")));
    QVERIFY(!request.contains(QStringLiteral("replace_sid")));
    QVERIFY(!request.contains(QStringLiteral("resume_session_id")));
    QVERIFY(!request.contains(QStringLiteral("fork_session_id")));
    QCOMPARE(server.requestCount(QStringLiteral("POST"), QStringLiteral("/agents")), 1);
    QTRY_VERIFY_WITH_TIMEOUT(controller.startingContact().isEmpty(), 3000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.agents()->rowCount(), 2, 3000);
    QCOMPARE(controller.selectedSession(), freshSession);
    QCOMPARE(controller.panes()->activeSession(), freshSession);
    QCOMPARE(controller.composerFocusPane(), controller.panes()->activePaneId());
    QVERIFY(!focusRequested.isEmpty());

    // A contact can become occupied after the picker opens on another client.
    controller.contacts()->applySnapshot({{QStringLiteral("personas"), QJsonArray{
        QJsonObject{{QStringLiteral("name"), QStringLiteral("Theo")}}
    }}}, {});
    server.setJsonResponse(QStringLiteral("POST"), QStringLiteral("/agents"), 409,
        {{QStringLiteral("error"), QStringLiteral("contact_occupied")},
         {QStringLiteral("message"), QStringLiteral("Theo already has an active session")}});
    QVERIFY(controller.quickStartContact(QStringLiteral("Theo")));
    QTRY_VERIFY_WITH_TIMEOUT(controller.startingContact().isEmpty(), 3000);
    QVERIFY(!controller.errorMessage().isEmpty());
    QCOMPARE(controller.selectedSession(), freshSession);
}

void NativeCoreTest::newAgentWaitsForOwnRosterAndRejectsLateSnapshots() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    const auto restore = qScopeGuard([&] { qputenv("CLARP_BASE_URL", oldBase); qputenv("CLARP_TOKEN", oldToken); });
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8()); qputenv("CLARP_TOKEN", "test-token");
    AppController controller;
    QTRY_VERIFY(controller.connected());
    QTRY_COMPARE(controller.selectedSession(), QStringLiteral("rachel"));
    server.holdNextSnapshot(); controller.refreshAgents(); QTRY_VERIFY(server.hasHeldSnapshot());
    const QJsonObject old{{QStringLiteral("agents"), QJsonArray{QJsonObject{
        {QStringLiteral("session"), QStringLiteral("rachel")}, {QStringLiteral("persona"), QStringLiteral("Rachel")}}}}};
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agents/snapshot"), 200, old);
    server.setJsonResponse(QStringLiteral("POST"), QStringLiteral("/agents"), 201,
        {{QStringLiteral("session"), QStringLiteral("new-codex")}});
    QSignalSpy ready(&controller, &AppController::agentMutationSucceeded);
    const auto snapshotsBeforeCreate = server.requestCount(QStringLiteral("GET"), QStringLiteral("/agents/snapshot"));
    QVERIFY(controller.startAnonymousAgent(QStringLiteral("codex"), {}, {}));
    QTRY_VERIFY(server.requestCount(QStringLiteral("GET"), QStringLiteral("/agents/snapshot")) > snapshotsBeforeCreate);
    QTest::qWait(30);
    QCOMPARE(ready.size(), 0);
    QVERIFY(!controller.startingContact().isEmpty());
    QVERIFY(controller.startAnonymousAgent(QStringLiteral("codex"), {}, {}));
    QCOMPARE(server.requestCount(QStringLiteral("POST"), QStringLiteral("/agents")), 1);
    QJsonArray rows = old.value(QStringLiteral("agents")).toArray();
    rows.append(QJsonObject{{QStringLiteral("session"), QStringLiteral("new-codex")},
                           {QStringLiteral("persona"), QStringLiteral("Codex-1234")},
                           {QStringLiteral("backend"), QStringLiteral("codex")}});
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agents/snapshot"), 200,
                           {{QStringLiteral("agents"), rows}});
    controller.refreshAgents();
    QTRY_COMPARE(ready.size(), 1);
    QCOMPARE(controller.selectedSession(), QStringLiteral("new-codex"));
    server.releaseHeldSnapshot(old); QTest::qWait(100);
    QCOMPARE(controller.selectedSession(), QStringLiteral("new-codex"));
    QCOMPARE(controller.panes()->activeSession(), QStringLiteral("new-codex"));
    QCOMPARE(controller.agents()->rowCount(), 2);
}

void NativeCoreTest::fastLaunchOpensWithoutWaitingForFleet() {
    FakeClarpServer server; QVERIFY(server.listenLocal());
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    const auto oldLaunch=qApp->property("clarpLaunchMode");
    const auto restore=qScopeGuard([&] { qputenv("CLARP_BASE_URL",oldBase); qputenv("CLARP_TOKEN",oldToken); qApp->setProperty("clarpLaunchMode",oldLaunch); });
    qputenv("CLARP_BASE_URL",server.baseUrl().toUtf8()); qputenv("CLARP_TOKEN","test-token");
    qApp->setProperty("clarpLaunchMode",true);
    AppController controller;
    QTRY_VERIFY(controller.connected());
    server.sendEvent({{QStringLiteral("type"),QStringLiteral("tts-error")},
                      {QStringLiteral("message"),QStringLiteral("Other agent speech failed")}});
    QTest::qWait(50);
    QVERIFY(controller.errorMessage().isEmpty());
    QCOMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot")),0);
    QCOMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agent-conversations")),0);
    const QJsonObject agent{{QStringLiteral("agent_id"),QStringLiteral("new-id")},
        {QStringLiteral("session"),QStringLiteral("new-fast")},{QStringLiteral("persona"),QStringLiteral("Codex-fast")},
        {QStringLiteral("backend"),QStringLiteral("codex")},{QStringLiteral("alive"),true}};
    server.setJsonResponse(QStringLiteral("POST"),QStringLiteral("/agents"),201,
        {{QStringLiteral("session"),QStringLiteral("new-fast")},{QStringLiteral("agent"),agent}});
    server.setJsonResponse(QStringLiteral("GET"),QStringLiteral("/agents/snapshot"),200,
        {{QStringLiteral("agents"),QJsonArray{agent}}});
    server.holdNextSnapshot();
    QSignalSpy ready(&controller,&AppController::agentMutationSucceeded);
    controller.setLaunchDirectory(QStringLiteral("/work/chosen directory"));
    QVERIFY(controller.startAnonymousAgent(QStringLiteral("codex"),{},{}));
    QTRY_COMPARE(ready.size(),1);
    QCOMPARE(server.requestJson(QStringLiteral("POST"),QStringLiteral("/agents")).value(QStringLiteral("cwd")).toString(),QStringLiteral("/work/chosen directory"));
    QCOMPARE(controller.selectedSession(),QStringLiteral("new-fast"));
    QCOMPARE(controller.panes()->activeSession(),QStringLiteral("new-fast"));
    QCOMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot")),0);
    QTRY_VERIFY(server.hasHeldSnapshot());
    const auto before=server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot"));
    for (int i=0;i<20;++i) controller.refreshAgents();
    QTest::qWait(50);
    QCOMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot")),before);
    server.releaseHeldSnapshot({{QStringLiteral("agents"),QJsonArray{agent}}});
    QTRY_COMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot")),before+1);
    QCOMPARE(controller.selectedSession(),QStringLiteral("new-fast"));
}

void NativeCoreTest::resumeLaunchOpensExactSessionWithoutFleet() {
    FakeClarpServer server; QVERIFY(server.listenLocal());
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    const auto oldLaunch=qApp->property("clarpLaunchMode");
    const auto restore=qScopeGuard([&] { qputenv("CLARP_BASE_URL",oldBase); qputenv("CLARP_TOKEN",oldToken); qApp->setProperty("clarpLaunchMode",oldLaunch); });
    qputenv("CLARP_BASE_URL",server.baseUrl().toUtf8()); qputenv("CLARP_TOKEN","test-token");
    qApp->setProperty("clarpLaunchMode",true);
    AppController controller;
    QTRY_VERIFY(controller.connected());
    server.sendEvent({{QStringLiteral("type"),QStringLiteral("tts-error")},
                      {QStringLiteral("message"),QStringLiteral("Other agent speech failed")}});
    QTest::qWait(50);
    QVERIFY(controller.errorMessage().isEmpty());
    QCOMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot")),0);
    QCOMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agent-conversations")),0);
    const QJsonObject agent{{QStringLiteral("agent_id"),QStringLiteral("new-id")},
        {QStringLiteral("session"),QStringLiteral("new-fast")},{QStringLiteral("persona"),QStringLiteral("Codex-fast")},
        {QStringLiteral("backend"),QStringLiteral("codex")},{QStringLiteral("alive"),true}};
    server.setJsonResponse(QStringLiteral("POST"),QStringLiteral("/agents"),201,
        {{QStringLiteral("session"),QStringLiteral("new-fast")},{QStringLiteral("agent"),agent}});
    server.setJsonResponse(QStringLiteral("GET"),QStringLiteral("/agents/snapshot"),200,
        {{QStringLiteral("agents"),QJsonArray{agent}}});
    server.holdNextSnapshot();
    QSignalSpy ready(&controller,&AppController::agentMutationSucceeded);
    controller.setLaunchDirectory(QStringLiteral("/work/chosen directory"));
    QVERIFY(controller.resumeLaunchSession(QStringLiteral("codex"),QStringLiteral("native-session"),true));
    QTRY_COMPARE(ready.size(),1);
    const auto payload = server.requestJson(QStringLiteral("POST"),QStringLiteral("/agents"));
    QCOMPARE(payload.value(QStringLiteral("resume_session_id")).toString(),QStringLiteral("native-session"));
    QVERIFY(payload.value(QStringLiteral("open_existing")).toBool());
    QVERIFY(!payload.contains(QStringLiteral("model")));

    QCOMPARE(server.requestJson(QStringLiteral("POST"),QStringLiteral("/agents")).value(QStringLiteral("cwd")).toString(),QStringLiteral("/work/chosen directory"));
    QCOMPARE(controller.selectedSession(),QStringLiteral("new-fast"));
    QCOMPARE(controller.panes()->activeSession(),QStringLiteral("new-fast"));
    QCOMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot")),0);
    QTRY_VERIFY(server.hasHeldSnapshot());
    const auto before=server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot"));
    for (int i=0;i<20;++i) controller.refreshAgents();
    QTest::qWait(50);
    QCOMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot")),before);
    server.releaseHeldSnapshot({{QStringLiteral("agents"),QJsonArray{agent}}});
    QTRY_COMPARE(server.requestCount(QStringLiteral("GET"),QStringLiteral("/agents/snapshot")),before+1);
    QCOMPARE(controller.selectedSession(),QStringLiteral("new-fast"));
}

void NativeCoreTest::launchPoolCarriesBackendModelAndHandlesEmpty() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    const auto oldBackend = QSettings().value(QStringLiteral("launch/backend"));
    const auto restore = qScopeGuard([&] {
        QSettings().setValue(QStringLiteral("launch/backend"), oldBackend);
        qputenv("CLARP_BASE_URL", oldBase); qputenv("CLARP_TOKEN", oldToken);
    });
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8());
    qputenv("CLARP_TOKEN", "test-token");
    AppController controller;
    QTRY_VERIFY_WITH_TIMEOUT(controller.connected(), 3000);
    QTRY_COMPARE_WITH_TIMEOUT(controller.agents()->rowCount(), 1, 3000);
    const QJsonArray personas{
        QJsonObject{{QStringLiteral("name"), QStringLiteral("Rachel")}},
        QJsonObject{{QStringLiteral("name"), QStringLiteral("Bella")}}};
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agents/snapshot"), 200,
        {{QStringLiteral("agents"), QJsonArray{QJsonObject{
            {QStringLiteral("session"), QStringLiteral("rachel")},
            {QStringLiteral("persona"), QStringLiteral("Rachel")}}}},
         {QStringLiteral("personas"), personas}});
    server.setJsonResponse(QStringLiteral("POST"), QStringLiteral("/agents"), 201,
        {{QStringLiteral("session"), QStringLiteral("bella-new")}});
    QVERIFY(controller.startAvailableContact(QStringLiteral("grok"), QStringLiteral("test-model"), QStringLiteral("high")));
    QVERIFY(!controller.startAvailableContact(QStringLiteral("claude"), {}, {}));
    QTRY_COMPARE_WITH_TIMEOUT(server.requestCount(QStringLiteral("POST"), QStringLiteral("/agents")), 1, 3000);
    const auto request = server.requestJson(QStringLiteral("POST"), QStringLiteral("/agents"));
    QVERIFY(request.value(QStringLiteral("auto_contact")).toBool());
    QVERIFY(!request.contains(QStringLiteral("name")));
    QCOMPARE(request.value(QStringLiteral("backend")).toString(), QStringLiteral("grok"));
    QCOMPARE(request.value(QStringLiteral("model")).toString(), QStringLiteral("test-model"));
    QCOMPARE(request.value(QStringLiteral("effort")).toString(), QStringLiteral("high"));
    QVERIFY(!request.contains(QStringLiteral("replace_sid")));
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agents/snapshot"), 200,
        {{QStringLiteral("agents"), QJsonArray{QJsonObject{
            {QStringLiteral("session"), QStringLiteral("bella-new")},
            {QStringLiteral("persona"), QStringLiteral("Bella")},
            {QStringLiteral("backend"), QStringLiteral("grok")}}}}});
    controller.refreshAgents();
    QTRY_VERIFY_WITH_TIMEOUT(controller.startingContact().isEmpty(), 3000);
    QCOMPARE(controller.lastBackend(), QStringLiteral("grok"));
    QSignalSpy empty(&controller, &AppController::launchPoolEmpty);
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agents/snapshot"), 200,
        {{QStringLiteral("agents"), QJsonArray{}}, {QStringLiteral("personas"), QJsonArray{}}});
    server.setJsonResponse(QStringLiteral("POST"), QStringLiteral("/agents"), 409,
        {{QStringLiteral("error"), QStringLiteral("contact_pool_empty")}});
    QVERIFY(controller.startAvailableContact(QStringLiteral("codex"), {}, {}));
    QTRY_COMPARE_WITH_TIMEOUT(empty.size(), 1, 3000);
    QCOMPARE(server.requestCount(QStringLiteral("POST"), QStringLiteral("/agents")), 2);
}

void NativeCoreTest::agentTerminalLaunchesNativeCliThroughDefaultTerminal() {
    QTemporaryDir directory;
    QVERIFY(directory.isValid());
    const QString workspace = directory.filePath(QStringLiteral("project with spaces"));
    QVERIFY(QDir().mkpath(workspace));
    const QString capture = directory.filePath(QStringLiteral("arguments"));
    const auto executable = [&directory](const QString& name, const QByteArray& contents) {
        QFile file(directory.filePath(name));
        if (!file.open(QIODevice::WriteOnly) || file.write(contents) != contents.size()) return false;
        return file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner);
    };
    QVERIFY(executable(QStringLiteral("xdg-terminal-exec"),
        "#!/bin/sh\nprintf '%s\\n' \"$PWD\" \"$@\" > \"$CLARP_TEST_TERMINAL_CAPTURE\"\n"));
    for (const auto& name : {QStringLiteral("claude"), QStringLiteral("codex"),
                             QStringLiteral("agy"), QStringLiteral("grok")}) {
        QVERIFY(executable(name, "#!/bin/sh\nexit 0\n"));
    }
    const auto oldPath = qgetenv("PATH");
    const auto oldCapture = qgetenv("CLARP_TEST_TERMINAL_CAPTURE");
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    const auto restore = qScopeGuard([&] {
        qputenv("PATH", oldPath); qputenv("CLARP_TEST_TERMINAL_CAPTURE", oldCapture);
        qputenv("CLARP_BASE_URL", oldBase); qputenv("CLARP_TOKEN", oldToken);
    });
    qputenv("PATH", directory.path().toUtf8());
    qputenv("CLARP_TEST_TERMINAL_CAPTURE", capture.toUtf8());
    qputenv("CLARP_BASE_URL", "http://127.0.0.1:1");
    qputenv("CLARP_TOKEN", "terminal-fixture");
    AppController controller;
    controller.setSharedFilesystem(true);
    for (const auto& backend : {QStringLiteral("claude"), QStringLiteral("codex"),
                                QStringLiteral("agy"), QStringLiteral("grok")}) {
        QFile::remove(capture);
        controller.agents()->applySnapshot({{QStringLiteral("agents"), QJsonArray{QJsonObject{
            {QStringLiteral("agent_id"), QStringLiteral("agent-terminal")},
            {QStringLiteral("session"), QStringLiteral("agent")},
            {QStringLiteral("persona"), QStringLiteral("Agent")},
            {QStringLiteral("backend"), backend},
            {QStringLiteral("cwd"), workspace},
            {QStringLiteral("conversation_id"), QStringLiteral("native-session;literal")},
        }}}});
        controller.openAgentTerminal(QStringLiteral("agent"));
        QTRY_VERIFY_WITH_TIMEOUT(QFileInfo(capture).size() > 0, 3000);
        QFile file(capture);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const auto arguments = QString::fromUtf8(file.readAll()).split(u'\n', Qt::SkipEmptyParts);
        QCOMPARE(arguments, QStringList({workspace, QStringLiteral("--dir=") + workspace,
            QStringLiteral("--title=Agent — ") + backend, QStringLiteral("--"),
            QStringLiteral("env"), QStringLiteral("-u"), QStringLiteral("CLARP_TOKEN"),
            QStringLiteral("CLAUDE_PWA_SESSION=agent"),
            directory.filePath(backend), backend == QStringLiteral("codex") ? QStringLiteral("resume")
                : backend == QStringLiteral("agy") ? QStringLiteral("--conversation") : QStringLiteral("--resume"),
            QStringLiteral("native-session;literal")}));
    }
    QFile::remove(capture);
    controller.setSharedFilesystem(false);
    controller.openAgentTerminal(QStringLiteral("agent"));
    QVERIFY(!QFileInfo::exists(capture));
}

void NativeCoreTest::onlyWebAndMailLinksAreOpenable() {
    QVERIFY(isOpenableLink(QStringLiteral("https://example.com/a?b=1#c")));
    QVERIFY(isOpenableLink(QStringLiteral("http://example.com")));
    QVERIFY(isOpenableLink(QStringLiteral("  https://example.com/padded  ")));
    QVERIFY(isOpenableLink(QStringLiteral("HTTPS://Example.com/upper")));
    QVERIFY(isOpenableLink(QStringLiteral("mailto:team@example.com")));

    // Transcript text is untrusted output; nothing else may reach the handler.
    QVERIFY(!isOpenableLink(QStringLiteral("file:///etc/passwd")));
    QVERIFY(!isOpenableLink(QStringLiteral("https:///etc/passwd")));
    QVERIFY(!isOpenableLink(QStringLiteral("javascript:alert(1)")));
    QVERIFY(!isOpenableLink(QStringLiteral("smb://host/share")));
    QVERIFY(!isOpenableLink(QStringLiteral("ssh://box/repo")));
    QVERIFY(!isOpenableLink(QStringLiteral("mailto:")));
    QVERIFY(!isOpenableLink(QStringLiteral("/plain/path")));
    QVERIFY(!isOpenableLink(QString{}));
}

void NativeCoreTest::toolOutputLinksAreAnchoredWithoutChangingTheText() {
    const QString output =
        QStringLiteral("  branch pushed\n\tsee https://example.com/pr/7 (open it)\n"
                       "contact www.example.com now\n<not a tag> & \"quoted\"");
    const QString rich = linkifiedPlainText(output);

    // Anchors point at real targets; a bare host gains an https scheme.
    QVERIFY(rich.contains(QStringLiteral("<a href=\"https://example.com/pr/7\">")));
    QVERIFY(rich.contains(QStringLiteral("<a href=\"https://www.example.com\">")));
    // Trailing prose punctuation stays outside the target.
    QVERIFY(!rich.contains(QStringLiteral("pr/7(")));
    QVERIFY(!rich.contains(QStringLiteral("href=\"https://example.com/pr/7(")));
    // Original characters are escaped rather than interpreted as markup.
    QVERIFY(rich.contains(QStringLiteral("&lt;not a tag&gt; &amp; &quot;quoted&quot;")));
    // Whitespace fidelity depends on pre-wrap, not on a <pre> that would overflow.
    QVERIFY(rich.startsWith(QStringLiteral("<div style=\"white-space: pre-wrap;\">")));

    // The visible text must survive the round trip byte for byte.
    QTextDocument document;
    document.setHtml(rich);
    QCOMPARE(document.toPlainText(), output);

    // Output with no URL still round-trips, and oversized output opts out.
    QCOMPARE(linkifiedPlainText(QStringLiteral("plain\n  text")),
             QStringLiteral("<div style=\"white-space: pre-wrap;\">plain\n  text</div>"));
    QVERIFY(maxLinkifiedTextLength > 0);
}

void NativeCoreTest::reportHtmlKeepsStructureButNeverFetchesRemoteResources() {
    const QString host = QStringLiteral("https://host.example");
    const QString report = QStringLiteral(
        "<html><head><style>@import url(\"http://tracker.example/x.css\");"
        "body{background-image:url('http://tracker.example/bg.png')}"
        ".c{background:url(http://tracker.example/short.png)}</style></head><body>"
        "<h1>Quarterly report</h1>"
        "<p style=\"background-image:url(http://tracker.example/inline.png)\">Body</p>"
        "<img src=\"http://tracker.example/pixel.png\">"
        "<img src=\"data:image/gif;base64,R0lGODlh\">"
        "<img src=\"https://host.example/static/chart.png\">"
        "<table background=\"http://tracker.example/tablebg.png\"><tr><td>1</td></tr></table>"
        "<script>fetch('http://tracker.example/beacon')</script>"
        "<a href=\"https://example.com/source\">source</a>"
        "</body></html>");

    const QString safe = sanitizedReportHtml(report, host);

    // Nothing that would reach the tracker may survive, in any vector.
    QVERIFY(!safe.contains(QStringLiteral("tracker.example")));
    QVERIFY(!safe.contains(QStringLiteral("@import"), Qt::CaseInsensitive));
    QVERIFY(!safe.contains(QStringLiteral("<script"), Qt::CaseInsensitive));
    QVERIFY(!safe.contains(QStringLiteral("beacon")));

    // Self-contained and Host-served resources are preserved.
    QVERIFY(safe.contains(QStringLiteral("data:image/gif;base64,R0lGODlh")));
    QVERIFY(safe.contains(QStringLiteral("https://host.example/static/chart.png")));

    // The readable report survives: headings, text, tables and outbound links.
    QVERIFY(safe.contains(QStringLiteral("<h1>Quarterly report</h1>")));
    QVERIFY(safe.contains(QStringLiteral("https://example.com/source")));
    QTextDocument document;
    document.setHtml(safe);
    const QString plain = document.toPlainText();
    QVERIFY(plain.contains(QStringLiteral("Quarterly report")));
    QVERIFY(plain.contains(QStringLiteral("Body")));
    QVERIFY(plain.contains(QStringLiteral("source")));

    // An empty host origin must not turn into a prefix that matches everything.
    const QString noHost = sanitizedReportHtml(report, QString{});
    QVERIFY(!noHost.contains(QStringLiteral("tracker.example")));
    QVERIFY(!noHost.contains(QStringLiteral("host.example/static")));
    QVERIFY(noHost.contains(QStringLiteral("data:image/gif")));

    // Markdown must not be pushed through the HTML renderer, and vice versa.
    QVERIFY(looksLikeHtmlReport(QStringLiteral("<h1>Title</h1><p>x</p>")));
    QVERIFY(looksLikeHtmlReport(QStringLiteral("<!DOCTYPE html><html><body>x</body></html>")));
    QVERIFY(!looksLikeHtmlReport(QStringLiteral("# Title\n\nA paragraph with <not-a-tag>.")));
    QVERIFY(!looksLikeHtmlReport(QStringLiteral("Plain text 1 < 2 and 3 > 2.")));
}

void NativeCoreTest::reportHtmlCannotFetchRemoteResources() {
    // Every one of these was observed making a real request through Qt's rich
    // text engine before sanitizing; see sanitizedReportHtml's comment.
    const QString host = QStringLiteral("https://host.example");
    const QString report = QStringLiteral(
        "<html><head><style>"
        "@import url(\"http://tracker.example/imported.css\");"
        "body{background-image:url('http://tracker.example/bg.png')}"
        ".x{background:url(http://tracker.example/shorthand.png)}"
        "</style></head><body>"
        "<p style=\"background-image:url(http://tracker.example/inline.png)\">styled</p>"
        "<img src=\"http://tracker.example/pixel.png\">"
        "<table background=\"http://tracker.example/tablebg.png\"><tr><td>c</td></tr></table>"
        "<script>fetch('http://tracker.example/beacon')</script>"
        "</body></html>");

    const QString safe = sanitizedReportHtml(report, host);
    QVERIFY(!safe.contains(QStringLiteral("tracker.example")));
    QVERIFY(!safe.contains(QStringLiteral("@import"), Qt::CaseInsensitive));
    QVERIFY(!safe.contains(QStringLiteral("<script"), Qt::CaseInsensitive));
    QVERIFY(!safe.contains(QStringLiteral("beacon")));
    // The readable report survives the rewrite.
    QVERIFY(safe.contains(QStringLiteral("styled")));
    QVERIFY(safe.contains(QStringLiteral("<table")));

    // Self-contained and Host-served resources still resolve.
    const QString kept = sanitizedReportHtml(
        QStringLiteral("<img src=\"data:image/gif;base64,R0lGODlh\">"
                       "<img src=\"https://host.example/static/avatars/a.png\">"
                       "<p style=\"background-image:url('data:image/png;base64,AAA')\">x</p>"),
        host);
    QVERIFY(kept.contains(QStringLiteral("data:image/gif;base64,R0lGODlh")));
    QVERIFY(kept.contains(QStringLiteral("https://host.example/static/avatars/a.png")));
    QVERIFY(kept.contains(QStringLiteral("url('data:image/png;base64,AAA')")));

    // With no Host origin known, only self-contained data URLs may load.
    const QString noHost = sanitizedReportHtml(
        QStringLiteral("<img src=\"https://host.example/x.png\">"), QString{});
    QVERIFY(!noHost.contains(QStringLiteral("host.example")));

    // A protocol-relative or scheme-confused reference must not slip through.
    const QString tricky = sanitizedReportHtml(
        QStringLiteral("<img src=\"//tracker.example/p.png\">"
                       "<img src=' http://tracker.example/pad.png '>"
                       "<IMG SRC=HTTP://TRACKER.EXAMPLE/UPPER.PNG>"), host);
    QVERIFY(!tricky.contains(QStringLiteral("tracker"), Qt::CaseInsensitive));

    QVERIFY(looksLikeHtmlReport(QStringLiteral("<html><body><h1>R</h1></body></html>")));
    QVERIFY(looksLikeHtmlReport(QStringLiteral("<div class=\"card\">x</div>")));
    QVERIFY(!looksLikeHtmlReport(QStringLiteral("# Heading\n\nPlain **markdown** only.")));
}

void NativeCoreTest::reportForArtifactExposesSanitizedBody() {
    AppController controller;
    qputenv("CLARP_SCREENSHOT_PATH", QByteArray("/dev/null"));
    controller.seedScreenshotArtifacts({QVariantMap{
        {QStringLiteral("artifact_id"), QStringLiteral("r1")},
        {QStringLiteral("type"), QStringLiteral("document")},
        {QStringLiteral("title"), QStringLiteral("Deployment review")},
        {QStringLiteral("content"), QStringLiteral(
            "<h1>Deployment review</h1><img src=\"http://tracker.example/p.png\">")}}});
    qunsetenv("CLARP_SCREENSHOT_PATH");

    const QVariantMap report = controller.reportForArtifact(QStringLiteral("r1"));
    QCOMPARE(report.value(QStringLiteral("title")).toString(),
             QStringLiteral("Deployment review"));
    QVERIFY(report.value(QStringLiteral("isHtml")).toBool());
    const QString body = report.value(QStringLiteral("body")).toString();
    QVERIFY(!body.isEmpty());
    QVERIFY(body.contains(QStringLiteral("<h1>Deployment review</h1>")));
    QVERIFY(!body.contains(QStringLiteral("tracker.example")));

    // A Markdown body is passed through untouched for the Markdown renderer.
    qputenv("CLARP_SCREENSHOT_PATH", QByteArray("/dev/null"));
    controller.seedScreenshotArtifacts({QVariantMap{
        {QStringLiteral("artifact_id"), QStringLiteral("r2")},
        {QStringLiteral("type"), QStringLiteral("research")},
        {QStringLiteral("content"), QStringLiteral("# Findings\n\nOne thing.")}}});
    qunsetenv("CLARP_SCREENSHOT_PATH");
    const QVariantMap markdown = controller.reportForArtifact(QStringLiteral("r2"));
    QVERIFY(!markdown.value(QStringLiteral("isHtml")).toBool());
    QCOMPARE(markdown.value(QStringLiteral("body")).toString(),
             QStringLiteral("# Findings\n\nOne thing."));

    // Types without a readable body are not offered as reports.
    QVERIFY(!controller.artifactIsViewableReport(QVariantMap{
        {QStringLiteral("type"), QStringLiteral("countdown")},
        {QStringLiteral("content"), QStringLiteral("<h1>x</h1>")}}));
    QVERIFY(!controller.artifactIsViewableReport(QVariantMap{
        {QStringLiteral("type"), QStringLiteral("document")},
        {QStringLiteral("content"), QStringLiteral("   ")}}));
    QVERIFY(controller.reportForArtifact(QStringLiteral("missing")).isEmpty());
}

void NativeCoreTest::portedUrlsBecomeLinksWithoutChangingVisibleText() {
    // The reported case: md4c does not autolink a URL with an explicit port,
    // so this rendered as inert text while the same URL without :14443 linked.
    const QString reported =
        QStringLiteral("Open: https://elitebook.tailf14237.ts.net:14443/final/");
    const QString fixed = markdownWithExplicitAutolinks(reported);
    QCOMPARE(fixed, QStringLiteral(
        "Open: <https://elitebook.tailf14237.ts.net:14443/final/>"));

    // Qt must now see a real link, and the reader must show the original text.
    QTextDocument document;
    document.setMarkdown(fixed);
    QCOMPARE(document.toPlainText(),
             QStringLiteral("Open: https://elitebook.tailf14237.ts.net:14443/final/"));
    QVERIFY(document.toHtml().contains(
        QStringLiteral("href=\"https://elitebook.tailf14237.ts.net:14443/final/\"")));

    // Ports in every shape agents actually produce.
    QVERIFY(markdownWithExplicitAutolinks(QStringLiteral("http://127.0.0.1:8080/x"))
                .contains(QStringLiteral("<http://127.0.0.1:8080/x>")));
    QVERIFY(markdownWithExplicitAutolinks(QStringLiteral("see https://host:7699 now"))
                .contains(QStringLiteral("<https://host:7699>")));

    // Unported URLs md4c already handles must not be rewritten.
    QCOMPARE(markdownWithExplicitAutolinks(QStringLiteral("https://example.com/final/")),
             QStringLiteral("https://example.com/final/"));

    // Existing markdown links and autolinks must be left exactly as written.
    const QString labelled =
        QStringLiteral("[the lab](https://host:14443/final/) and <https://host:9/x>");
    QCOMPARE(markdownWithExplicitAutolinks(labelled), labelled);

    // Code must stay literal: an inline span, a fence, and an indented block.
    QCOMPARE(markdownWithExplicitAutolinks(QStringLiteral("run `curl https://host:14443/x`")),
             QStringLiteral("run `curl https://host:14443/x`"));
    const QString fenced =
        QStringLiteral("```\ncurl https://host:14443/x\n```\nthen https://host:14443/y");
    const QString fencedFixed = markdownWithExplicitAutolinks(fenced);
    QVERIFY(fencedFixed.contains(QStringLiteral("curl https://host:14443/x\n")));
    QVERIFY(!fencedFixed.contains(QStringLiteral("<https://host:14443/x>")));
    QVERIFY(fencedFixed.contains(QStringLiteral("then <https://host:14443/y>")));
    QCOMPARE(markdownWithExplicitAutolinks(QStringLiteral("    https://host:14443/x")),
             QStringLiteral("    https://host:14443/x"));

    // Trailing prose punctuation must not be swallowed into the target.
    QCOMPARE(markdownWithExplicitAutolinks(QStringLiteral("go to https://host:14443/x).")),
             QStringLiteral("go to <https://host:14443/x>)."));
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
    const auto roles = qvariant_cast<QList<int>>(changes.at(0).at(2));
    QVERIFY(roles.contains(ConversationModel::BodyRole));
    QVERIFY(roles.contains(ConversationModel::RevisionRole));
    QVERIFY(!roles.contains(ConversationModel::ToolsRole));
    QVERIFY(!roles.contains(ConversationModel::DisplayCellsRole));
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

void NativeCoreTest::emptyStartupWaitsForExplicitChoiceAndRetryTargetsLatestFailure() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    const QVariant oldEmpty = QCoreApplication::instance()->property("clarpEmptyStartup");
    const auto restore = qScopeGuard([&] {
        qputenv("CLARP_BASE_URL", oldBase); qputenv("CLARP_TOKEN", oldToken);
        QCoreApplication::instance()->setProperty("clarpEmptyStartup", oldEmpty);
    });
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8());
    qputenv("CLARP_TOKEN", "test-token");
    QCoreApplication::instance()->setProperty("clarpEmptyStartup", true);
    AppController controller;
    QTRY_COMPARE(controller.agents()->rowCount(), 1);
    QVERIFY(controller.selectedSession().isEmpty());
    QVERIFY(controller.panes()->activeSession().isEmpty());
    QCOMPARE(server.requestCount(QStringLiteral("POST"), QStringLiteral("/select")), 0);
    controller.selectSession(QStringLiteral("rachel"));
    QTRY_VERIFY(server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/select")));
    QCOMPARE(controller.selectedSession(), QStringLiteral("rachel"));
    auto* model = controller.conversationForSession(QStringLiteral("rachel"));
    model->addOptimistic(QStringLiteral("older-failure"), QStringLiteral("Earlier failed text"));
    model->markDeliveryFailed(QStringLiteral("older-failure"));
    model->addOptimistic(QStringLiteral("latest-failure"), QStringLiteral("Latest failed text"));
    model->markDeliveryFailed(QStringLiteral("latest-failure"));
    auto* other = controller.conversationForSession(QStringLiteral("other"));
    other->addOptimistic(QStringLiteral("other-failure"), QStringLiteral("Other chat text"));
    other->markDeliveryFailed(QStringLiteral("other-failure"));
    controller.retryLatestFailedMessage();
    QTRY_VERIFY(server.receivedRequest(QStringLiteral("POST"), QStringLiteral("/send")));
    QCOMPARE(server.requestJson(QStringLiteral("POST"), QStringLiteral("/send")).value(QStringLiteral("text")).toString(), QStringLiteral("Latest failed text"));
    QCOMPARE(server.requestJson(QStringLiteral("POST"), QStringLiteral("/send")).value(QStringLiteral("session")).toString(), QStringLiteral("rachel"));
    QVERIFY(model->indexOfMessage(QStringLiteral("u-older-failure")) >= 0);
    QVERIFY(other->indexOfMessage(QStringLiteral("u-other-failure")) >= 0);
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

void NativeCoreTest::paneActivationAlwaysTargetsItsComposer() {
    const QByteArray previousBaseUrl = qgetenv("CLARP_BASE_URL");
    const QByteArray previousToken = qgetenv("CLARP_TOKEN");
    qputenv("CLARP_BASE_URL", "http://composer-focus-test.invalid");
    qputenv("CLARP_TOKEN", "offline-composer-focus-test");

    AppController controller;
    const QString firstPane = controller.panes()->activePaneId();
    QCOMPARE(controller.composerFocusPane(), firstPane);

    controller.panes()->splitActive(QStringLiteral("vertical"), QStringLiteral("other"));
    const QString secondPane = controller.panes()->activePaneId();
    QVERIFY(secondPane != firstPane);
    QCOMPARE(controller.composerFocusPane(), secondPane);

    controller.requestComposerFocus({});
    controller.panes()->focusPane(firstPane);
    QCOMPARE(controller.composerFocusPane(), firstPane);

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
    const QUrl inlineSource = controller.mediaSource(QStringLiteral("asset-1"));
    QVERIFY(inlineSource.isLocalFile());
    QFile inlineImage(inlineSource.toLocalFile());
    QVERIFY(inlineImage.open(QIODevice::ReadOnly));
    QVERIFY(inlineImage.readAll().startsWith(QByteArray("\x89PNG\r\n", 6)));
    QVERIFY(!(inlineImage.permissions() & (QFileDevice::ReadGroup | QFileDevice::ReadOther)));
    const QString renderedMedia = controller.resolveMediaMarkdown(
        QStringLiteral("![Rendered result](clarp-media://asset/asset-1)"));
    QVERIFY(renderedMedia.startsWith(QStringLiteral("![Rendered result](file:")));
    QVERIFY(renderedMedia.size() < 512);
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
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedRequest(QStringLiteral("DELETE"), QStringLiteral("/agents/mike")), 3'000);
    QVERIFY(!controller.errorMessage().contains(QStringLiteral("protected")));
    // Persona names must not grant an implicit lifecycle exemption either.
    controller.agents()->applySnapshot({{QStringLiteral("agents"), QJsonArray{QJsonObject{
        {QStringLiteral("session"), QStringLiteral("mike-fixture")},
        {QStringLiteral("persona"), QStringLiteral("Mike")}}}}});
    controller.releaseAgent(QStringLiteral("mike-fixture"));
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedRequest(QStringLiteral("DELETE"), QStringLiteral("/agents/mike-fixture")), 3'000);
    QVERIFY(!controller.errorMessage().contains(QStringLiteral("protected")));
    controller.reconnect();
    QTRY_COMPARE_WITH_TIMEOUT(controller.selectedSession(), QStringLiteral("rachel"), 3'000);
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

void NativeCoreTest::sharedPlaybackDoesNotDuplicateDownloads() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/clip.wav"), 404,
                           {{QStringLiteral("error"), QStringLiteral("offline fixture")}});
    AudioController first;
    AudioController second;
    const QString token = QUuid::createUuid().toString();
    first.setEndpoint(QUrl(server.baseUrl()), token);
    second.setEndpoint(QUrl(server.baseUrl()), token);
    RecordingSession microphone;
    QVERIFY(microphone.acquire(QStringLiteral("recording-window-chat")));
    const QJsonObject event{{QStringLiteral("clip_id"), 1}, {QStringLiteral("url"), QStringLiteral("/clip.wav")}};
    first.enqueueClip(event);
    second.enqueueClip(event);
    QTRY_COMPARE_WITH_TIMEOUT(server.requestCount(QStringLiteral("POST"), QStringLiteral("/clips/ack")), 1, 3'000);
    QTest::qWait(600);
    QCOMPARE(server.requestCount(QStringLiteral("GET"), QStringLiteral("/clip.wav")), 0);
    QCOMPARE(microphone.release(), QStringLiteral("recording-window-chat"));
    QTRY_COMPARE_WITH_TIMEOUT(server.requestCount(QStringLiteral("GET"), QStringLiteral("/clip.wav")), 1, 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(server.requestCount(QStringLiteral("POST"), QStringLiteral("/clips/ack")), 2, 3'000);
    first.enqueueClip(event);
    second.enqueueClip(event);
    QTest::qWait(600);
    QCOMPARE(server.requestCount(QStringLiteral("GET"), QStringLiteral("/clip.wav")), 1);
    QCOMPARE(server.requestCount(QStringLiteral("POST"), QStringLiteral("/clips/ack")), 2);
    QSignalSpy firstMuted(&first, &AudioController::mutedChanged);
    QSignalSpy secondMuted(&second, &AudioController::mutedChanged);
    second.setMuted(true);
    QTRY_COMPARE_WITH_TIMEOUT(firstMuted.count(), 1, 3'000);
    QTRY_COMPARE_WITH_TIMEOUT(secondMuted.count(), 1, 3'000);
    QCOMPARE(firstMuted.first().first().toBool(), true);
    QCOMPARE(secondMuted.first().first().toBool(), true);
}

void NativeCoreTest::backgroundTranscriptionsKeepTheirChatOwnership() {
    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    AudioController audio;
    audio.setEndpoint(QUrl(server.baseUrl()), QStringLiteral("test-token"));
    QSignalSpy ready(&audio, &AudioController::transcriptionReady);
    AudioController otherWindow;
    otherWindow.setEndpoint(QUrl(server.baseUrl()), QStringLiteral("test-token"));
    QSignalSpy otherReady(&otherWindow, &AudioController::transcriptionReady);

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
    QCOMPARE(otherReady.count(), 0); // A different window never receives these results.
    audio.transcribeRecording(QByteArray(2'000, 'd'), QStringLiteral("original-host-chat"));
    audio.setEndpoint(QUrl(server.baseUrl()), QStringLiteral("different-host-credential"));
    QTRY_COMPARE_WITH_TIMEOUT(audio.transcriptionsInFlight(), 0, 3'000);
    QTest::qWait(50);
    QCOMPARE(ready.count(), 2); // Switching Host/account cancels the pending delivery.
}

void NativeCoreTest::agentReplyKeepsItsAuthorAndNamesTheAnsweredAgent() {
    ConversationModel model;
    model.openSession(QStringLiteral("hugo"));
    // Wire shape from the Host: the incoming prompt names its author; the
    // answering row names the agent it answers instead of claiming a sender.
    const QJsonObject prompt{
        {QStringLiteral("id"), QStringLiteral("u-1")},
        {QStringLiteral("role"), QStringLiteral("user")},
        {QStringLiteral("text"), QStringLiteral("Status: survey done")},
        {QStringLiteral("revision"), 1},
        {QStringLiteral("origin"), QStringLiteral("agent")},
        {QStringLiteral("sender_agent_id"), QStringLiteral("agent-cpp")},
        {QStringLiteral("sender_name"), QStringLiteral("C++ Junior")},
        {QStringLiteral("sender_session"), QStringLiteral("cjunior-0940")},
        {QStringLiteral("reply_to_agent_id"), QString{}},
        {QStringLiteral("delivery"), QStringLiteral("sent")},
    };
    const QJsonObject reply{
        {QStringLiteral("id"), QStringLiteral("m-1")},
        {QStringLiteral("role"), QStringLiteral("assistant")},
        {QStringLiteral("text"), QStringLiteral("Good, that matches the agreed scope.")},
        {QStringLiteral("revision"), 2},
        {QStringLiteral("origin"), QStringLiteral("agent")},
        {QStringLiteral("sender_agent_id"), QString{}},
        {QStringLiteral("sender_name"), QString{}},
        {QStringLiteral("reply_to_agent_id"), QStringLiteral("agent-cpp")},
        {QStringLiteral("reply_to_name"), QStringLiteral("C++ Junior")},
        {QStringLiteral("reply_to_session"), QStringLiteral("cjunior-0940")},
        {QStringLiteral("delivery"), QStringLiteral("private")},
    };
    model.applyLog({{QStringLiteral("turns"), QJsonArray{prompt, reply}}},
                   ConversationModel::LoadKind::Tail);
    QCOMPARE(model.rowCount(), 2);
    const QModelIndex incoming = model.index(0, 0);
    QCOMPARE(incoming.data(ConversationModel::SenderNameRole).toString(), QStringLiteral("C++ Junior"));
    QCOMPARE(incoming.data(ConversationModel::ReplyToNameRole).toString(), QString{});
    QCOMPARE(incoming.data(ConversationModel::DeliveryRole).toString(), QStringLiteral("sent"));
    const QModelIndex answer = model.index(1, 0);
    QCOMPARE(answer.data(ConversationModel::SenderNameRole).toString(), QString{});
    QCOMPARE(answer.data(ConversationModel::SenderAgentIdRole).toString(), QString{});
    QCOMPARE(answer.data(ConversationModel::ReplyToAgentIdRole).toString(), QStringLiteral("agent-cpp"));
    QCOMPARE(answer.data(ConversationModel::ReplyToNameRole).toString(), QStringLiteral("C++ Junior"));
    QCOMPARE(answer.data(ConversationModel::ReplyToSessionRole).toString(), QStringLiteral("cjunior-0940"));
    QCOMPARE(answer.data(ConversationModel::DeliveryRole).toString(), QStringLiteral("private"));

    // A delta that only changes the marker still notifies the reply roles.
    QSignalSpy changes(&model, &QAbstractItemModel::dataChanged);
    QJsonObject renamed = reply;
    renamed.insert(QStringLiteral("reply_to_name"), QStringLiteral("C++ Renamed"));
    renamed.insert(QStringLiteral("revision"), 3);
    model.applyLog({{QStringLiteral("turns"), QJsonArray{renamed}}}, ConversationModel::LoadKind::Delta);
    QCOMPARE(model.index(1, 0).data(ConversationModel::ReplyToNameRole).toString(), QStringLiteral("C++ Renamed"));
    bool notified = false;
    for (const auto& signal : changes) {
        if (signal.at(2).value<QList<int>>().contains(ConversationModel::ReplyToNameRole)) notified = true;
    }
    QVERIFY(notified);

    ConversationModel restored;
    QVERIFY(restored.restoreCacheSnapshot(model.cacheSnapshot()));
    QCOMPARE(restored.index(1, 0).data(ConversationModel::ReplyToAgentIdRole).toString(), QStringLiteral("agent-cpp"));
    QCOMPARE(restored.index(1, 0).data(ConversationModel::DeliveryRole).toString(), QStringLiteral("private"));
}

void NativeCoreTest::pairConversationRoomsAreReadOnlyProjections() {
    QVERIFY(AppController::isPairSession(QStringLiteral("pair:a:b")));
    QVERIFY(!AppController::isPairSession(QStringLiteral("hugo")));

    FakeClarpServer server;
    QVERIFY(server.listenLocal());
    const QString room = QStringLiteral("pair:agent-cpp:agent-rachel");
    const auto participant = [](const QString& id, const QString& session, const QString& name) {
        QJsonObject value;
        value.insert(QStringLiteral("agent_id"), id);
        value.insert(QStringLiteral("session"), session);
        value.insert(QStringLiteral("name"), name);
        return value;
    };
    QJsonObject preview;
    preview.insert(QStringLiteral("text"), QStringLiteral("Good, that matches."));
    preview.insert(QStringLiteral("sender_name"), QStringLiteral("Rachel"));
    preview.insert(QStringLiteral("delivery"), QStringLiteral("private"));
    QJsonObject listedRoom;
    listedRoom.insert(QStringLiteral("conversation_id"), room);
    listedRoom.insert(QStringLiteral("agent_ids"),
                      QJsonArray{QStringLiteral("agent-cpp"), QStringLiteral("agent-rachel")});
    listedRoom.insert(QStringLiteral("title"), QStringLiteral("C++ Junior & Rachel"));
    listedRoom.insert(QStringLiteral("latest_revision"), 7);
    listedRoom.insert(QStringLiteral("latest_activity"), Q_INT64_C(1788750466681));
    listedRoom.insert(QStringLiteral("participants"),
        QJsonArray{participant(QStringLiteral("agent-cpp"), QStringLiteral("cjunior-0940"),
                               QStringLiteral("C++ Junior")),
                   participant(QStringLiteral("agent-rachel"), QStringLiteral("rachel"),
                               QStringLiteral("Rachel"))});
    listedRoom.insert(QStringLiteral("latest_message"), preview);
    QJsonObject roomsResponse;
    roomsResponse.insert(QStringLiteral("conversations"), QJsonArray{listedRoom});
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agent-conversations"), 200,
                           roomsResponse);
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8());
    qputenv("CLARP_TOKEN", "test-token");
    // A device that has never opened this room must see it as unread.
    QSettings settings;
    for (const QString& key : settings.allKeys()) {
        if (key.startsWith(QStringLiteral("agentConversations/"))) settings.remove(key);
    }
    settings.sync();

    AppController controller;
    QTRY_COMPARE_WITH_TIMEOUT(controller.agentConversations().size(), 1, 3'000);
    const QVariantMap listed = controller.agentConversations().first().toMap();
    QCOMPARE(listed.value(QStringLiteral("conversation_id")).toString(), room);
    QCOMPARE(listed.value(QStringLiteral("session")).toString(), room);
    QVERIFY(listed.value(QStringLiteral("unread")).toBool());
    QCOMPARE(controller.unreadAgentConversations(), 1);
    QCOMPARE(controller.agentConversation(room).value(QStringLiteral("title")).toString(),
             QStringLiteral("C++ Junior & Rachel"));
    QCOMPARE(controller.agentName(room), QStringLiteral("C++ Junior & Rachel"));
    QVERIFY(controller.agentConversation(QStringLiteral("pair:nope:nada")).isEmpty());

    // Identical Host snapshots must not invalidate the QML room list.
    QSignalSpy roomsChanged(&controller, &AppController::agentConversationsChanged);
    for (int refresh = 0; refresh < 5; ++refresh) {
        const auto before = server.requestCount(QStringLiteral("GET"), QStringLiteral("/agent-conversations"));
        controller.loadAgentConversations();
        QTRY_VERIFY_WITH_TIMEOUT(server.requestCount(QStringLiteral("GET"), QStringLiteral("/agent-conversations")) > before, 3'000);
        QTest::qWait(100);
    }
    QCOMPARE(roomsChanged.count(), 0);
    listedRoom.insert(QStringLiteral("title"), QStringLiteral("Renamed pair"));
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agent-conversations"), 200,
        QJsonObject{{QStringLiteral("conversations"), QJsonArray{listedRoom}}});
    controller.loadAgentConversations();
    QTRY_COMPARE_WITH_TIMEOUT(roomsChanged.count(), 1, 3'000);
    QCOMPARE(controller.agentName(room), QStringLiteral("Renamed pair"));
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agent-conversations"), 200, roomsResponse);
    controller.loadAgentConversations();
    QTRY_COMPARE_WITH_TIMEOUT(roomsChanged.count(), 2, 3'000);

    // Opening a room reads its timeline but never claims Host focus for it,
    // and never resolves it to an agent record.
    const qsizetype selectsBefore = server.requestCount(QStringLiteral("POST"), QStringLiteral("/select"));
    // Startup selects a real agent, which legitimately loads its clips. Only
    // additional traffic caused by opening the pair room matters here.
    const qsizetype clipsBefore = server.requestCount(QStringLiteral("GET"), QStringLiteral("/clips/recoverable"));
    controller.selectSession(room);
    QCOMPARE(controller.selectedSession(), room);
    QCOMPARE(controller.selectedName(), QStringLiteral("C++ Junior & Rachel"));
    QCOMPARE(controller.agentBackend(room), QString{});
    QTRY_VERIFY_WITH_TIMEOUT(server.receivedRequest(QStringLiteral("GET"), QStringLiteral("/log")), 3'000);
    QTest::qWait(60);
    QCOMPARE(server.requestCount(QStringLiteral("POST"), QStringLiteral("/select")), selectsBefore);
    QCOMPARE(server.requestCount(QStringLiteral("GET"), QStringLiteral("/clips/recoverable")), clipsBefore);
    QTRY_VERIFY_WITH_TIMEOUT(!controller.agentConversations().isEmpty()
        && !controller.agentConversations().first().toMap().value(QStringLiteral("unread")).toBool(), 3'000);
    QCOMPARE(controller.unreadAgentConversations(), 0);

    // An older Host without the route must not raise a chat error banner.
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agent-conversations"), 404,
                           QJsonObject{{QStringLiteral("error"), QStringLiteral("not found")}});
    controller.clearError();
    controller.loadAgentConversations();
    QTRY_VERIFY_WITH_TIMEOUT(controller.agentConversations().isEmpty(), 3'000);
    QCOMPARE(controller.errorMessage(), QString{});
    QCOMPARE(controller.unreadAgentConversations(), 0);
    server.setJsonResponse(QStringLiteral("GET"), QStringLiteral("/agent-conversations"), 200,
                           roomsResponse);
    controller.loadAgentConversations();
    QTRY_COMPARE_WITH_TIMEOUT(controller.agentConversations().size(), 1, 3'000);

    // Another agent's turn refreshes open rooms without a second selection.
    server.sendEvent({{QStringLiteral("type"), QStringLiteral("transcript-updated")},
                      {QStringLiteral("session"), QStringLiteral("rachel")}});
    QTRY_VERIFY_WITH_TIMEOUT(server.requestCount(QStringLiteral("GET"), QStringLiteral("/agent-conversations")) >= 2, 3'000);
    QCOMPARE(controller.selectedSession(), room);
}


void NativeCoreTest::localReportsRequireOriginAndSafeReadableFiles() {
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    ReportUrlCapture capture;
    QDesktopServices::setUrlHandler("file", &capture, "capture");
    const auto cleanup = qScopeGuard([] { QDesktopServices::unsetUrlHandler("file"); });
    const auto write = [&](const QString& name, const QByteArray& body) {
        QString path = dir.filePath(name);
        QFile file(path);
        if (!file.open(QIODevice::WriteOnly) || file.write(body) != body.size()) return QString{};
        file.close();
        file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
        return path;
    };
    const QString html = write("report with spaces.html", "<html><body>Report</body></html>");
    QVERIFY(!html.isEmpty());
    AppController controller;
    const QString origin = controller.baseUrl();
    controller.setSharedFilesystem(false);
    QVERIFY(!controller.openLocalReport(html, origin));
    controller.setSharedFilesystem(true);
    QVERIFY(!controller.openLocalReport(html, "http://different.invalid"));
    QVERIFY(!controller.openLocalReport(html, {}));
    QVERIFY(!controller.openLocalReport(dir.filePath("missing.txt"), origin));
    QVERIFY(!controller.openLocalReport(dir.path(), origin));
    QVERIFY(!controller.openLocalReport(write("launcher.desktop", "[Desktop Entry]\nExec=bad"), origin));
    QVERIFY(!controller.openLocalReport(write("hidden.txt", "[Desktop Entry]\nExec=bad"), origin));
    QVERIFY(!controller.openLocalReport(write("program.txt", "#!/bin/sh\necho bad"), origin));
    QVERIFY(!controller.openLocalReport(write("unknown.xyz", "plain text"), origin));
    const QString executable = write("executable.html", "<html>Report</html>");
    QVERIFY(QFile::setPermissions(executable, QFileDevice::ReadOwner | QFileDevice::ExeOwner));
    QVERIFY(!controller.openLocalReport(executable, origin));
    const QString unreadable = write("unreadable.txt", "text");
    QVERIFY(QFile::setPermissions(unreadable, QFileDevice::WriteOwner));
    QVERIFY(!controller.openLocalReport(unreadable, origin));
    QVERIFY(!controller.openLocalReport("file://remote/report.html", origin));
    QVERIFY(!controller.openLocalReport("javascript:alert(1)", origin));
    QCOMPARE(capture.opened.size(), 0);
    QVERIFY(controller.openExternalLink(html, origin));
    QCOMPARE(capture.opened.last(), QUrl::fromLocalFile(QFileInfo(html).canonicalFilePath()));
    const QString alias = dir.filePath("alias.html");
    QVERIFY(QFile::link(html, alias));
    QVERIFY(controller.openLocalReport(QUrl::fromLocalFile(alias).toString(), origin));
    QCOMPARE(capture.opened.last(), QUrl::fromLocalFile(QFileInfo(html).canonicalFilePath()));
    for (const auto& pair : QList<QPair<QString,QByteArray>>{{"report.pdf", "%PDF-1.4\n"}, {"report.txt", "plain text"}, {"report.svg", "<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>"}}) {
        QVERIFY(controller.openLocalReport(write(pair.first, pair.second), origin));
    }
    QVERIFY(!isOpenableLink(html));
    QVERIFY(!isOpenableLink(QUrl::fromLocalFile(html).toString()));
    QVERIFY(isOpenableLink("https://example.com/report"));
    controller.setSharedFilesystem(false);
}

void NativeCoreTest::leadingDayTracksVisibleHistory() {
  QStandardItemModel rows; ConversationPresentationModel view;view.setSourceModel(&rows);
  QSignalSpy changed(&view,&ConversationPresentationModel::leadingDayLabelChanged);
  auto add=[&](const QString& day,bool prepend=false){auto *r=new QStandardItem;r->setData(day,ConversationModel::DayLabelRole);r->setData("user",ConversationModel::AuthorRole);if(prepend)rows.insertRow(0,r);else rows.appendRow(r);};
  QCOMPARE(view.leadingDayLabel(),QString{}); // empty: no heading
  add("Today");QCOMPARE(view.leadingDayLabel(),QString("Today"));QVERIFY(changed.count()>0);
  add("Today");QCOMPARE(view.leadingDayLabel(),QString("Today")); // one current day: suppress initial heading
  changed.clear();add("Yesterday",true);QCOMPARE(view.leadingDayLabel(),QString("Yesterday"));QVERIFY(changed.count()>0); // prepend exposes useful Today transition
  QCOMPARE(view.index(1,0).data(ConversationModel::DayLabelRole).toString(),QString("Today")); // roles unchanged
  rows.removeRow(0);QCOMPARE(view.leadingDayLabel(),QString("Today"));
  changed.clear();rows.item(0)->setData("Yesterday",ConversationModel::DayLabelRole);QCOMPARE(view.leadingDayLabel(),QString("Yesterday"));QVERIFY(changed.count()>0);
  rows.clear();QCOMPARE(view.leadingDayLabel(),QString{});
  add("");add("Today");QCOMPARE(view.leadingDayLabel(),QString("Today")); // pending row without timestamp
  rows.clear();add("12 June");add("Yesterday");add("Today");QCOMPARE(view.leadingDayLabel(),QString("12 June")); // loaded multi-day history
  rows.clear();add("Yesterday");QCOMPARE(view.leadingDayLabel(),QString("Yesterday")); // preserve old-day context
}


void NativeCoreTest::clipboardImageBecomesAttachmentWithoutSending() {
    QVERIFY(QGuiApplication::platformName() == QStringLiteral("offscreen"));
    auto* clipboard = QGuiApplication::clipboard();
    const auto cleanup = qScopeGuard([clipboard] { clipboard->clear(); });
    AppController controller;
    const QString session = QStringLiteral("paste-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    controller.restoreDesktopSession(session);
    controller.setSharedFilesystem(true);
    const QString pane = controller.panes()->activePaneId();
    controller.setPaneDraft(pane, session, QStringLiteral("Keep this draft"));
    clipboard->setText(QStringLiteral("plain paste"));
    QVERIFY(!controller.pasteClipboardImage(pane, session));
    QImage image(3, 2, QImage::Format_ARGB32);
    image.fill(QColor(Qt::red));
    clipboard->setImage(image);
    QVERIFY(controller.pasteClipboardImage(pane, session));
    const auto attachments = controller.composerAttachments(pane, session);
    QCOMPARE(attachments.size(), 1);
    const auto attachment = attachments.first().toMap();
    const QString path = attachment.value(QStringLiteral("path")).toString();
    QCOMPARE(QImage(path), image);
    QCOMPARE(attachment.value(QStringLiteral("status")).toString(), QStringLiteral("ready"));
    QCOMPARE(controller.paneDraft(pane, session), QStringLiteral("Keep this draft"));
    QVERIFY(!controller.sending());
    QVERIFY(controller.pasteClipboardImage(QStringLiteral("wrong-pane"), session));
    QCOMPARE(controller.composerAttachments(pane, session).size(), 1);
    controller.removeComposerAttachment(pane, session, attachment.value(QStringLiteral("id")).toString());
    QVERIFY(QFile::remove(path));
    controller.setPaneDraft(pane, session, {});
    controller.setSharedFilesystem(false);
}

void NativeCoreTest::relaunchPreservesHostSessionAndDraft() {
    QProcessEnvironment env;
    env.insert(QStringLiteral("UNRELATED"), QStringLiteral("retained"));
    const auto next = previewRelaunchEnvironment(env, QStringLiteral("http://host.example:7682"), QStringLiteral("agent-exact"));
    QCOMPARE(next.value(QStringLiteral("CLARP_RESTORE_SESSION")), QStringLiteral("agent-exact"));
    QCOMPARE(next.value(QStringLiteral("CLARP_BASE_URL")), QStringLiteral("http://host.example:7682"));
    QCOMPARE(next.value(QStringLiteral("CLARP_RESTORE_DESKTOP")), QStringLiteral("1"));
    QCOMPARE(next.value(QStringLiteral("UNRELATED")), QStringLiteral("retained"));
    QCOMPARE(previewRelaunchArguments(QStringLiteral("/helper.py"), 123),
             QStringList({QStringLiteral("/helper.py"), QStringLiteral("--restart-after"), QStringLiteral("123")}));
    AppController first;
    first.restoreDesktopSession(QStringLiteral("restore-exact"));
    const auto pane = first.panes()->activePaneId();
    first.setPaneDraft(pane, QStringLiteral("restore-exact"), QStringLiteral("unsent draft"));
    QSettings().sync();
    AppController nextController;
    nextController.restoreDesktopSession(QStringLiteral("restore-exact"));
    QCOMPARE(nextController.selectedSession(), QStringLiteral("restore-exact"));
    QCOMPARE(nextController.panes()->activeSession(), QStringLiteral("restore-exact"));
    QCOMPARE(nextController.paneDraft(nextController.panes()->activePaneId(), QStringLiteral("restore-exact")), QStringLiteral("unsent draft"));
    first.setPaneDraft(pane, QStringLiteral("restore-exact"), {});
}


void NativeCoreTest::previewRestartCapturesContextAndRejectsBusy() {
    const auto oldPath = qgetenv("CLARP_SCREENSHOT_PATH");
    const auto oldScenario = qgetenv("CLARP_SCREENSHOT_SCENARIO");
    const auto restore = qScopeGuard([&] {
        oldPath.isNull() ? qunsetenv("CLARP_SCREENSHOT_PATH") : qputenv("CLARP_SCREENSHOT_PATH", oldPath);
        oldScenario.isNull() ? qunsetenv("CLARP_SCREENSHOT_SCENARIO") : qputenv("CLARP_SCREENSHOT_SCENARIO", oldScenario);
    });
    qputenv("CLARP_SCREENSHOT_PATH", "/dev/null");
    qputenv("CLARP_SCREENSHOT_SCENARIO", "preview-versions");
    PreviewVersions versions;
    versions.setProperty("selectedHost", "http://origin.example");
    versions.setProperty("selectedSession", "exact-selected-session");
    versions.setProperty("restartAllowed", false);
    versions.selectVersion("new");
    QVERIFY(versions.restartContext().value("session").toString().isEmpty());
    QVERIFY(!versions.error().isEmpty());
    versions.setProperty("restartAllowed", true);
    versions.selectVersion("new");
    QCOMPARE(versions.restartContext().value("session").toString(), QString("exact-selected-session"));
    QCOMPARE(versions.restartContext().value("host").toString(), QString("http://origin.example"));
    QVERIFY(versions.restartContext().value("arguments").toStringList().contains("--restart-after"));
}
void NativeCoreTest::restoredSessionDoesNotFallBackToAnotherAgent() {
    FakeClarpServer server;
    QVERIFY(server.listen(QHostAddress::LocalHost, 0));
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    const auto restore = qScopeGuard([&] {
        oldBase.isNull() ? qunsetenv("CLARP_BASE_URL") : qputenv("CLARP_BASE_URL", oldBase);
        oldToken.isNull() ? qunsetenv("CLARP_TOKEN") : qputenv("CLARP_TOKEN", oldToken);
    });
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8()); qputenv("CLARP_TOKEN", "fixture-token");
    AppController controller;
    controller.restoreDesktopSession("missing-restored-target");
    QTRY_VERIFY_WITH_TIMEOUT(controller.agents()->rowCount() > 0, 3000);
    QCOMPARE(controller.selectedSession(), QString("missing-restored-target"));
    QVERIFY(controller.errorMessage().contains("before updating"));
    controller.selectSession("rachel");
    QCOMPARE(controller.selectedSession(), QString("rachel"));
}


void NativeCoreTest::spawnedLifecycleNeverBecomesTranscriptTool() {
    ConversationModel model;
    // Reconstructed from the retained event preceding the incident capture.
    model.applyActivityEvent({{"type", "agent-activity"}, {"kind", "spawned"},
        {"phase", "spawned"}, {"status", "ok"}, {"action", "started"}, {"summary", "Started"}});
    QCOMPARE(model.rowCount(), 0);
    model.applyActivityEvent({{"activity_kind", "spawned"}, {"activity_phase", "spawned"},
        {"activity_status", "ok"}, {"activity_action", "started"}, {"activity_summary", "Started"}});
    QCOMPARE(model.rowCount(), 0);
    model.addOptimistic("real-user", "Start the task session.");
    QCOMPARE(model.rowCount(), 1); // The literal remains valid user content.
    model.applyActivityEvent({{"kind", "tool"}, {"phase", "tool"}, {"status", "running"},
        {"tool", "Bash"}, {"action", "started"}, {"summary", "Run tests"}});
    QCOMPARE(model.rowCount(), 2); // Real tool activity is not suppressed.
}

QTEST_MAIN(NativeCoreTest)

#include "tst_native_core.moc"

void NativeCoreTest::voiceErrorsStayInTheirSession() {
    FakeClarpServer server; QVERIFY(server.listenLocal());
    const auto oldBase = qgetenv("CLARP_BASE_URL");
    const auto oldToken = qgetenv("CLARP_TOKEN");
    const auto restore = qScopeGuard([&] { qputenv("CLARP_BASE_URL", oldBase); qputenv("CLARP_TOKEN", oldToken); });
    qputenv("CLARP_BASE_URL", server.baseUrl().toUtf8()); qputenv("CLARP_TOKEN", "test-token");
    AppController controller;
    QTRY_VERIFY(controller.connected());
    auto* current = controller.conversationForSession("current");
    auto* other = controller.conversationForSession("other");
    server.sendEvent({{"type", "tts-error"}, {"session", "other"}, {"message", "Other voice failed"}});
    QTRY_COMPARE(other->voiceError(), QStringLiteral("Other voice failed"));
    QVERIFY(current->voiceError().isEmpty()); QVERIFY(controller.errorMessage().isEmpty());
    server.sendEvent({{"type", "tts-error"}, {"session", "current"}, {"message", "Current voice failed"}});
    QTRY_COMPARE(current->voiceError(), QStringLiteral("Current voice failed"));
    QVERIFY(controller.errorMessage().isEmpty()); QVERIFY(current->error().isEmpty());
    current->setVoiceError({});
    QCOMPARE(other->voiceError(), QStringLiteral("Other voice failed"));
    server.sendEvent({{"type", "audio"}, {"session", "other"}});
    QTRY_VERIFY(other->voiceError().isEmpty());
    server.sendEvent({{"type", "tts-error"}, {"message", "Unscoped voice failed"}});
    QTRY_COMPARE(controller.errorMessage(), QStringLiteral("Unscoped voice failed"));
}
