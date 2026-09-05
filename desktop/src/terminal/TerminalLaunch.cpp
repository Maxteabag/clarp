#include "terminal/TerminalLaunch.h"

namespace clarp {
TerminalLaunch nativeTerminalLaunch(const Agent& agent) {
    if (agent.conversationId.isEmpty() || agent.conversationId.startsWith(u'-') ||
        agent.conversationId.contains(QChar::Null)) {
        return {{}, {}, QStringLiteral("Send a chat message first to create a native CLI session")};
    }
    if (agent.backend == QStringLiteral("claude")) {
        return {QStringLiteral("claude"), {QStringLiteral("--resume"), agent.conversationId}, {}};
    }
    if (agent.backend == QStringLiteral("codex")) {
        return {QStringLiteral("codex"), {QStringLiteral("resume"), agent.conversationId}, {}};
    }
    if (agent.backend == QStringLiteral("agy")) {
        return {QStringLiteral("agy"), {QStringLiteral("--conversation"), agent.conversationId}, {}};
    }
    if (agent.backend == QStringLiteral("grok")) {
        return {QStringLiteral("grok"), {QStringLiteral("--resume"), agent.conversationId}, {}};
    }
    return {{}, {}, QStringLiteral("This backend does not have a supported interactive CLI")};
}
} // namespace clarp
