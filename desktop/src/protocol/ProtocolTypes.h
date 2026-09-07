#pragma once

#include <QDateTime>
#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QStringList>

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
    QString lastCompletedMessage;
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
    QString replyToAgentId;
    QString replyToName;
    QString replyToSession;
    QString delivery;
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
[[nodiscard]] QStringList markdownDisplayBlocks(const QString& markdown);

// Links reach the transcript from model output, tool results and fetched web
// pages, so the scheme is never trustworthy. Only hand the desktop handler the
// schemes a chat link legitimately needs; anything else stays inert text.
[[nodiscard]] bool isOpenableLink(const QString& link);

// Wrap the URLs inside verbatim tool output in anchors without altering the
// text itself. The result is Qt rich text: every original character is escaped
// and `white-space: pre-wrap` keeps the original indentation and line breaks
// while still wrapping to the card width.
[[nodiscard]] QString linkifiedPlainText(const QString& text);

// Verbatim output can be a whole file. Above this size the caller keeps the
// cheaper PlainText path rather than building a rich-text document.
inline constexpr int maxLinkifiedTextLength = 20'000;

// Qt's rich text engine renders a useful subset of HTML with no web engine, but
// it *does* fetch remote resources: <img src>, <table background>, CSS
// `url(...)` in both style attributes and <style> blocks, and CSS `@import`.
// An agent report assembled from scraped pages can therefore phone home the
// moment it is opened. Rewrite every such reference that is not self-contained
// (`data:`) or served by the user's own Host before rendering untrusted HTML.
[[nodiscard]] QString sanitizedReportHtml(const QString& html, const QString& hostOrigin);

// md4c's permissive autolinker does not recognise a URL with an explicit port,
// so `https://host:14443/path` renders as inert text while `https://host/path`
// becomes a link. Agent messages are full of ported URLs (tailscale serve,
// localhost dev servers, the Host itself), so wrap the ones md4c would miss in
// CommonMark autolink brackets before handing the text to the Markdown reader.
// Code spans, fenced blocks and existing links are left untouched.
[[nodiscard]] QString markdownWithExplicitAutolinks(const QString& markdown);

// True when the artifact body should be rendered as HTML rather than Markdown.
[[nodiscard]] bool looksLikeHtmlReport(const QString& content);

} // namespace clarp
