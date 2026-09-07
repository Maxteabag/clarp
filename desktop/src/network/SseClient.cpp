#include "network/SseClient.h"

#include <QNetworkReply>
#include <QNetworkRequest>
#include <algorithm>

namespace clarp {
namespace {

constexpr int WatchdogIntervalMs = 25'000;
constexpr int MaximumReconnectDelayMs = 5'000;
constexpr int InitialReconnectDelayMs = 250;

} // namespace

SseClient::SseClient(QObject* parent) : QObject(parent) {
    m_reconnectTimer.setSingleShot(true);
    connect(&m_reconnectTimer, &QTimer::timeout, this, &SseClient::open);

    m_watchdog.setSingleShot(true);
    m_watchdog.setInterval(WatchdogIntervalMs);
    connect(&m_watchdog, &QTimer::timeout, this, [this] {
        if (m_reply != nullptr) {
            m_reply->abort();
        }
    });
}

SseClient::~SseClient() { stop(); }

void SseClient::setEndpoint(QUrl baseUrl, QString bearerToken) {
    QString path = baseUrl.path();
    if (!path.endsWith('/')) {
        path.append('/');
    }
    baseUrl.setPath(path);
    baseUrl.setQuery(QString{});
    baseUrl.setFragment(QString{});
    if (!m_baseUrl.isEmpty() && m_baseUrl != baseUrl) {
        // Event IDs are scoped to one Host. Reusing another Host's cursor can
        // make the new stream skip its initial state or reject the request.
        m_lastEventId.clear();
    }
    m_baseUrl = std::move(baseUrl);
    m_bearerToken = std::move(bearerToken);
}

void SseClient::start() {
    m_running = true;
    m_reconnectAttempt = 0;
    open();
}

void SseClient::stop() {
    m_running = false;
    m_reconnectTimer.stop();
    m_watchdog.stop();
    if (m_reply != nullptr) {
        disconnect(m_reply, nullptr, this, nullptr);
        m_reply->abort();
        m_reply->deleteLater();
        m_reply = nullptr;
    }
    m_parser.reset();
    setConnected(false);
}

bool SseClient::connected() const { return m_connected; }

QString SseClient::lastEventId() const { return m_lastEventId; }

void SseClient::setLastEventId(const QString& eventId) { m_lastEventId = eventId; }

void SseClient::open() {
    if (!m_running || !m_baseUrl.isValid() || m_reply != nullptr) {
        return;
    }

    QUrl eventsUrl = m_baseUrl.resolved(QUrl(QStringLiteral("events")));
    QNetworkRequest request(eventsUrl);
    request.setRawHeader("Accept", "text/event-stream");
    request.setRawHeader("Cache-Control", "no-cache");
    request.setRawHeader("User-Agent", "ClarpNativeDesktop/0.1");
    request.setTransferTimeout(0);
    if (!m_bearerToken.isEmpty()) {
        request.setRawHeader("Authorization", "Bearer " + m_bearerToken.toUtf8());
    }
    if (!m_lastEventId.isEmpty()) {
        request.setRawHeader("Last-Event-ID", m_lastEventId.toUtf8());
    }

    m_parser.reset();
    m_reply = m_network.get(request);
    connect(m_reply, &QNetworkReply::readyRead, this, &SseClient::onReadyRead);
    connect(m_reply, &QNetworkReply::finished, this, &SseClient::onFinished);
    connect(m_reply, &QNetworkReply::metaDataChanged, this, [this] {
        if (m_reply == nullptr) {
            return;
        }
        const int status = m_reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (status >= 200 && status < 300) {
            setConnected(true);
            m_reconnectAttempt = 0;
            m_watchdog.start();
        }
    });
}

void SseClient::scheduleReconnect() {
    if (!m_running || m_reconnectTimer.isActive()) {
        return;
    }
    const int shift = std::min(m_reconnectAttempt, 5);
    const int delay = std::min(InitialReconnectDelayMs * (1 << shift), MaximumReconnectDelayMs);
    ++m_reconnectAttempt;
    m_reconnectTimer.start(delay);
}

void SseClient::setConnected(bool connected) {
    if (m_connected == connected) {
        return;
    }
    m_connected = connected;
    emit connectedChanged();
}

void SseClient::onReadyRead() {
    if (m_reply == nullptr) {
        return;
    }
    m_watchdog.start();
    const QList<SseMessage> messages = m_parser.feed(m_reply->readAll());
    for (const SseMessage& message : messages) {
        if (!message.id.isEmpty()) {
            m_lastEventId = message.id;
        }
        QJsonObject event = message.data;
        if (!message.id.isEmpty()) {
            bool validId = false;
            const qint64 eventId = message.id.toLongLong(&validId);
            event.insert(QStringLiteral("event_id"),
                         validId ? QJsonValue(eventId) : QJsonValue(message.id));
        }
        emit eventReceived(event);
    }
}

void SseClient::onFinished() {
    if (m_reply == nullptr) {
        return;
    }
    QNetworkReply* finishedReply = m_reply;
    m_reply = nullptr;
    m_watchdog.stop();
    setConnected(false);

    if (m_running && finishedReply->error() != QNetworkReply::OperationCanceledError) {
        emit connectionError(finishedReply->errorString());
    }
    finishedReply->deleteLater();
    scheduleReconnect();
}

} // namespace clarp
