#pragma once
#include <QFileInfo>
#include <QFile>
#include <QMimeDatabase>
#include <QSet>
#include <QUrl>
namespace clarp {
// Explicit local-report action only; never widen the general URL scheme policy.
inline QUrl localReportUrl(const QString& link) {
    QString path;
    if (link.startsWith('/') && !link.startsWith("//")) {
        path = link;
    } else {
        const QUrl url(link, QUrl::StrictMode);
        if (!url.isValid() || url.scheme() != QStringLiteral("file") ||
            !url.host().isEmpty() || url.hasQuery() || url.hasFragment()) return {};
        path = url.toLocalFile();
    }
    const QFileInfo supplied(path);
    const QString canonical = supplied.canonicalFilePath();
    if (canonical.isEmpty()) return {};
    const QFileInfo file(canonical);
    // A privileged process may report a mode-000 file as readable. Respect
    // the file's explicit read bits as well as effective access permission.
    const auto readBits = QFileDevice::ReadOwner | QFileDevice::ReadGroup | QFileDevice::ReadOther;
    if (!file.isFile() || !(file.permissions() & readBits) || !file.isReadable() || file.isExecutable()) return {};
    static const QSet<QString> extensions{"html", "htm", "pdf", "txt", "md", "csv", "log",
                                          "json", "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"};
    if (!extensions.contains(file.suffix().toLower())) return {};
    QFile input(canonical);
    if (!input.open(QIODevice::ReadOnly)) return {};
    const QByteArray prefix = input.read(4096);
    if (prefix.startsWith("#!") || prefix.startsWith("\x7f" "ELF") ||
        prefix.startsWith("MZ") || prefix.contains("[Desktop Entry]")) return {};
    const QString mime = QMimeDatabase().mimeTypeForFile(file, QMimeDatabase::MatchContent).name();
    if (!(mime.startsWith("text/") || mime.startsWith("image/") ||
          mime == "application/pdf" || mime == "application/json" || mime == "application/x-empty")) return {};
    return QUrl::fromLocalFile(canonical);
}
}
