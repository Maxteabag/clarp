#include "app/TranscriptCache.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QSaveFile>
#include <QStandardPaths>
#include <utility>

namespace clarp {
namespace {

constexpr qint64 MaxCacheBytes = qint64{8} * 1024 * 1024;

} // namespace

TranscriptCache::TranscriptCache(QString rootDirectory)
    : m_rootDirectory(std::move(rootDirectory)) {
    if (m_rootDirectory.isEmpty()) {
        m_rootDirectory = QDir(QStandardPaths::writableLocation(QStandardPaths::CacheLocation))
                              .filePath(QStringLiteral("transcripts"));
    }
}

QString TranscriptCache::pathFor(const QString& baseUrl, const QString& session) const {
    const QByteArray identity = (baseUrl + QChar::Null + session).toUtf8();
    const QString digest = QString::fromLatin1(
        QCryptographicHash::hash(identity, QCryptographicHash::Sha256).toHex());
    return QDir(m_rootDirectory).filePath(digest + QStringLiteral(".json"));
}

QJsonObject TranscriptCache::load(const QString& baseUrl, const QString& session) const {
    if (baseUrl.isEmpty() || session.isEmpty()) {
        return {};
    }
    QFile file(pathFor(baseUrl, session));
    if (!file.open(QIODevice::ReadOnly) || file.size() <= 0 || file.size() > MaxCacheBytes) {
        return {};
    }
    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(file.readAll(), &error);
    if (error.error != QJsonParseError::NoError || !document.isObject()) {
        return {};
    }
    const QJsonObject envelope = document.object();
    if (envelope.value(QStringLiteral("schema")).toInt() != 1 ||
        envelope.value(QStringLiteral("base_url")).toString() != baseUrl ||
        envelope.value(QStringLiteral("session")).toString() != session) {
        return {};
    }
    return envelope.value(QStringLiteral("snapshot")).toObject();
}

bool TranscriptCache::save(const QString& baseUrl, const QString& session,
                           const QJsonObject& snapshot) const {
    if (baseUrl.isEmpty() || session.isEmpty() || snapshot.isEmpty()) {
        return false;
    }
    if (!QDir().mkpath(m_rootDirectory)) {
        return false;
    }
    const QByteArray bytes =
        QJsonDocument(QJsonObject{{QStringLiteral("schema"), 1},
                                  {QStringLiteral("base_url"), baseUrl},
                                  {QStringLiteral("session"), session},
                                  {QStringLiteral("snapshot"), snapshot}})
            .toJson(QJsonDocument::Compact);
    if (bytes.size() > MaxCacheBytes) {
        return false;
    }
    QSaveFile file(pathFor(baseUrl, session));
    if (!file.open(QIODevice::WriteOnly)) {
        return false;
    }
    file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
    if (file.write(bytes) != bytes.size()) {
        file.cancelWriting();
        return false;
    }
    return file.commit();
}

void TranscriptCache::remove(const QString& baseUrl, const QString& session) const {
    QFile::remove(pathFor(baseUrl, session));
}

} // namespace clarp
