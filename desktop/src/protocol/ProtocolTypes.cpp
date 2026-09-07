#include "protocol/ProtocolTypes.h"

#include <QJsonValue>
#include <QRegularExpression>
#include <QUrl>
#include <QSet>
#include <algorithm>
#include <ranges>

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

QString cleanedDisplayText(QString text, bool streaming) {
    static const QRegularExpression voxBlock(QStringLiteral(R"(<vox\b[^>]*>.*?</vox>)"),
                                             QRegularExpression::CaseInsensitiveOption |
                                                 QRegularExpression::DotMatchesEverythingOption);
    static const QRegularExpression speakTag(QStringLiteral(R"(</?speak\b[^>]*>)"),
                                             QRegularExpression::CaseInsensitiveOption);
    static const QRegularExpression audioTag(
        QStringLiteral(R"(</?(?:break|speed|volume|emotion)\b[^>]*/?>)"),
        QRegularExpression::CaseInsensitiveOption);
    text.remove(voxBlock);
    text.remove(speakTag);
    text.remove(audioTag);
    if (streaming) {
        const qsizetype openVox = text.lastIndexOf(QStringLiteral("<vox"), -1, Qt::CaseInsensitive);
        const qsizetype closeVox =
            text.lastIndexOf(QStringLiteral("</vox>"), -1, Qt::CaseInsensitive);
        if (openVox >= 0 && openVox > closeVox) {
            text.truncate(openVox);
        }
        const qsizetype marker = text.lastIndexOf(u'<');
        if (marker >= 0) {
            const QString tail = text.sliced(marker).toLower();
            static const QStringList prefixes{
                QStringLiteral("<speak"),   QStringLiteral("</speak"),  QStringLiteral("<vox"),
                QStringLiteral("</vox"),    QStringLiteral("<break"),   QStringLiteral("</break"),
                QStringLiteral("<speed"),   QStringLiteral("</speed"),  QStringLiteral("<volume"),
                QStringLiteral("</volume"), QStringLiteral("<emotion"), QStringLiteral("</emotion"),
            };
            const bool incompleteVoiceTag =
                !tail.contains(u'>') &&
                std::ranges::any_of(prefixes, [&tail](const QString& prefix) {
                    return prefix.startsWith(tail) || tail.startsWith(prefix);
                });
            if (incompleteVoiceTag) {
                text.truncate(marker);
            }
        }
    }
    return text.trimmed();
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
    agent.lastCompletedMessage = stringValue(object, "last_completed_message");
    agent.conversationId = stringValue(object, "conversation_id");
    agent.voiceId = stringValue(object, "voice_id");
    if (object.value(QStringLiteral("schedules")).isArray()) {
        agent.schedules = object.value(QStringLiteral("schedules")).toArray();
    }
    if (object.value(QStringLiteral("mcp_servers")).isArray()) {
        agent.mcpServers = object.value(QStringLiteral("mcp_servers")).toArray();
    }
    if (object.value(QStringLiteral("team_ids")).isArray()) {
        agent.teamIds = object.value(QStringLiteral("team_ids")).toArray();
    }
    agent.latestStateTimestamp = integerValue(object, "latest_state_ts");
    agent.lastActivity = integerValue(object, "last_activity");
    agent.headRevision = integerValue(object, "head_revision");
    agent.contextTokens = integerValue(object, "context_tokens");
    agent.contextWindow = integerValue(object, "context_window");
    agent.queuedTurnCount = static_cast<int>(integerValue(object, "queued_turn_count"));
    agent.queueRevision = integerValue(object, "queue_revision");
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
    message.displayText = cleanedDisplayText(message.text, message.kind == QStringLiteral("live"));
    message.toolName = stringValue(object, "tool_name");
    message.origin = stringValue(object, "origin");
    message.senderName = stringValue(object, "sender_name");
    message.senderAgentId = stringValue(object, "sender_agent_id");
    message.senderSession = stringValue(object, "sender_session");
    message.replyToAgentId = stringValue(object, "reply_to_agent_id");
    message.replyToName = stringValue(object, "reply_to_name");
    message.replyToSession = stringValue(object, "reply_to_session");
    message.delivery = stringValue(object, "delivery");
    message.traceId = stringValue(object, "trace_id");
    message.category = stringValue(object, "category");
    if (message.category.isEmpty()) {
        message.category = stringValue(object, "automated_category");
    }
    message.revision = integerValue(object, "revision");
    message.activityCount = static_cast<int>(integerValue(object, "activity_count"));
    message.automated = boolValue(object, "automated") || boolValue(object, "is_automated");
    message.toolDetailsAvailable = boolValue(object, "tool_details_available");
    message.deliveryFailed = boolValue(object, "delivery_failed");
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

QString voiceDeliverySession(const QString& captureSession, const QString& currentSession) {
    return captureSession.isEmpty() ? currentSession : captureSession;
}

bool isOpenableLink(const QString& link) {
    const QUrl url(link.trimmed(), QUrl::StrictMode);
    if (!url.isValid() || url.isRelative() || url.isEmpty()) {
        return false;
    }
    const QString scheme = url.scheme().toLower();
    if (scheme == QStringLiteral("mailto")) {
        return !url.path().isEmpty();
    }
    // A host is required so "https:///etc/passwd" cannot reach the handler.
    return (scheme == QStringLiteral("http") || scheme == QStringLiteral("https"))
        && !url.host().isEmpty();
}

QString linkifiedPlainText(const QString& text) {
    // Stop before the closing punctuation that usually follows a URL in prose
    // so a trailing ")" or "." is not swallowed into the target.
    static const QRegularExpression candidate(
        QStringLiteral(R"((?:https?://|www\.)[^\s<>"']+)"));

    QString result;
    result.reserve(text.size() + 64);
    result += QStringLiteral("<div style=\"white-space: pre-wrap;\">");

    qsizetype cursor = 0;
    auto matches = candidate.globalMatch(text);
    while (matches.hasNext()) {
        const QRegularExpressionMatch match = matches.next();
        QString found = match.captured();
        while (!found.isEmpty()
               && QStringLiteral(".,;:!?)]}'\"").contains(found.back())) {
            found.chop(1);
        }
        if (found.isEmpty()) {
            continue;
        }
        const QString target = found.startsWith(QStringLiteral("www."))
            ? QStringLiteral("https://") + found : found;
        if (!isOpenableLink(target)) {
            continue;
        }
        result += text.mid(cursor, match.capturedStart() - cursor).toHtmlEscaped();
        result += QStringLiteral("<a href=\"%1\">%2</a>")
                      .arg(target.toHtmlEscaped(), found.toHtmlEscaped());
        cursor = match.capturedStart() + found.size();
    }
    result += text.mid(cursor).toHtmlEscaped();
    result += QStringLiteral("</div>");
    return result;
}

namespace {

// A resource may load only if it carries its own bytes or comes from the Host
// the user is already authenticated against.
bool reportResourceAllowed(QString reference, const QString& hostOrigin) {
    reference = reference.trimmed();
    while (reference.size() >= 2
           && ((reference.startsWith(u'"') && reference.endsWith(u'"'))
               || (reference.startsWith(u'\'') && reference.endsWith(u'\'')))) {
        reference = reference.mid(1, reference.size() - 2).trimmed();
    }
    if (reference.isEmpty()) {
        return false;
    }
    if (reference.startsWith(QStringLiteral("data:"), Qt::CaseInsensitive)) {
        return true;
    }
    return !hostOrigin.isEmpty()
        && reference.startsWith(hostOrigin, Qt::CaseInsensitive);
}

} // namespace

QString markdownWithExplicitAutolinks(const QString& markdown) {
    // Only URLs carrying an explicit port need help; md4c already links the
    // rest, and rewriting those would needlessly churn well-formed text.
    static const QRegularExpression ported(
        QStringLiteral(R"(https?://[^\s<>"'`\]\)]*:\d{1,5}(?:/[^\s<>"'`\]\)]*)?)"));

    const QStringList lines = markdown.split(u'\n');
    QStringList output;
    output.reserve(lines.size());
    bool inFence = false;

    for (const QString& line : lines) {
        const QString trimmed = line.trimmed();
        if (trimmed.startsWith(QStringLiteral("```"))
            || trimmed.startsWith(QStringLiteral("~~~"))) {
            inFence = !inFence;
            output.append(line);
            continue;
        }
        // Indented code blocks and fenced content must stay literal.
        if (inFence || line.startsWith(QStringLiteral("    "))
            || line.startsWith(u'\t')) {
            output.append(line);
            continue;
        }

        QString rewritten;
        rewritten.reserve(line.size() + 16);
        qsizetype cursor = 0;
        bool inCodeSpan = false;
        auto matches = ported.globalMatch(line);
        while (matches.hasNext()) {
            const QRegularExpressionMatch match = matches.next();
            const QString before = line.mid(cursor, match.capturedStart() - cursor);
            // Track backtick parity so a URL inside `code` is left alone.
            inCodeSpan ^= (before.count(u'`') % 2) != 0;
            rewritten += before;
            const QChar preceding = match.capturedStart() > 0
                ? line.at(match.capturedStart() - 1) : QChar(u' ');
            // Already a markdown link target, an existing autolink, or code.
            const bool alreadyLinked = preceding == u'(' || preceding == u'<';
            if (inCodeSpan || alreadyLinked) {
                rewritten += match.captured();
            } else {
                rewritten += u'<' + match.captured() + u'>';
            }
            cursor = match.capturedEnd();
        }
        rewritten += line.mid(cursor);
        output.append(rewritten);
    }
    return output.join(u'\n');
}

bool looksLikeHtmlReport(const QString& content) {
    static const QRegularExpression markup(
        QStringLiteral(R"((?i)<(!doctype\s+html|html|head|body|div|table|h[1-6]|p|ul|ol|section|article|style)\b)"));
    return markup.match(content).hasMatch();
}

QString sanitizedReportHtml(const QString& html, const QString& hostOrigin) {
    QString result = html;

    // Qt ignores <script> when rendering, but never carry it into the document.
    static const QRegularExpression scripts(
        QStringLiteral(R"((?is)<script\b[^>]*>.*?</script\s*>)"));
    result.remove(scripts);

    // CSS @import fetches a stylesheet; there is no safe rewrite, so drop it.
    static const QRegularExpression cssImport(
        QStringLiteral(R"((?is)@import\s+[^;}]*;?)"));
    result.remove(cssImport);

    // Every CSS url(...) reference, in <style> blocks and style attributes.
    static const QRegularExpression cssUrl(
        QStringLiteral(R"((?is)url\(\s*([^)]*)\s*\))"));
    QString cssRewritten;
    cssRewritten.reserve(result.size());
    qsizetype cursor = 0;
    auto cssMatches = cssUrl.globalMatch(result);
    while (cssMatches.hasNext()) {
        const QRegularExpressionMatch match = cssMatches.next();
        cssRewritten += result.mid(cursor, match.capturedStart() - cursor);
        if (reportResourceAllowed(match.captured(1), hostOrigin)) {
            cssRewritten += match.captured();
        } else {
            // "none" keeps the declaration syntactically valid without a fetch.
            cssRewritten += QStringLiteral("none");
        }
        cursor = match.capturedEnd();
    }
    cssRewritten += result.mid(cursor);
    result = cssRewritten;

    // Attributes Qt resolves as resources: <img src> and the legacy
    // background= attribute honoured on table elements.
    static const QRegularExpression resourceAttribute(
        QStringLiteral(R"((?is)\b(src|background)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+))"));
    QString attributeRewritten;
    attributeRewritten.reserve(result.size());
    cursor = 0;
    auto attributeMatches = resourceAttribute.globalMatch(result);
    while (attributeMatches.hasNext()) {
        const QRegularExpressionMatch match = attributeMatches.next();
        attributeRewritten += result.mid(cursor, match.capturedStart() - cursor);
        if (reportResourceAllowed(match.captured(2), hostOrigin)) {
            attributeRewritten += match.captured();
        } else {
            // Keep the attribute present but unresolvable, so the layout still
            // reserves the element and nothing leaves the machine.
            attributeRewritten += match.captured(1) + QStringLiteral("=\"\"");
        }
        cursor = match.capturedEnd();
    }
    attributeRewritten += result.mid(cursor);
    return attributeRewritten;
}

QStringList markdownDisplayBlocks(const QString& markdown) {
    QString normalized = markdown;
    normalized.replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
    normalized.replace(u'\r', u'\n');
    if (normalized.trimmed().isEmpty()) {
        return {};
    }

    const QStringList lines = normalized.split(u'\n');
    QStringList blocks;
    QStringList current;
    QString fence;

    const auto listKind = [](const QString& line) -> int {
        static const QRegularExpression bullet(QStringLiteral(R"(^\s*[-+*]\s+\S)"));
        static const QRegularExpression ordered(QStringLiteral(R"(^\s*\d+[.)]\s+\S)"));
        if (bullet.match(line).hasMatch()) {
            return 1;
        }
        return ordered.match(line).hasMatch() ? 2 : 0;
    };
    const auto flush = [&blocks, &current] {
        while (!current.isEmpty() && current.constLast().trimmed().isEmpty()) {
            current.removeLast();
        }
        if (!current.isEmpty()) {
            blocks.append(current.join(u'\n'));
            current.clear();
        }
    };

    for (qsizetype index = 0; index < lines.size(); ++index) {
        const QString& line = lines.at(index);
        const QString trimmed = line.trimmed();
        if (!fence.isEmpty()) {
            current.append(line);
            if (trimmed.startsWith(fence)) {
                fence.clear();
            }
            continue;
        }
        if (trimmed.startsWith(QStringLiteral("```")) ||
            trimmed.startsWith(QStringLiteral("~~~"))) {
            fence = trimmed.first(3);
            current.append(line);
            continue;
        }
        if (!trimmed.isEmpty()) {
            current.append(line);
            continue;
        }

        qsizetype nextIndex = index + 1;
        while (nextIndex < lines.size() && lines.at(nextIndex).trimmed().isEmpty()) {
            ++nextIndex;
        }
        int previousKind = 0;
        for (const auto& previous : std::views::reverse(current)) {
            if (!previous.trimmed().isEmpty()) {
                previousKind = listKind(previous);
                break;
            }
        }
        const int nextKind = nextIndex < lines.size() ? listKind(lines.at(nextIndex)) : 0;
        if (previousKind != 0 && previousKind == nextKind) {
            if (!current.constLast().isEmpty()) {
                current.append(QString{});
            }
        } else {
            flush();
        }
    }
    flush();
    return blocks;
}

} // namespace clarp
