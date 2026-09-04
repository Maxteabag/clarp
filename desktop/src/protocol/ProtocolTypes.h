#pragma once

#include <QDateTime>
#include <QJsonArray>
#include <QJsonObject>
#include <QString>

namespace clarp {

struct Agent {
    QString agentId;
    QString session;
    QString persona;
    QString backend;
    QString workingDirectory;
    QString model;
    QString effort;
    QString avatarUrl;
    QString avatarSymbol;
    QString latestState;
    QString statusText;
    QString lastMessage;
    QString conversationId;
    QString voiceId;
    QJsonArray schedules;
    QJsonArray mcpServers;
    QJsonArray teamIds;
    qint64 latestStateTimestamp = 0;
    qint64 lastActivity = 0;
    qint64 headRevision = 0;
    qint64 contextTokens = 0;
    qint64 contextWindow = 0;
    qint64 queueRevision = 0;
    int queuedTurnCount = 0;
    bool alive = false;
    bool busy = false;
    bool focused = false;
    bool muted = false;
    bool heartbeatEnabled = false;
    bool dreamingEnabled = false;
    bool archived = false;
    bool unread = false;

    [[nodiscard]] static Agent fromJson(const QJsonObject& object);
};

struct Message {
    QString id;
    QString role;
    QString text;
    QString displayText;
    QString timestamp;
    QString kind;
    QString toolName;
    QString origin;
    QString senderName;
    QString senderAgentId;
    QString senderSession;
    QString traceId;
    QString category;
    QString activityStatus;
    QString activityMatchKey;
    QJsonArray tools;
    QJsonArray displayCells;
    qint64 revision = 0;
    int activityCount = 0;
    bool pending = false;
    bool deliveryFailed = false;
    bool activity = false;
    bool automated = false;
    bool toolDetailsAvailable = false;

    [[nodiscard]] static Message fromJson(const QJsonObject& object);
};

struct AudioClip {
    qint64 clipId = 0;
    QString session;
    QString persona;
    QString traceId;
    QString url;
    QString streamUrl;
    QString playlistUrl;
    QString completeUrl;
    QJsonObject audioFormat;
    QString preview;

    [[nodiscard]] static AudioClip fromJson(const QJsonObject& object);
    [[nodiscard]] QString preferredSource() const;
};

[[nodiscard]] bool isBusyState(const QString& state);
[[nodiscard]] QString displayName(const Agent& agent);
[[nodiscard]] QString voiceDeliverySession(const QString& captureSession,
                                           const QString& currentSession);

} // namespace clarp
