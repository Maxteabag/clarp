#include "protocol/ProtocolTypes.h"

#include <QJsonValue>
#include <QSet>

namespace clarp {
namespace {

QString stringValue(const QJsonObject& object, const char* key) {
    const QJsonValue value = object.value(QLatin1StringView(key));
    return value.isString() ? value.toString() : QString{};
}

qint64 integerValue(const QJsonObject& object, const char* key) {
    const QJsonValue value = object.value(QLatin1StringView(key));
    return value.isDouble() ? value.toInteger() : 0;
}

bool boolValue(const QJsonObject& object, const char* key) {
    const QJsonValue value = object.value(QLatin1StringView(key));
    return value.isBool() && value.toBool();
}

} // namespace

Agent Agent::fromJson(const QJsonObject& object) {
    Agent agent;
    agent.agentId = stringValue(object, "agent_id");
    agent.session = stringValue(object, "session");
    agent.persona = stringValue(object, "persona");
    agent.backend = stringValue(object, "backend");
    agent.workingDirectory = stringValue(object, "cwd");
    agent.model = stringValue(object, "model");
    agent.effort = stringValue(object, "effort");
    agent.avatarUrl = stringValue(object, "avatar_url");
    agent.avatarSymbol = stringValue(object, "avatar_symbol");
    agent.latestState = stringValue(object, "latest_state");
    agent.statusText = stringValue(object, "status_text");
    agent.lastMessage = stringValue(object, "last_message");
    agent.conversationId = stringValue(object, "conversation_id");
    agent.voiceId = stringValue(object, "voice_id");
    if (object.value(QStringLiteral("schedules")).isArray()) {
        agent.schedules = object.value(QStringLiteral("schedules")).toArray();
    }
    agent.latestStateTimestamp = integerValue(object, "latest_state_ts");
    agent.lastActivity = integerValue(object, "last_activity");
    agent.headRevision = integerValue(object, "head_revision");
    agent.contextTokens = integerValue(object, "context_tokens");
    agent.contextWindow = integerValue(object, "context_window");
    agent.queuedTurnCount = static_cast<int>(integerValue(object, "queued_turn_count"));
    agent.alive = boolValue(object, "alive");
    agent.busy = boolValue(object, "busy") || isBusyState(agent.latestState);
    agent.focused = boolValue(object, "focused");
    agent.muted = boolValue(object, "muted");
    agent.heartbeatEnabled = boolValue(object, "heartbeat_enabled");
    agent.dreamingEnabled = boolValue(object, "dreaming_enabled");
    agent.archived = !object.value(QStringLiteral("archived_at")).isNull() &&
                     !object.value(QStringLiteral("archived_at")).isUndefined();
    return agent;
}

Message Message::fromJson(const QJsonObject& object) {
    Message message;
    message.id = stringValue(object, "id");
    message.role = stringValue(object, "role");
    message.text = stringValue(object, "text");
    message.timestamp = stringValue(object, "timestamp");
    message.kind = stringValue(object, "kind");
    message.toolName = stringValue(object, "tool_name");
    message.origin = stringValue(object, "origin");
    message.senderName = stringValue(object, "sender_name");
    message.revision = integerValue(object, "revision");
    if (object.value(QStringLiteral("tools")).isArray()) {
        message.tools = object.value(QStringLiteral("tools")).toArray();
    }
    if (object.value(QStringLiteral("display_cells")).isArray()) {
        message.displayCells = object.value(QStringLiteral("display_cells")).toArray();
    }
    return message;
}

AudioClip AudioClip::fromJson(const QJsonObject& object) {
    AudioClip clip;
    clip.clipId = integerValue(object, "clip_id");
    clip.session = stringValue(object, "session");
    clip.persona = stringValue(object, "persona");
    clip.traceId = stringValue(object, "trace_id");
    clip.url = stringValue(object, "url");
    clip.streamUrl = stringValue(object, "stream_url");
    clip.playlistUrl = stringValue(object, "playlist_url");
    clip.completeUrl = stringValue(object, "complete_url");
    if (object.value(QStringLiteral("audio_format")).isObject()) {
        clip.audioFormat = object.value(QStringLiteral("audio_format")).toObject();
    }
    clip.preview = stringValue(object, "preview");
    return clip;
}

QString AudioClip::preferredSource() const {
    if (!playlistUrl.isEmpty()) {
        return playlistUrl;
    }
    if (!streamUrl.isEmpty()) {
        return streamUrl;
    }
    return url;
}

bool isBusyState(const QString& state) {
    static const QSet<QString> busyStates{
        QStringLiteral("thinking"),
        QStringLiteral("tool"),
        QStringLiteral("compacting"),
    };
    return busyStates.contains(state);
}

QString displayName(const Agent& agent) {
    return agent.persona.isEmpty() ? agent.session : agent.persona;
}

} // namespace clarp
