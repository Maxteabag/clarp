#pragma once

#include "network/SseParser.h"

#include <QNetworkAccessManager>
#include <QObject>
#include <QTimer>
#include <QUrl>

class QNetworkReply;

namespace clarp {

class SseClient final : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool connected READ connected NOTIFY connectedChanged)

  public:
    explicit SseClient(QObject* parent = nullptr);
    ~SseClient() override;

    void setEndpoint(QUrl baseUrl, QString bearerToken);
    void start();
    void stop();

    [[nodiscard]] bool connected() const;
    [[nodiscard]] QString lastEventId() const;
    void setLastEventId(const QString& eventId);

  signals:
    void eventReceived(const QJsonObject& event);
    void connectedChanged();
    void connectionError(const QString& message);

  private:
    void open();
    void scheduleReconnect();
    void setConnected(bool connected);
    void onReadyRead();
    void onFinished();

    QNetworkAccessManager m_network;
    QNetworkReply* m_reply = nullptr;
    QTimer m_reconnectTimer;
    QTimer m_watchdog;
    SseParser m_parser;
    QUrl m_baseUrl;
    QString m_bearerToken;
    QString m_lastEventId;
    int m_reconnectAttempt = 0;
    bool m_connected = false;
    bool m_running = false;
};

} // namespace clarp
