#pragma once
#include <QProcessEnvironment>
#include <QStringList>
namespace clarp {
inline QProcessEnvironment previewRelaunchEnvironment(QProcessEnvironment environment,
                                                     const QString& host, const QString& session) {
    environment.insert(QStringLiteral("CLARP_BASE_URL"), host);
    environment.insert(QStringLiteral("CLARP_RESTORE_SESSION"), session);
    environment.insert(QStringLiteral("CLARP_RESTORE_DESKTOP"), QStringLiteral("1"));
    return environment;
}
inline QStringList previewRelaunchArguments(const QString& helper, qint64 pid) {
    return {helper, QStringLiteral("--restart-after"), QString::number(pid)};
}
}
