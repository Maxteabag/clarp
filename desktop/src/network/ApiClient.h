#pragma once

#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QObject>
#include <QUrl>
#include <QUrlQuery>

class QNetworkReply;

namespace clarp {

class ApiClient final : public QObject {
    Q_OBJECT

  public:
    explicit ApiClient(QObject* parent = nullptr);

    void setEndpoint(QUrl baseUrl, QString bearerToken);
    [[nodiscard]] QUrl resolve(const QString& path) const;

    void get(const QString& tag, const QString& path, const QUrlQuery& query = {});
    void postJson(const QString& tag, const QString& path, const QJsonObject& body);
    void deleteResource(const QString& tag, const QString& path);
    void postBytes(const QString& tag, const QString& path, const QByteArray& body,
                   const QByteArray& contentType,
                   const QList<QPair<QByteArray, QByteArray>>& headers = {});

  signals:
    void jsonReceived(const QString& tag, const QJsonObject& object);
    void bytesReceived(const QString& tag, const QByteArray& bytes, const QByteArray& contentType);
    void requestFailed(const QString& tag, const QString& message, int statusCode);

  private:
    [[nodiscard]] QNetworkRequest requestFor(const QUrl& url) const;
    void watchJson(const QString& tag, QNetworkReply* reply);
    void watchBytes(const QString& tag, QNetworkReply* reply);

    QNetworkAccessManager m_network;
    QUrl m_baseUrl;
    QString m_bearerToken;
};

} // namespace clarp
