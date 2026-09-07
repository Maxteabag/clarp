#include "terminal/TerminalLaunch.h"

namespace clarp {
TerminalLaunch nativeTerminalLaunch(const Agent& agent) {
    if (agent.conversationId.isEmpty() || agent.conversationId.startsWith(u'-') ||
        agent.conversationId.contains(QChar::Null)) {
        return {.program = {}, .arguments = {}, .error = QStringLiteral("Send a chat message first to create a native CLI session")};
    }
    if (agent.backend == QStringLiteral("claude")) {
        return {.program = QStringLiteral("claude"), .arguments = {QStringLiteral("--resume"), agent.conversationId}, .error = {}};
    }
    if (agent.backend == QStringLiteral("codex")) {
        return {.program = QStringLiteral("codex"), .arguments = {QStringLiteral("resume"), agent.conversationId}, .error = {}};
    }
    if (agent.backend == QStringLiteral("agy")) {
        return {.program = QStringLiteral("agy"), .arguments = {QStringLiteral("--conversation"), agent.conversationId}, .error = {}};
    }
    if (agent.backend == QStringLiteral("grok")) {
        return {.program = QStringLiteral("grok"), .arguments = {QStringLiteral("--resume"), agent.conversationId}, .error = {}};
    }
    return {.program = {}, .arguments = {}, .error = QStringLiteral("This backend does not have a supported interactive CLI")};
}
} // namespace clarp
