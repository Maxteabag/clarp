#include "app/WorkspaceContext.h"
#include <QDir>
#include <QFile>
#include <QFileInfo>

namespace clarp {
namespace {
QString firstLine(const QString& path) {
    QFile file(path);
    return file.open(QIODevice::ReadOnly) ? QString::fromUtf8(file.readLine(4096)).trimmed() : QString{};
}
QVariantMap inspect(const QString& path, bool shared) {
    QVariantMap result{{QStringLiteral("kind"), QStringLiteral("directory")},
        {QStringLiteral("path"), path}, {QStringLiteral("label"), path},
        {QStringLiteral("verified"), false}};
    if (!shared || !QDir::isAbsolutePath(path) || !QFileInfo(path).isDir()) return result;
    QDir directory(path);
    for (bool next = true; next; next = directory.cdUp()) {
        const QFileInfo marker(directory.filePath(QStringLiteral(".git")));
        if (!marker.exists()) continue;
        QString gitPath;
        if (marker.isDir()) {
            gitPath = marker.absoluteFilePath();
        } else {
            const QString pointer = firstLine(marker.absoluteFilePath());
            if (!pointer.startsWith(QStringLiteral("gitdir:"))) return result;
            gitPath = QDir::cleanPath(directory.absoluteFilePath(pointer.sliced(7).trimmed()));
        }
        if (!QFileInfo(gitPath).isDir()) return result;
        const QString common = firstLine(QDir(gitPath).filePath(QStringLiteral("commondir")));
        const bool worktree = !common.isEmpty();
        const QString commonPath = worktree ? QDir::cleanPath(QDir(gitPath).absoluteFilePath(common)) : gitPath;
        if (!QFileInfo(commonPath).isDir()) return result;
        QString repository = directory.dirName();
        if (worktree) {
            const QFileInfo commonInfo(commonPath);
            repository = commonInfo.fileName() == QStringLiteral(".git")
                ? QDir(commonInfo.absolutePath()).dirName() : commonInfo.fileName();
        }
        const QString relative = directory.relativeFilePath(path);
        QString label = repository;
        if (worktree) label += QStringLiteral(" / ") + directory.dirName();
        if (relative != QStringLiteral(".")) label += QStringLiteral(" / ") + relative;
        result.insert(QStringLiteral("kind"), worktree ? QStringLiteral("worktree") : QStringLiteral("repo"));
        result.insert(QStringLiteral("repository"), repository);
        result.insert(QStringLiteral("worktree"), worktree ? directory.dirName() : QString{});
        result.insert(QStringLiteral("root"), directory.absolutePath());
        result.insert(QStringLiteral("label"), label);
        result.insert(QStringLiteral("verified"), true);
        return result;
    }
    result.insert(QStringLiteral("verified"), true);
    return result;
}
}
WorkspaceContext::WorkspaceContext() { m_clock.start(); }
QVariantMap WorkspaceContext::describe(const QString& path, bool sharedFilesystem) const {
    const QString key = (sharedFilesystem ? QStringLiteral("local:") : QStringLiteral("host:")) + path;
    const qint64 now = m_clock.elapsed();
    const auto found = m_cache.constFind(key);
    if (found != m_cache.cend() && now - found->checkedAt < 5000) return found->details;
    const QVariantMap details = inspect(path, sharedFilesystem);
    if (m_cache.size() >= 128) m_cache.clear();
    m_cache.insert(key, Entry{.checkedAt = now, .details = details});
    return details;
}
}
