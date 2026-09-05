#include "network/ApiClient.h"

#include <QJsonDocument>
#include <QJsonParseError>
#include <QNetworkReply>
#include <QNetworkRequest>

namespace clarp {
namespace {

int effectivePort(const QUrl& url) {
    if (url.port() >= 0) {
        return url.port();
    }
    return url.scheme().compare(QStringLiteral("https"), Qt::CaseInsensitive) == 0 ? 443 : 80;
}

} // namespace

ApiClient::ApiClient(QObject* parent) : QObject(parent) {}

void ApiClient::setEndpoint(QUrl baseUrl, QString bearerToken) {
    ++m_endpointGeneration;
    QString path = baseUrl.path();
    if (!path.endsWith('/')) {
        path.append('/');
    }
    baseUrl.setPath(path);
    baseUrl.setQuery(QString{});
    baseUrl.setFragment(QString{});
    m_baseUrl = std::move(baseUrl);
    m_bearerToken = std::move(bearerToken);
}

QUrl ApiClient::resolve(const QString& path) const {
    QString relative = path;
    while (relative.startsWith('/')) {
        relative.removeFirst();
    }
    return m_baseUrl.resolved(QUrl(relative));
}

void ApiClient::get(const QString& tag, const QString& path, const QUrlQuery& query) {
    QUrl url = resolve(path);
    url.setQuery(query);
    watchJson(tag, m_network.get(requestFor(url)));
}

void ApiClient::getBytes(const QString& tag, const QString& path) {
    const QUrl url = resolve(path);
    const bool sameOrigin = url.isValid() && url.userInfo().isEmpty() &&
                            url.scheme().compare(m_baseUrl.scheme(), Qt::CaseInsensitive) == 0 &&
                            url.host().compare(m_baseUrl.host(), Qt::CaseInsensitive) == 0 &&
                            effectivePort(url) == effectivePort(m_baseUrl);
    if (!sameOrigin) {
        emit requestFailed(tag, QStringLiteral("Refusing cross-origin authenticated media"), 0);
        return;
    }
    QNetworkRequest request = requestFor(url);
    // Unlike ordinary API calls, this request carries a credential into a
    // renderer-owned media path. Never let Qt retain that header across an
    // otherwise "safe" HTTPS redirect to another origin.
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                         QNetworkRequest::SameOriginRedirectPolicy);
    request.setRawHeader("Accept", "image/png,image/jpeg,image/webp,image/gif,image/*");
    watchBytes(tag, m_network.get(request));
}

void ApiClient::postJson(const QString& tag, const QString& path, const QJsonObject& body) {
    QNetworkRequest request = requestFor(resolve(path));
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    watchJson(tag, m_network.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact)));
}

void ApiClient::putJson(const QString& tag, const QString& path, const QJsonObject& body) {
    QNetworkRequest request = requestFor(resolve(path));
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    watchJson(tag, m_network.put(request, QJsonDocument(body).toJson(QJsonDocument::Compact)));
}

void ApiClient::deleteResource(const QString& tag, const QString& path) {
    watchJson(tag, m_network.deleteResource(requestFor(resolve(path))));
}

void ApiClient::postBytes(const QString& tag, const QString& path, const QByteArray& body,
                          const QByteArray& contentType,
                          const QList<QPair<QByteArray, QByteArray>>& headers) {
    QNetworkRequest request = requestFor(resolve(path));
    request.setRawHeader("Content-Type", contentType);
    for (const auto& [name, value] : headers) {
        request.setRawHeader(name, value);
    }
    watchJson(tag, m_network.post(request, body));
}

QNetworkRequest ApiClient::requestFor(const QUrl& url) const {
    QNetworkRequest request(url);
    request.setRawHeader("Accept", "application/json");
    request.setRawHeader("User-Agent", "ClarpNativeDesktop/0.1");
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                         QNetworkRequest::NoLessSafeRedirectPolicy);
    if (!m_bearerToken.isEmpty()) {
        request.setRawHeader("Authorization", "Bearer " + m_bearerToken.toUtf8());
    }
    return request;
}

void ApiClient::watchJson(const QString& tag, QNetworkReply* reply) {
    const quint64 generation = m_endpointGeneration;
    connect(reply, &QNetworkReply::finished, this, [this, tag, reply, generation] {
        if (generation != m_endpointGeneration) {
            reply->deleteLater();
            return;
        }
        const QByteArray body = reply->readAll();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (reply->error() != QNetworkReply::NoError || status < 200 || status >= 300) {
            QString message = reply->errorString();
            QJsonParseError parseError;
            const QJsonDocument errorDocument = QJsonDocument::fromJson(body, &parseError);
            if (parseError.error == QJsonParseError::NoError && errorDocument.isObject()) {
                const QString serverMessage =
                    errorDocument.object().value(QStringLiteral("error")).toString();
                if (!serverMessage.isEmpty()) {
                    message = serverMessage;
                }
            }
            emit requestFailed(tag, message, status);
            reply->deleteLater();
            return;
        }

        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(body, &parseError);
        if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
            emit requestFailed(
                tag, QStringLiteral("Invalid JSON response: %1").arg(parseError.errorString()),
                status);
        } else {
            emit jsonReceived(tag, document.object());
        }
        reply->deleteLater();
    });
}

void ApiClient::watchBytes(const QString& tag, QNetworkReply* reply) {
    const quint64 generation = m_endpointGeneration;
    connect(reply, &QNetworkReply::finished, this, [this, tag, reply, generation] {
        if (generation != m_endpointGeneration) {
            reply->deleteLater();
            return;
        }
        const QByteArray body = reply->readAll();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (reply->error() != QNetworkReply::NoError || status < 200 || status >= 300) {
            emit requestFailed(tag, reply->errorString(), status);
        } else {
            emit bytesReceived(tag, body, reply->rawHeader("Content-Type"));
        }
        reply->deleteLater();
    });
}

} // namespace clarp
