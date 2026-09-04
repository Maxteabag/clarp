#include "app/AppController.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QCryptographicHash>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QDesktopServices>
#include <QProcess>
#include <QRegularExpression>
#include <QSettings>
#include <QUrlQuery>
#include <QUuid>
#include <algorithm>
#include <utility>

namespace clarp {
namespace {

constexpr int DeliveryTimeoutMs = 20'000;
constexpr qsizetype MaxPortraitBytes = 20 * 1024 * 1024;
constexpr qsizetype MaxComposerUploadBytes = 50 * 1024 * 1024;

QString avatarUrlForAgent(const Agent& agent) {
    if (!agent.avatarUrl.isEmpty()) {
        return agent.avatarUrl;
    }
    static const QSet<QString> bundled{
        QStringLiteral("adam"),  QStringLiteral("antoni"), QStringLiteral("arnold"),
        QStringLiteral("bella"), QStringLiteral("caleb"),  QStringLiteral("diego"),
        QStringLiteral("domi"),  QStringLiteral("elli"),   QStringLiteral("freya"),
        QStringLiteral("josh"),  QStringLiteral("lena"),   QStringLiteral("marcus"),
        QStringLiteral("mike"),  QStringLiteral("nadia"),  QStringLiteral("omar"),
        QStringLiteral("priya"), QStringLiteral("rachel"), QStringLiteral("sam"),
        QStringLiteral("theo"),  QStringLiteral("yuki"),
    };
    QString slug = displayName(agent).toLower();
    slug.remove(QRegularExpression(QStringLiteral("[^a-z0-9_-]")));
    return bundled.contains(slug) ? QStringLiteral("/static/avatars/%1.png").arg(slug) : QString{};
}

QString normalizedBaseUrl(QString value) {
    value = value.trimmed();
    if (value.isEmpty()) {
        return QStringLiteral("http://127.0.0.1:7682");
    }
    while (value.endsWith('/')) {
        value.chop(1);
    }
    return value;
}

QString draftScopeSettingsKey(const QString& baseUrl, const QString& session) {
    const QByteArray identity = (normalizedBaseUrl(baseUrl) + QChar::Null + session).toUtf8();
    const QString digest = QString::fromLatin1(
        QCryptographicHash::hash(identity, QCryptographicHash::Sha256).toHex());
    return QStringLiteral("composerDrafts/%1").arg(digest);
}

QString draftSettingsKey(const QString& baseUrl, const QString& session) {
    return draftScopeSettingsKey(baseUrl, session) + QStringLiteral("/text");
}

QString draftAttachmentsSettingsKey(const QString& baseUrl, const QString& session) {
    return draftScopeSettingsKey(baseUrl, session) + QStringLiteral("/attachments");
}

QString sharedFilesystemSettingsKey(const QString& baseUrl) {
    const QString digest = QString::fromLatin1(
        QCryptographicHash::hash(normalizedBaseUrl(baseUrl).toUtf8(), QCryptographicHash::Sha256)
            .toHex());
    return QStringLiteral("connections/%1/sharedFilesystem").arg(digest);
}

} // namespace

AppController::AppController(QObject* parent)
    : QObject(parent), m_credentials(this), m_audio(this), m_agents(this),
      m_archivedAgents(true, this), m_contacts(this), m_panes(this), m_voices(this),
      m_emptyConversation(this), m_conversation(&m_emptyConversation) {
    m_cacheEnabled = !qEnvironmentVariableIsSet("CLARP_SCREENSHOT_SCENARIO");
    QSettings settings;
    m_baseUrl = normalizedBaseUrl(
        qEnvironmentVariable("CLARP_BASE_URL", settings
                                                   .value(QStringLiteral("connection/baseUrl"),
                                                          QStringLiteral("http://127.0.0.1:7682"))
                                                   .toString()));
    m_muted = settings.value(QStringLiteral("audio/muted"), false).toBool();
    m_toolsVisible = settings.value(QStringLiteral("conversation/toolsVisible"), false).toBool();
    m_timestampsVisible =
        settings.value(QStringLiteral("conversation/timestampsVisible"), false).toBool();
    const QString sharedFilesystemHost =
        qEnvironmentVariable("CLARP_SHARED_FILESYSTEM_HOST").trimmed();
    m_sharedFilesystemHostOverride =
        sharedFilesystemHost.isEmpty() ? QString{} : normalizedBaseUrl(sharedFilesystemHost);
    m_sharedFilesystem =
        (!m_sharedFilesystemHostOverride.isEmpty() &&
         m_sharedFilesystemHostOverride == m_baseUrl) ||
        settings.value(sharedFilesystemSettingsKey(m_baseUrl), false).toBool();
    settings.remove(QStringLiteral("connection/sharedFilesystem"));
    m_lastWorkingDirectory =
        settings.value(QStringLiteral("launch/workingDirectory"), QStringLiteral("~")).toString();
    m_lastBackend = settings.value(QStringLiteral("launch/backend")).toString();
    m_bearerToken = qEnvironmentVariable("CLARP_TOKEN", defaultToken());

    connect(&m_api, &ApiClient::jsonReceived, this, &AppController::handleJson);
    connect(&m_api, &ApiClient::bytesReceived, this, &AppController::handleBytes);
    connect(&m_api, &ApiClient::requestFailed, this, &AppController::handleRequestFailure);
    connect(&m_credentials, &CredentialStore::lookupFinished, this,
            [this](const QString& serverUrl, const QString& token) {
                if (normalizedBaseUrl(serverUrl) != m_baseUrl) {
                    return;
                }
                if (m_bearerToken.isEmpty()) {
                    m_bearerToken = token;
                }
                const bool stored = !token.isEmpty();
                if (m_hasStoredCredential != stored) {
                    m_hasStoredCredential = stored;
                    emit hasStoredCredentialChanged();
                }
                reconnect();
            });
    connect(&m_credentials, &CredentialStore::storeFinished, this,
            [this](const QString& serverUrl) {
                if (normalizedBaseUrl(serverUrl) == m_baseUrl && !m_hasStoredCredential) {
                    m_hasStoredCredential = true;
                    emit hasStoredCredentialChanged();
                }
            });
    connect(&m_credentials, &CredentialStore::removeFinished, this,
            [this](const QString& serverUrl) {
                if (normalizedBaseUrl(serverUrl) != m_baseUrl) {
                    return;
                }
                m_bearerToken.clear();
                m_sse.stop();
                m_audio.silence();
                setConnecting(false);
                setConnectionState(QStringLiteral("offline"));
                setErrorMessage({});
                if (m_hasStoredCredential) {
                    m_hasStoredCredential = false;
                    emit hasStoredCredentialChanged();
                }
            });
    connect(&m_credentials, &CredentialStore::storeFailed, this, [this](const QString& message) {
        if (!message.isEmpty()) {
            setErrorMessage(message);
        }
    });
    connect(&m_sse, &SseClient::eventReceived, this, &AppController::handleSseEvent);
    connect(&m_panes, &PaneTreeModel::activePaneChanged, this, [this] {
        const QString session = m_panes.activeSession();
        if (!session.isEmpty() && session != m_selectedSession) {
            selectSession(session);
        }
    });
    const auto bumpAgentRevision = [this] {
        ++m_agentRevision;
        emit agentRevisionChanged();
    };
    connect(&m_agents, &QAbstractItemModel::modelReset, this, bumpAgentRevision);
    connect(&m_agents, &QAbstractItemModel::dataChanged, this, bumpAgentRevision);
    connect(&m_agents, &QAbstractItemModel::rowsInserted, this, bumpAgentRevision);
    connect(&m_agents, &QAbstractItemModel::rowsRemoved, this, bumpAgentRevision);
    connect(&m_agents, &QAbstractItemModel::rowsMoved, this, bumpAgentRevision);
    connect(&m_sse, &SseClient::connectedChanged, this, [this] {
        emit connectedChanged();
        setConnectionState(m_sse.connected() ? QStringLiteral("live")
                                             : QStringLiteral("reconnecting"));
        if (m_sse.connected()) {
            requestSnapshot();
        }
    });
    connect(&m_sse, &SseClient::connectionError, this,
            [this](const QString& message) { setErrorMessage(message); });
    connect(&m_audio, &AudioController::mediaError, this, &AppController::setErrorMessage);
    connect(&m_audio, &AudioController::transcriptionReady, this,
            [this](const QString& text, const QString& traceId, const QString& transcriptionId,
                   bool handsFree) {
                sendMessageInternal(m_selectedSession, text, false, traceId, transcriptionId,
                                    handsFree);
            });
    QTimer::singleShot(0, this, [this] {
        if (m_bearerToken.isEmpty()) {
            m_credentials.lookup(m_baseUrl);
        } else {
            reconnect();
        }
    });
}

AgentListModel* AppController::agents() { return &m_agents; }

AgentListModel* AppController::archivedAgents() { return &m_archivedAgents; }

AudioController* AppController::audio() { return &m_audio; }

ConversationModel* AppController::conversation() { return m_conversation; }

ContactListModel* AppController::contacts() { return &m_contacts; }

PaneTreeModel* AppController::panes() { return &m_panes; }

VoiceListModel* AppController::voices() { return &m_voices; }

QString AppController::voiceBio() const { return m_voiceBio; }

bool AppController::voicesLoading() const { return m_voicesLoading; }

QVariantMap AppController::orchestratorSettings() const { return m_orchestratorSettings; }

QString AppController::orchestratorLastDecision() const { return m_orchestratorLastDecision; }

bool AppController::orchestratorLoading() const { return m_orchestratorLoading; }

QVariantList AppController::backendOptions() const {
    QVariantList options;
    const QVariantMap providers = m_modelCatalog.value(QStringLiteral("providers")).toMap();
    QList<QPair<int, QVariantMap>> sorted;
    for (auto it = providers.cbegin(); it != providers.cend(); ++it) {
        const QVariantMap provider = it.value().toMap();
        if (provider.value(QStringLiteral("hidden")).toBool() ||
            !provider.value(QStringLiteral("installed"), true).toBool()) {
            continue;
        }
        sorted.append({provider.value(QStringLiteral("sort_index"), 1'000).toInt(),
                       QVariantMap{
                           {QStringLiteral("id"), it.key()},
                           {QStringLiteral("label"),
                            provider.value(QStringLiteral("label"), it.key()).toString()},
                       }});
    }
    std::ranges::stable_sort(
        sorted, [](const auto& left, const auto& right) { return left.first < right.first; });
    for (const auto& option : std::as_const(sorted)) {
        options.append(option.second);
    }
    if (options.isEmpty()) {
        options = {
            QVariantMap{{QStringLiteral("id"), QStringLiteral("claude")},
                        {QStringLiteral("label"), QStringLiteral("Claude")}},
            QVariantMap{{QStringLiteral("id"), QStringLiteral("codex")},
                        {QStringLiteral("label"), QStringLiteral("Codex")}},
            QVariantMap{{QStringLiteral("id"), QStringLiteral("agy")},
                        {QStringLiteral("label"), QStringLiteral("Antigravity")}},
        };
    }
    return options;
}

QVariantList AppController::availableMcpServers() const { return m_availableMcpServers; }

quint64 AppController::agentRevision() const { return m_agentRevision; }

quint64 AppController::avatarRevision() const { return m_avatarRevision; }

QVariantList AppController::pastSessions() const { return m_pastSessions; }

bool AppController::pastSessionsLoading() const { return m_pastSessionsLoading; }

QVariantList AppController::directorySuggestions() const { return m_directorySuggestions; }

QVariantList AppController::favoritePaths() const { return m_favoritePaths; }

QString AppController::lastWorkingDirectory() const { return m_lastWorkingDirectory; }

QString AppController::lastBackend() const { return m_lastBackend; }

bool AppController::hasStoredCredential() const { return m_hasStoredCredential; }

ConversationModel* AppController::conversationForSession(const QString& session) {
    return ensureConversation(session);
}

QUrl AppController::avatarSource(const QString& session) const {
    return m_avatarSources.value(session);
}

QString AppController::baseUrl() const { return m_baseUrl; }

QString AppController::selectedSession() const { return m_selectedSession; }

QString AppController::selectedName() const {
    const Agent* agent = m_agents.find(m_selectedSession);
    return agent == nullptr ? QString{} : displayName(*agent);
}

QString AppController::selectedState() const {
    const Agent* agent = m_agents.find(m_selectedSession);
    return agent == nullptr ? QString{} : agent->latestState;
}

QString AppController::selectedBackend() const {
    const Agent* agent = m_agents.find(m_selectedSession);
    return agent == nullptr ? QString{} : agent->backend;
}

bool AppController::connected() const { return m_sse.connected(); }

bool AppController::connecting() const { return m_connecting; }

bool AppController::sending() const { return m_sending; }

bool AppController::muted() const { return m_muted; }

bool AppController::toolsVisible() const { return m_toolsVisible; }

bool AppController::timestampsVisible() const { return m_timestampsVisible; }

bool AppController::sharedFilesystem() const { return m_sharedFilesystem; }

QString AppController::connectionState() const { return m_connectionState; }

QString AppController::errorMessage() const { return m_errorMessage; }

QString AppController::serverName() const { return m_serverName; }

QString AppController::serverVersion() const { return m_serverVersion; }

QString AppController::composerFocusPane() const { return m_composerFocusPane; }

quint64 AppController::composerRevision() const { return m_composerRevision; }

QVariantList AppController::attentionItems() const { return m_attentionItems; }

QVariantList AppController::backgroundJobs() const { return m_backgroundJobs; }

QVariantList AppController::updateArtifacts() const { return m_updateArtifacts; }

bool AppController::updatesLoading() const { return m_updateRequestsPending > 0; }

QString AppController::updatesError() const { return m_updatesError; }

int AppController::attentionCount() const { return static_cast<int>(m_attentionItems.size()); }

QVariantList AppController::teams() const { return m_teams; }

QVariantList AppController::teamMessages() const { return m_teamMessages; }

QString AppController::selectedTeamId() const { return m_selectedTeamId; }

bool AppController::teamsLoading() const { return m_teamListLoading || m_teamMessagesLoading; }

QString AppController::teamsError() const { return m_teamsError; }

QVariantList AppController::turnQueueItems() const { return m_turnQueueItems; }

QString AppController::turnQueueSession() const { return m_turnQueueSession; }

bool AppController::turnQueuePaused() const { return m_turnQueuePaused; }

bool AppController::turnQueueLoading() const { return m_turnQueueLoading; }

QString AppController::turnQueueError() const { return m_turnQueueError; }

QVariantMap AppController::profileTaskPlan() const { return m_profileTaskPlan; }

QString AppController::profileSession() const { return m_profileSession; }

bool AppController::profileLoading() const { return m_profileLoading; }

QString AppController::profileError() const { return m_profileError; }

void AppController::setBaseUrl(const QString& value) {
    const QString normalized = normalizedBaseUrl(value);
    if (m_baseUrl == normalized) {
        return;
    }
    resetTransientRequestState();
    m_sse.stop();
    m_audio.silence();
    m_api.setEndpoint(QUrl(normalized), {});
    m_baseUrl = normalized;
    const bool shared =
        (!m_sharedFilesystemHostOverride.isEmpty() &&
         m_sharedFilesystemHostOverride == m_baseUrl) ||
        QSettings().value(sharedFilesystemSettingsKey(m_baseUrl), false).toBool();
    if (m_sharedFilesystem != shared) {
        m_sharedFilesystem = shared;
        emit sharedFilesystemChanged();
    }
    m_attentionItems.clear();
    m_backgroundJobs.clear();
    m_updateArtifacts.clear();
    m_updatesError.clear();
    m_teams.clear();
    m_teamMessages.clear();
    m_selectedTeamId.clear();
    m_teamsError.clear();
    m_turnQueueItems.clear();
    m_turnQueueSession.clear();
    m_turnQueueError.clear();
    m_turnQueuePaused = false;
    m_profileTaskPlan.clear();
    m_profileSession.clear();
    m_profileError.clear();
    m_modelCatalog.clear();
    m_availableMcpServers.clear();
    m_pastSessions.clear();
    m_directorySuggestions.clear();
    m_favoritePaths.clear();
    m_orchestratorSettings.clear();
    m_orchestratorLastDecision.clear();
    m_serverName.clear();
    m_serverVersion.clear();
    m_agents.applySnapshot({{QStringLiteral("agents"), QJsonArray{}}});
    m_archivedAgents.applySnapshot({{QStringLiteral("agents"), QJsonArray{}}});
    m_contacts.applySnapshot({}, {});
    m_selectedSession.clear();
    m_conversation = &m_emptyConversation;
    m_emptyConversation.openSession({});
    ++m_composerRevision;
    for (auto it = m_conversations.cbegin(); it != m_conversations.cend(); ++it) {
        if (it.value() == nullptr) {
            continue;
        }
        it.value()->openSession({});
        it.value()->openSession(it.key());
        it.value()->restoreCacheSnapshot(m_transcriptCache.load(m_baseUrl, it.key()));
    }
    QSettings().setValue(QStringLiteral("connection/baseUrl"), m_baseUrl);
    emit baseUrlChanged();
    emit selectedSessionChanged();
    emit selectedAgentChanged();
    emit conversationChanged();
    emit composerRevisionChanged();
    emit updatesChanged();
    emit teamsChanged();
    emit turnQueueChanged();
    emit profileChanged();
    emit modelCatalogChanged();
    emit pastSessionsChanged();
    emit pathsChanged();
    emit orchestratorChanged();
    emit serverInfoChanged();
}

void AppController::setMuted(bool muted) {
    if (m_muted == muted) {
        return;
    }
    m_muted = muted;
    m_audio.setMuted(muted);
    QSettings().setValue(QStringLiteral("audio/muted"), muted);
    emit mutedChanged();
}

void AppController::setToolsVisible(bool visible) {
    if (m_toolsVisible == visible) {
        return;
    }
    m_toolsVisible = visible;
    QSettings().setValue(QStringLiteral("conversation/toolsVisible"), visible);
    emit toolsVisibleChanged();
}

void AppController::setTimestampsVisible(bool visible) {
    if (m_timestampsVisible == visible) {
        return;
    }
    m_timestampsVisible = visible;
    QSettings().setValue(QStringLiteral("conversation/timestampsVisible"), visible);
    emit timestampsVisibleChanged();
}

void AppController::setSharedFilesystem(bool shared) {
    if (m_sharedFilesystem == shared) {
        return;
    }
    m_sharedFilesystem = shared;
    QSettings().setValue(sharedFilesystemSettingsKey(m_baseUrl), shared);
    emit sharedFilesystemChanged();
}

void AppController::connectToServer(const QString& url, const QString& token) {
    setBaseUrl(url);
    m_bearerToken = token.trimmed();
    if (m_bearerToken.isEmpty()) {
        m_credentials.lookup(m_baseUrl);
    } else {
        reconnect();
    }
}

void AppController::pairDevice(const QString& url, const QString& code) {
    const QString trimmedCode = code.trimmed();
    if (trimmedCode.isEmpty()) {
        setErrorMessage(QStringLiteral("Enter the one-time pairing code"));
        return;
    }
    setBaseUrl(url);
    m_bearerToken.clear();
    m_sse.stop();
    m_api.setEndpoint(QUrl(m_baseUrl), {});
    setConnecting(true);
    setConnectionState(QStringLiteral("pairing"));
    setErrorMessage({});
    m_api.postJson(QStringLiteral("pairing"), QStringLiteral("/pairing/exchange"),
                   {{QStringLiteral("code"), trimmedCode},
                    {QStringLiteral("device_name"), QStringLiteral("Clarp desktop")}});
}

void AppController::forgetCredential() { m_credentials.remove(m_baseUrl); }

void AppController::reconnect() {
    const QUrl endpoint(m_baseUrl);
    if (!endpoint.isValid() || endpoint.scheme().isEmpty() || endpoint.host().isEmpty()) {
        setErrorMessage(QStringLiteral("Enter a valid Clarp server URL"));
        return;
    }
    m_sse.stop();
    resetTransientRequestState();
    clearAvatarCache();
    m_api.setEndpoint(endpoint, m_bearerToken);
    m_sse.setEndpoint(endpoint, m_bearerToken);
    m_audio.setEndpoint(endpoint, m_bearerToken);
    m_audio.setMuted(m_muted);
    setErrorMessage({});
    setConnecting(true);
    setConnectionState(QStringLiteral("connecting"));
    m_api.get(QStringLiteral("server-info"), QStringLiteral("/server-info"));
}

void AppController::resetTransientRequestState() {
    for (const QString& session : std::as_const(m_logRequestsInFlight)) {
        if (ConversationModel* model = m_conversations.value(session, nullptr)) {
            model->setLoading(false);
        }
    }
    m_logRequestsInFlight.clear();
    m_pendingLogMode.clear();

    for (auto it = m_deliveryTimers.begin(); it != m_deliveryTimers.end(); ++it) {
        it.value()->stop();
        it.value()->deleteLater();
        const QString session = m_deliverySessions.value(it.key());
        if (!session.isEmpty()) {
            ensureConversation(session)->markDeliveryFailed(it.key());
        }
    }
    m_deliveryTimers.clear();
    m_deliverySessions.clear();
    if (m_sending) {
        m_sending = false;
        emit sendingChanged();
    }

    for (const QVariantMap& pending : std::as_const(m_pendingUploads)) {
        const QString session = pending.value(QStringLiteral("session")).toString();
        const QString attachmentId = pending.value(QStringLiteral("id")).toString();
        QVariantList attachments = composerAttachments({}, session);
        for (QVariant& value : attachments) {
            QVariantMap attachment = value.toMap();
            if (attachment.value(QStringLiteral("id")).toString() == attachmentId) {
                attachment.insert(QStringLiteral("status"), QStringLiteral("failed"));
                value = attachment;
                break;
            }
        }
        storeComposerAttachments(session, attachments);
    }
    m_pendingUploads.clear();
    m_avatarRequests.clear();
    m_updateRequestsPending = 0;
    m_teamListLoading = false;
    m_teamMessagesLoading = false;
    m_turnQueueLoading = false;
    m_profileLoading = false;
    m_voicesLoading = false;
    m_orchestratorLoading = false;
    m_pastSessionsLoading = false;
    m_queueActionSessions.clear();
    emit updatesChanged();
    emit teamsChanged();
    emit turnQueueChanged();
    emit profileChanged();
    emit voicesChanged();
    emit orchestratorChanged();
    emit pastSessionsChanged();
}

void AppController::selectSession(const QString& session) {
    if (session.isEmpty()) {
        return;
    }
    const bool changed = m_selectedSession != session;
    m_selectedSession = session;
    m_agents.clearUnread(session);
    if (changed) {
        m_conversation = ensureConversation(session);
        m_panes.setActiveSession(session);
        emit selectedSessionChanged();
        emit conversationChanged();
        refreshSelectedProperties();
    }
    m_api.postJson(QStringLiteral("select:") + session, QStringLiteral("/select"),
                   {{QStringLiteral("session"), session}});
    requestTail(session);
    requestRecoverableClips(session);
}

void AppController::refreshConversation() { requestTail(m_selectedSession); }

void AppController::loadOlder() { loadOlderSession(m_selectedSession); }

void AppController::sendMessage(const QString& text, bool queueIfBusy) {
    sendMessageInternal(m_selectedSession, text, queueIfBusy, {}, {}, false);
}

void AppController::sendMessageTo(const QString& session, const QString& text, bool queueIfBusy) {
    sendMessageInternal(session, text, queueIfBusy, {}, {}, false);
}

void AppController::sendMessageInternal(const QString& targetSession, const QString& text,
                                        bool queueIfBusy, const QString& traceId,
                                        const QString& transcriptionId, bool handsFree) {
    const QString trimmed = text.trimmed();
    if (trimmed.isEmpty() || targetSession.isEmpty()) {
        return;
    }
    ConversationModel* targetConversation = ensureConversation(targetSession);
    const QString clientId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    targetConversation->addOptimistic(clientId, trimmed);
    m_audio.silence();
    m_sending = true;
    emit sendingChanged();

    QJsonObject body{
        {QStringLiteral("session"), targetSession},  {QStringLiteral("text"), trimmed},
        {QStringLiteral("client_msg_id"), clientId}, {QStringLiteral("synthesize_audio"), !m_muted},
        {QStringLiteral("hands_free"), handsFree},   {QStringLiteral("queue_if_busy"), queueIfBusy},
    };
    if (!traceId.isEmpty()) {
        body.insert(QStringLiteral("trace_id"), traceId);
    }
    if (!transcriptionId.isEmpty()) {
        body.insert(QStringLiteral("transcription_id"), transcriptionId);
    }
    m_api.postJson(QStringLiteral("send:") + clientId, QStringLiteral("/send"), body);

    auto* timer = new QTimer(this);
    timer->setSingleShot(true);
    timer->setInterval(DeliveryTimeoutMs);
    connect(timer, &QTimer::timeout, this, [this, clientId, targetConversation] {
        m_deliveryTimers.remove(clientId);
        m_deliverySessions.remove(clientId);
        targetConversation->markDeliveryFailed(clientId);
        if (m_deliveryTimers.isEmpty() && m_sending) {
            m_sending = false;
            emit sendingChanged();
        }
    });
    m_deliveryTimers.insert(clientId, timer);
    m_deliverySessions.insert(clientId, targetSession);
    timer->start();
}

void AppController::stopAgent() { stopSession(m_selectedSession); }

void AppController::stopSession(const QString& session) {
    if (session.isEmpty()) {
        return;
    }
    m_api.postJson(QStringLiteral("stop:") + session, QStringLiteral("/stop"),
                   {{QStringLiteral("session"), session}});
}

void AppController::refreshSession(const QString& session) { requestTail(session); }

void AppController::loadOlderSession(const QString& session) {
    ConversationModel* model = ensureConversation(session);
    if (session.isEmpty() || !model->hasMore() || model->rowCount() == 0) {
        return;
    }
    if (m_logRequestsInFlight.contains(session)) {
        return;
    }
    m_logRequestsInFlight.insert(session);
    const QModelIndex first = model->index(0, 0);
    const QString before = model->data(first, ConversationModel::MessageIdRole).toString();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("session"), session);
    query.addQueryItem(QStringLiteral("limit"), QStringLiteral("100"));
    query.addQueryItem(QStringLiteral("include_automated"), QStringLiteral("0"));
    query.addQueryItem(QStringLiteral("before"), before);
    model->setLoading(true);
    m_api.get(QStringLiteral("log-older:") + session, QStringLiteral("/log"), query);
}

QString AppController::agentName(const QString& session) const {
    const Agent* agent = m_agents.find(session);
    return agent == nullptr ? session : displayName(*agent);
}

QString AppController::agentState(const QString& session) const {
    const Agent* agent = m_agents.find(session);
    return agent == nullptr ? QString{} : agent->latestState;
}

int AppController::agentQueueCount(const QString& session) const {
    const Agent* agent = m_agents.find(session);
    return agent == nullptr ? 0 : agent->queuedTurnCount;
}

QVariantMap AppController::agentDetails(const QString& session) const {
    const Agent* agent = m_agents.find(session);
    if (agent == nullptr) {
        return {};
    }
    return {{QStringLiteral("agent_id"), agent->agentId},
            {QStringLiteral("session"), agent->session},
            {QStringLiteral("name"), displayName(*agent)},
            {QStringLiteral("backend"), agent->backend},
            {QStringLiteral("working_directory"), agent->workingDirectory},
            {QStringLiteral("model"), agent->model},
            {QStringLiteral("effort"), agent->effort},
            {QStringLiteral("state"), agent->latestState},
            {QStringLiteral("status_text"), agent->statusText},
            {QStringLiteral("context_tokens"), agent->contextTokens},
            {QStringLiteral("context_window"), agent->contextWindow},
            {QStringLiteral("queue_count"), agent->queuedTurnCount},
            {QStringLiteral("muted"), agent->muted},
            {QStringLiteral("heartbeat_enabled"), agent->heartbeatEnabled},
            {QStringLiteral("dreaming_enabled"), agent->dreamingEnabled},
            {QStringLiteral("schedules"), agent->schedules.toVariantList()},
            {QStringLiteral("mcp_servers"), agent->mcpServers.toVariantList()}};
}

QString AppController::agentBackend(const QString& session) const {
    const Agent* agent = m_agents.find(session);
    return agent == nullptr ? QString{} : agent->backend;
}

QString AppController::agentWorkingDirectory(const QString& session) const {
    const Agent* agent = m_agents.find(session);
    return agent == nullptr ? QString{} : agent->workingDirectory;
}

QString AppController::agentModel(const QString& session) const {
    const Agent* agent = m_agents.find(session);
    return agent == nullptr ? QString{} : agent->model;
}

QString AppController::agentEffort(const QString& session) const {
    const Agent* agent = m_agents.find(session);
    return agent == nullptr ? QString{} : agent->effort;
}

QString AppController::agentNameById(const QString& agentId) const {
    for (const QString& session : m_agents.sessions()) {
        if (const Agent* agent = m_agents.find(session); agent != nullptr && agent->agentId == agentId) {
            return displayName(*agent);
        }
    }
    return agentId;
}

QVariantList AppController::teamAgentChoices() const {
    QVariantList choices;
    for (const QString& session : m_agents.sessions()) {
        if (const Agent* agent = m_agents.find(session); agent != nullptr) {
            choices.append(QVariantMap{{QStringLiteral("id"), agent->agentId},
                                       {QStringLiteral("session"), agent->session},
                                       {QStringLiteral("name"), displayName(*agent)}});
        }
    }
    return choices;
}

QVariantList AppController::matchingAgents(const QString& query) const {
    const QString needle = query.trimmed();
    QVariantList matches;
    for (const QString& session : m_agents.sessions()) {
        const Agent* agent = m_agents.find(session);
        if (agent == nullptr) {
            continue;
        }
        const QString name = displayName(*agent);
        if (!needle.isEmpty() && !name.contains(needle, Qt::CaseInsensitive) &&
            !session.contains(needle, Qt::CaseInsensitive) &&
            !agent->workingDirectory.contains(needle, Qt::CaseInsensitive)) {
            continue;
        }
        matches.append(QVariantMap{
            {QStringLiteral("session"), session},
            {QStringLiteral("name"), name},
            {QStringLiteral("backend"), agent->backend},
            {QStringLiteral("state"), agent->latestState},
            {QStringLiteral("busy"), agent->busy},
            {QStringLiteral("unread"), agent->unread},
        });
    }
    return matches;
}

QVariantList AppController::modelsForBackend(const QString& backend) const {
    QVariantList result{QVariantMap{{QStringLiteral("id"), QString{}},
                                    {QStringLiteral("label"), QStringLiteral("Provider default")}}};
    const QVariantMap providers = m_modelCatalog.value(QStringLiteral("providers")).toMap();
    const QVariantList models =
        providers.value(backend).toMap().value(QStringLiteral("models")).toList();
    for (const QVariant& value : models) {
        const QVariantMap model = value.toMap();
        const QString id = model.value(QStringLiteral("id")).toString();
        if (!id.isEmpty()) {
            result.append(QVariantMap{
                {QStringLiteral("id"), id},
                {QStringLiteral("label"), model.value(QStringLiteral("label"), id).toString()},
            });
        }
    }
    return result;
}

QVariantList AppController::effortsForModel(const QString& backend, const QString& modelId) const {
    QStringList ids;
    const QVariantMap providers = m_modelCatalog.value(QStringLiteral("providers")).toMap();
    const QVariantMap provider = providers.value(backend).toMap();
    const QVariantList models = provider.value(QStringLiteral("models")).toList();
    for (const QVariant& value : models) {
        const QVariantMap model = value.toMap();
        if (model.value(QStringLiteral("id")).toString() == modelId) {
            for (const QVariant& effort :
                 model.value(QStringLiteral("supported_efforts")).toList()) {
                ids.append(effort.toString());
            }
            break;
        }
    }
    if (ids.isEmpty()) {
        for (const QVariant& effort :
             provider.value(QStringLiteral("supported_efforts")).toList()) {
            ids.append(effort.toString());
        }
    }
    QVariantList efforts{
        QVariantMap{{QStringLiteral("id"), QString{}},
                    {QStringLiteral("label"), QStringLiteral("Provider default")}},
    };
    for (const QString& id : std::as_const(ids)) {
        QString label = id;
        if (!label.isEmpty()) {
            label.front() = label.front().toUpper();
        }
        efforts.append(QVariantMap{{QStringLiteral("id"), id}, {QStringLiteral("label"), label}});
    }
    return efforts;
}

bool AppController::backendSupportsResume(const QString& backend) const {
    const QVariantMap providers = m_modelCatalog.value(QStringLiteral("providers")).toMap();
    return providers.value(backend).toMap().value(QStringLiteral("supports_resume")).toBool();
}

bool AppController::backendSupportsFork(const QString& backend) const {
    const QVariantMap providers = m_modelCatalog.value(QStringLiteral("providers")).toMap();
    return providers.value(backend).toMap().value(QStringLiteral("supports_fork")).toBool();
}

void AppController::loadPastSessions(const QString& workingDirectory, const QString& backend,
                                     bool allProjects) {
    if (workingDirectory.trimmed().isEmpty() || backend.isEmpty()) {
        return;
    }
    m_pastSessionsLoading = true;
    m_pastSessions.clear();
    emit pastSessionsChanged();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("cwd"), workingDirectory.trimmed());
    query.addQueryItem(QStringLiteral("backend"), backend);
    if (allProjects) {
        query.addQueryItem(QStringLiteral("scope"), QStringLiteral("all"));
    }
    m_api.get(QStringLiteral("past-sessions"), QStringLiteral("/past-sessions"), query);
}

void AppController::loadDirectorySuggestions(const QString& path) {
    const QString trimmed = path.trimmed();
    if (trimmed.isEmpty()) {
        if (!m_directorySuggestions.isEmpty()) {
            m_directorySuggestions.clear();
            emit pathsChanged();
        }
        return;
    }
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("path"), trimmed);
    m_api.get(QStringLiteral("directory-suggestions"), QStringLiteral("/dirs"), query);
}

void AppController::loadFavoritePaths() {
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("limit"), QStringLiteral("5"));
    m_api.get(QStringLiteral("favorite-paths"), QStringLiteral("/favorite-paths"), query);
}

void AppController::refreshAgents() { requestSnapshot(); }

void AppController::createAgent(const QString& name, const QString& workingDirectory,
                                const QString& backend, const QString& model, const QString& effort,
                                const QString& replaceSession, const QString& mode,
                                const QString& pastSessionId) {
    const QString trimmedName = name.trimmed();
    const QString trimmedDirectory = workingDirectory.trimmed();
    if (trimmedName.isEmpty() || trimmedDirectory.isEmpty() || backend.trimmed().isEmpty()) {
        setErrorMessage(QStringLiteral("Name, workspace, and backend are required"));
        return;
    }
    QJsonObject body{
        {QStringLiteral("name"), trimmedName},
        {QStringLiteral("session"), trimmedName.toLower()},
        {QStringLiteral("cwd"), trimmedDirectory},
        {QStringLiteral("backend"), backend.trimmed()},
        {QStringLiteral("synthesize_audio"), !m_muted},
    };
    if (!model.trimmed().isEmpty()) {
        body.insert(QStringLiteral("model"), model.trimmed());
    }
    if (!effort.trimmed().isEmpty()) {
        body.insert(QStringLiteral("effort"), effort.trimmed());
    }
    const bool defaultsChanged =
        m_lastWorkingDirectory != trimmedDirectory || m_lastBackend != backend.trimmed();
    m_lastWorkingDirectory = trimmedDirectory;
    m_lastBackend = backend.trimmed();
    QSettings settings;
    settings.setValue(QStringLiteral("launch/workingDirectory"), m_lastWorkingDirectory);
    settings.setValue(QStringLiteral("launch/backend"), m_lastBackend);
    if (defaultsChanged) {
        emit launchDefaultsChanged();
    }
    if (!replaceSession.isEmpty()) {
        body.insert(QStringLiteral("replace_sid"), replaceSession);
    }
    if (mode == QStringLiteral("resume") && !pastSessionId.isEmpty()) {
        body.insert(QStringLiteral("resume_session_id"), pastSessionId);
    } else if (mode == QStringLiteral("fork") && !pastSessionId.isEmpty()) {
        body.insert(QStringLiteral("fork_session_id"), pastSessionId);
    }
    m_api.postJson(QStringLiteral("agent-create:") + replaceSession, QStringLiteral("/agents"),
                   body);
}

void AppController::releaseAgent(const QString& session) {
    if (session.isEmpty()) {
        return;
    }
    const Agent* agent = m_agents.find(session);
    if (session.compare(QStringLiteral("mike"), Qt::CaseInsensitive) == 0 ||
        (agent != nullptr && displayName(*agent).compare(QStringLiteral("Mike"),
                                                        Qt::CaseInsensitive) == 0)) {
        setErrorMessage(QStringLiteral("Mike is the protected default chat and cannot be released"));
        return;
    }
    m_api.deleteResource(QStringLiteral("agent-release:") + session,
                         QStringLiteral("/agents/") +
                             QString::fromUtf8(QUrl::toPercentEncoding(session)));
}

void AppController::setAgentHeartbeat(const QString& session, bool enabled) {
    m_api.postJson(
        QStringLiteral("agent-setting:") + session, QStringLiteral("/agent-heartbeat"),
        {{QStringLiteral("session"), session}, {QStringLiteral("heartbeat_enabled"), enabled}});
}

void AppController::setAgentDreaming(const QString& session, bool enabled) {
    m_api.postJson(
        QStringLiteral("agent-setting:") + session, QStringLiteral("/agent-dreaming"),
        {{QStringLiteral("session"), session}, {QStringLiteral("dreaming_enabled"), enabled}});
}

void AppController::setAgentPushMuted(const QString& session, bool muted) {
    m_api.postJson(QStringLiteral("agent-setting:") + session, QStringLiteral("/agent-mute"),
                   {{QStringLiteral("session"), session}, {QStringLiteral("muted"), muted}});
}

void AppController::archiveAgent(const QString& session) { setAgentArchived(session, true); }

void AppController::setAgentArchived(const QString& session, bool archived) {
    m_api.postJson(QStringLiteral("agent-setting:") + session, QStringLiteral("/agent-archive"),
                   {{QStringLiteral("session"), session}, {QStringLiteral("archived"), archived}});
}

void AppController::setScheduleEnabled(const QString& scheduleId, bool enabled) {
    if (scheduleId.trimmed().isEmpty()) {
        return;
    }
    m_api.postJson(QStringLiteral("schedule-toggle:"), QStringLiteral("/agent-schedules/toggle"),
                   {{QStringLiteral("schedule_id"), scheduleId.trimmed()},
                    {QStringLiteral("enabled"), enabled}});
}

void AppController::loadVoices(const QString& session) {
    if (session.isEmpty()) {
        return;
    }
    m_voiceSession = session;
    m_voiceBio.clear();
    m_voicesLoading = true;
    emit voicesChanged();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("for"), session);
    m_api.get(QStringLiteral("voices:") + session, QStringLiteral("/voices"), query);
}

void AppController::previewVoice(const QString& session, const QString& name,
                                 const QString& voiceId) {
    if (session.isEmpty() || voiceId.isEmpty()) {
        return;
    }
    m_api.postJson(QStringLiteral("voice-preview:") + session, QStringLiteral("/preview"),
                   {{QStringLiteral("voice_id"), voiceId},
                    {QStringLiteral("session"), session},
                    {QStringLiteral("text"),
                     QStringLiteral("Hi, I'm %1.").arg(name.isEmpty() ? session : name)}});
}

void AppController::chooseVoice(const QString& session, const QString& voiceId) {
    if (session.isEmpty() || voiceId.isEmpty()) {
        return;
    }
    m_api.postJson(QStringLiteral("voice-select:") + session, QStringLiteral("/agent-voice"),
                   {{QStringLiteral("session"), session}, {QStringLiteral("voice_id"), voiceId}});
}

void AppController::loadOrchestrator() {
    m_orchestratorLoading = true;
    emit orchestratorChanged();
    m_api.get(QStringLiteral("orchestrator-load"), QStringLiteral("/orchestrator/settings"));
}

void AppController::saveOrchestrator(bool enabled, bool fallbackOnly, double confidence,
                                     const QString& provider, const QString& model,
                                     const QString& effort, int timeoutMs) {
    m_orchestratorLoading = true;
    emit orchestratorChanged();
    m_api.postJson(
        QStringLiteral("orchestrator-save"), QStringLiteral("/orchestrator/settings"),
        {{QStringLiteral("enabled"), enabled},
         {QStringLiteral("fallback_only"), fallbackOnly},
         {QStringLiteral("confidence_threshold"), std::clamp(confidence, 0.5, 0.99)},
         {QStringLiteral("provider"), provider.isEmpty() ? QStringLiteral("openai") : provider},
         {QStringLiteral("model"), model.trimmed()},
         {QStringLiteral("effort"), effort.trimmed()},
         {QStringLiteral("timeout_ms"), std::clamp(timeoutMs, 250, 60'000)}});
}

void AppController::clearError() { setErrorMessage({}); }

QUrl AppController::resourceUrl(const QString& path) const { return m_api.resolve(path); }

QString AppController::paneDraft(const QString& paneId, const QString& session) const {
    Q_UNUSED(paneId)
    if (session.isEmpty()) {
        return {};
    }
    return QSettings().value(draftSettingsKey(m_baseUrl, session)).toString();
}

void AppController::setPaneDraft(const QString& paneId, const QString& session,
                                 const QString& text) {
    if (paneId.isEmpty() || session.isEmpty()) {
        return;
    }
    const QString key = draftSettingsKey(m_baseUrl, session);
    QSettings settings;
    if (settings.value(key).toString() == text) {
        return;
    }
    if (text.isEmpty()) {
        settings.remove(key);
    } else {
        settings.setValue(key, text);
    }
    emit draftChanged(session, text, paneId);
}

QVariantList AppController::composerAttachments(const QString& paneId,
                                                const QString& session) const {
    Q_UNUSED(paneId)
    if (session.isEmpty()) {
        return {};
    }
    const QByteArray encoded =
        QSettings().value(draftAttachmentsSettingsKey(m_baseUrl, session)).toByteArray();
    const QJsonDocument document = QJsonDocument::fromJson(encoded);
    return document.isArray() ? document.array().toVariantList() : QVariantList{};
}

bool AppController::composerCanSend(const QString& paneId, const QString& session) const {
    const QVariantList attachments = composerAttachments(paneId, session);
    return std::ranges::all_of(attachments, [](const QVariant& value) {
        const QVariantMap attachment = value.toMap();
        return !attachment.value(QStringLiteral("path")).toString().isEmpty() &&
               attachment.value(QStringLiteral("status"), QStringLiteral("ready")).toString() ==
                   QStringLiteral("ready");
    });
}

void AppController::storeComposerAttachments(const QString& session,
                                             const QVariantList& attachments) {
    if (session.isEmpty()) {
        return;
    }
    QSettings settings;
    const QString key = draftAttachmentsSettingsKey(m_baseUrl, session);
    if (attachments.isEmpty()) {
        settings.remove(key);
    } else {
        settings.setValue(
            key,
            QJsonDocument(QJsonArray::fromVariantList(attachments)).toJson(QJsonDocument::Compact));
    }
    ++m_composerRevision;
    emit composerRevisionChanged();
}

void AppController::attachLocalFile(const QString& paneId, const QString& session,
                                    const QUrl& fileUrl) {
    const QString path = fileUrl.toLocalFile();
    const QFileInfo info(path);
    if (paneId.isEmpty() || session.isEmpty() || path.isEmpty() || !info.exists() ||
        !info.isFile() || info.size() <= 0 || info.size() > MaxComposerUploadBytes) {
        setErrorMessage(QStringLiteral("Choose a readable file no larger than 50 MB"));
        return;
    }
    const QString attachmentId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    if (m_sharedFilesystem) {
        QVariantList attachments = composerAttachments(paneId, session);
        attachments.append(QVariantMap{{QStringLiteral("id"), attachmentId},
                                       {QStringLiteral("path"), info.canonicalFilePath()},
                                       {QStringLiteral("name"), info.fileName()},
                                       {QStringLiteral("content_type"),
                                        QStringLiteral("application/octet-stream")},
                                       {QStringLiteral("local"), true},
                                       {QStringLiteral("status"), QStringLiteral("ready")}});
        storeComposerAttachments(session, attachments);
        return;
    }

    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        setErrorMessage(QStringLiteral("Could not read %1").arg(info.fileName()));
        return;
    }
    const QByteArray bytes = file.readAll();
    const QString tag = QStringLiteral("composer-upload:%1").arg(attachmentId);
    const QVariantMap pending{{QStringLiteral("pane_id"), paneId},
                              {QStringLiteral("session"), session},
                              {QStringLiteral("id"), attachmentId},
                              {QStringLiteral("name"), info.fileName()},
                              {QStringLiteral("content_type"),
                               QStringLiteral("application/octet-stream")},
                              {QStringLiteral("local_source"), info.canonicalFilePath()},
                              {QStringLiteral("status"), QStringLiteral("uploading")}};
    m_pendingUploads.insert(tag, pending);
    QVariantList attachments = composerAttachments(paneId, session);
    QVariantMap visiblePending = pending;
    visiblePending.remove(QStringLiteral("pane_id"));
    attachments.append(visiblePending);
    storeComposerAttachments(session, attachments);
    m_api.postBytes(tag, QStringLiteral("/upload"), bytes,
                    QByteArrayLiteral("application/octet-stream"),
                    {{QByteArrayLiteral("X-File-Name"),
                      QUrl::toPercentEncoding(info.fileName())},
                     {QByteArrayLiteral("X-Session"), session.toUtf8()},
                     {QByteArrayLiteral("X-Upload-ID"), attachmentId.toUtf8()}});
}

void AppController::removeComposerAttachment(const QString& paneId, const QString& session,
                                             const QString& attachmentId) {
    for (auto it = m_pendingUploads.begin(); it != m_pendingUploads.end();) {
        const QVariantMap pending = it.value();
        if (pending.value(QStringLiteral("session")).toString() == session &&
            pending.value(QStringLiteral("id")).toString() == attachmentId) {
            it = m_pendingUploads.erase(it);
        } else {
            ++it;
        }
    }
    QVariantList attachments = composerAttachments(paneId, session);
    const auto oldSize = attachments.size();
    attachments.erase(std::remove_if(attachments.begin(), attachments.end(),
                                     [&attachmentId](const QVariant& value) {
                                         return value.toMap().value(QStringLiteral("id")).toString() ==
                                                attachmentId;
                                     }),
                      attachments.end());
    if (attachments.size() == oldSize) {
        return;
    }
    storeComposerAttachments(session, attachments);
}

bool AppController::sendComposerMessage(const QString& paneId, const QString& session,
                                        const QString& text, bool queueIfBusy) {
    QString outbound = text.trimmed();
    const QVariantList attachments = composerAttachments(paneId, session);
    for (const QVariant& value : attachments) {
        const QVariantMap attachment = value.toMap();
        const QString path = attachment.value(QStringLiteral("path")).toString();
        if (path.isEmpty() ||
            attachment.value(QStringLiteral("status"), QStringLiteral("ready")).toString() !=
                QStringLiteral("ready")) {
            setErrorMessage(QStringLiteral("Wait for attachments to finish uploading or remove them"));
            return false;
        }
        if (!path.isEmpty()) {
            if (!outbound.isEmpty() && !outbound.endsWith(u' ')) {
                outbound.append(u' ');
            }
            outbound.append(path);
        }
    }
    if (session.isEmpty() || outbound.isEmpty()) {
        return false;
    }
    setPaneDraft(paneId, session, {});
    QSettings().remove(draftAttachmentsSettingsKey(m_baseUrl, session));
    ++m_composerRevision;
    emit composerRevisionChanged();
    sendMessageTo(session, outbound, queueIfBusy);
    return true;
}

void AppController::requestComposerFocus(const QString& paneId) {
    m_composerFocusPane = paneId;
    emit composerFocusPaneChanged();
}

void AppController::loadUpdates() {
    const quint64 generation = ++m_updatesGeneration;
    m_updatesError.clear();
    m_updateRequestsPending = 3;
    emit updatesChanged();
    m_api.get(QStringLiteral("updates:%1:attention").arg(generation), QStringLiteral("/attention"));
    m_api.get(QStringLiteral("updates:%1:jobs").arg(generation),
              QStringLiteral("/background-jobs"));
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("limit"), QStringLiteral("50"));
    query.addQueryItem(QStringLiteral("order"), QStringLiteral("updated"));
    m_api.get(QStringLiteral("updates:%1:artifacts").arg(generation), QStringLiteral("/artifacts"),
              query);
}

void AppController::resolveDecision(const QString& decisionId, const QString& choice,
                                    int revision) {
    QString protocolChoice = choice;
    if (protocolChoice == QStringLiteral("yes")) {
        protocolChoice = QStringLiteral("accepted");
    } else if (protocolChoice == QStringLiteral("no")) {
        protocolChoice = QStringLiteral("rejected");
    }
    if (decisionId.isEmpty() ||
        (protocolChoice != QStringLiteral("accepted") &&
         protocolChoice != QStringLiteral("rejected"))) {
        return;
    }
    const QString path = QStringLiteral("/decisions/%1/resolve")
                             .arg(QString::fromUtf8(QUrl::toPercentEncoding(decisionId)));
    m_api.postJson(QStringLiteral("update-action:decision"), path,
                   {{QStringLiteral("choice"), protocolChoice},
                    {QStringLiteral("expected_revision"), revision}});
}

void AppController::cancelBackgroundJob(const QString& jobId) {
    if (jobId.isEmpty()) {
        return;
    }
    m_api.deleteResource(QStringLiteral("update-action:job-cancel"),
                         QStringLiteral("/background-jobs/%1")
                             .arg(QString::fromUtf8(QUrl::toPercentEncoding(jobId))));
}

void AppController::finishUpdateRequest(quint64 generation) {
    if (generation != m_updatesGeneration) {
        return;
    }
    m_updateRequestsPending = std::max(0, m_updateRequestsPending - 1);
    emit updatesChanged();
}

void AppController::loadTeams() {
    const quint64 generation = ++m_teamListGeneration;
    m_teamListLoading = true;
    m_teamsError.clear();
    emit teamsChanged();
    m_api.get(QStringLiteral("team-list:%1").arg(generation), QStringLiteral("/teams"));
}

void AppController::selectTeam(const QString& teamId) {
    if (teamId.isEmpty()) {
        return;
    }
    const bool changedTeam = m_selectedTeamId != teamId;
    m_selectedTeamId = teamId;
    if (changedTeam) {
        m_teamMessages.clear();
    }
    const quint64 generation = ++m_teamMessagesGeneration;
    m_teamMessagesLoading = changedTeam || m_teamMessages.isEmpty();
    emit teamsChanged();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("limit"), QStringLiteral("100"));
    const QString path = QStringLiteral("/teams/%1/messages")
                             .arg(QString::fromUtf8(QUrl::toPercentEncoding(teamId)));
    m_api.get(QStringLiteral("team-messages:%1:%2").arg(generation).arg(teamId), path, query);
}

void AppController::createTeam(const QString& name, const QString& color) {
    const QString trimmed = name.trimmed();
    if (trimmed.isEmpty()) {
        return;
    }
    m_api.postJson(QStringLiteral("team-action:create"), QStringLiteral("/teams"),
                   {{QStringLiteral("name"), trimmed}, {QStringLiteral("color"), color}});
}

void AppController::updateTeam(const QString& teamId, const QString& name,
                               const QString& color, const QString& leaderAgentId) {
    const QString trimmed = name.trimmed();
    if (teamId.isEmpty() || trimmed.isEmpty()) {
        return;
    }
    if (!leaderAgentId.isEmpty()) {
        for (const QVariant& value : std::as_const(m_teams)) {
            const QVariantMap team = value.toMap();
            if (team.value(QStringLiteral("team_id")).toString() != teamId) {
                continue;
            }
            const QVariantList members =
                team.value(QStringLiteral("member_agent_ids")).toList();
            if (!members.contains(leaderAgentId)) {
                m_teamsError = QStringLiteral("Team leader must already be a member");
                emit teamsChanged();
                return;
            }
            break;
        }
    }
    const QString path = QStringLiteral("/teams/%1")
                             .arg(QString::fromUtf8(QUrl::toPercentEncoding(teamId)));
    m_api.postJson(QStringLiteral("team-action:update"), path,
                   {{QStringLiteral("name"), trimmed},
                    {QStringLiteral("color"), color.trimmed()},
                    {QStringLiteral("leader"), leaderAgentId}});
}

void AppController::addTeamMember(const QString& teamId, const QString& agentId) {
    if (teamId.isEmpty() || agentId.isEmpty()) {
        return;
    }
    const QString path = QStringLiteral("/teams/%1/members")
                             .arg(QString::fromUtf8(QUrl::toPercentEncoding(teamId)));
    m_api.postJson(QStringLiteral("team-action:add-member"), path,
                   {{QStringLiteral("agent_id"), agentId}});
}

void AppController::removeTeamMember(const QString& teamId, const QString& agentId) {
    if (teamId.isEmpty() || agentId.isEmpty()) {
        return;
    }
    const QString path = QStringLiteral("/teams/%1/members/%2")
                             .arg(QString::fromUtf8(QUrl::toPercentEncoding(teamId)),
                                  QString::fromUtf8(QUrl::toPercentEncoding(agentId)));
    m_api.deleteResource(QStringLiteral("team-action:remove-member"), path);
}

void AppController::deleteTeam(const QString& teamId) {
    if (teamId.isEmpty()) {
        return;
    }
    if (m_selectedTeamId == teamId) {
        m_selectedTeamId.clear();
        m_teamMessages.clear();
        emit teamsChanged();
    }
    m_api.deleteResource(
        QStringLiteral("team-action:delete"),
        QStringLiteral("/teams/%1").arg(QString::fromUtf8(QUrl::toPercentEncoding(teamId))));
}

void AppController::loadTurnQueue(const QString& session) {
    if (session.isEmpty()) {
        return;
    }
    if (m_turnQueueSession != session) {
        m_turnQueueItems.clear();
        m_turnQueuePaused = false;
    }
    m_turnQueueSession = session;
    m_turnQueueLoading = true;
    m_turnQueueError.clear();
    const quint64 generation = ++m_turnQueueGeneration;
    emit turnQueueChanged();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("session"), session);
    m_api.get(QStringLiteral("turn-queue:%1:%2").arg(generation).arg(session),
              QStringLiteral("/turn-queue"), query);
}

void AppController::updateQueuedTurn(const QString& queueId, const QString& text) {
    const QString trimmed = text.trimmed();
    if (queueId.isEmpty() || trimmed.isEmpty() || m_turnQueueSession.isEmpty()) {
        return;
    }
    const QString tag = QStringLiteral("queue-action:%1").arg(
        QUuid::createUuid().toString(QUuid::WithoutBraces));
    m_queueActionSessions.insert(tag, m_turnQueueSession);
    m_api.putJson(tag,
                  QStringLiteral("/turn-queue/%1")
                      .arg(QString::fromUtf8(QUrl::toPercentEncoding(queueId))),
                  {{QStringLiteral("text"), trimmed}});
}

void AppController::deleteQueuedTurn(const QString& queueId) {
    if (queueId.isEmpty() || m_turnQueueSession.isEmpty()) {
        return;
    }
    const QString tag = QStringLiteral("queue-action:%1").arg(
        QUuid::createUuid().toString(QUuid::WithoutBraces));
    m_queueActionSessions.insert(tag, m_turnQueueSession);
    m_api.deleteResource(tag, QStringLiteral("/turn-queue/%1")
                                  .arg(QString::fromUtf8(QUrl::toPercentEncoding(queueId))));
}

void AppController::sendQueuedTurn(const QString& queueId) {
    if (queueId.isEmpty() || m_turnQueueSession.isEmpty()) {
        return;
    }
    const QString tag = QStringLiteral("queue-action:%1").arg(
        QUuid::createUuid().toString(QUuid::WithoutBraces));
    m_queueActionSessions.insert(tag, m_turnQueueSession);
    m_api.postJson(tag,
                   QStringLiteral("/turn-queue/%1/send")
                       .arg(QString::fromUtf8(QUrl::toPercentEncoding(queueId))),
                   {});
}

void AppController::openAgentFiles(const QString& session) {
    const QString path = agentWorkingDirectory(session);
    const QFileInfo info(path);
    if (path.isEmpty() || !info.exists() || !info.isDir() ||
        !QDesktopServices::openUrl(QUrl::fromLocalFile(info.canonicalFilePath()))) {
        setErrorMessage(QStringLiteral("The agent directory is not available on this desktop"));
    }
}

void AppController::openAgentTerminal(const QString& session) {
    const QString path = agentWorkingDirectory(session);
    const QFileInfo info(path);
    if (path.isEmpty() || !info.exists() || !info.isDir()) {
        setErrorMessage(QStringLiteral("The agent directory is not available on this desktop"));
        return;
    }
    const QString directory = info.canonicalFilePath();
    if (!QProcess::startDetached(QStringLiteral("xdg-terminal-exec"), {}, directory) &&
        !QProcess::startDetached(QStringLiteral("x-terminal-emulator"), {}, directory)) {
        setErrorMessage(QStringLiteral("No desktop terminal launcher was found"));
    }
}

void AppController::loadAgentProfile(const QString& session) {
    if (session.isEmpty()) {
        return;
    }
    m_profileSession = session;
    m_profileTaskPlan.clear();
    m_profileError.clear();
    m_profileLoading = true;
    const quint64 generation = ++m_profileGeneration;
    emit profileChanged();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("session"), session);
    m_api.get(QStringLiteral("profile:%1:%2").arg(generation).arg(session),
              QStringLiteral("/task-plan"), query);
}

void AppController::setAgentLlm(const QString& session, const QString& model,
                                const QString& effort) {
    if (session.isEmpty()) {
        return;
    }
    m_api.postJson(QStringLiteral("agent-setting:%1").arg(session),
                   QStringLiteral("/agent-llm"),
                   {{QStringLiteral("session"), session},
                    {QStringLiteral("model"), model},
                    {QStringLiteral("effort"), effort}});
}

void AppController::compactSession(const QString& session) {
    if (session.isEmpty()) {
        return;
    }
    m_api.postJson(QStringLiteral("agent-setting:%1").arg(session),
                   QStringLiteral("/compact"), {{QStringLiteral("session"), session}});
}

void AppController::setAgentMcp(const QString& session, const QVariantList& servers) {
    if (session.isEmpty()) {
        return;
    }
    m_api.postJson(QStringLiteral("agent-setting:%1").arg(session),
                   QStringLiteral("/agent-mcp"),
                   {{QStringLiteral("session"), session},
                    {QStringLiteral("mcp_servers"), QJsonArray::fromVariantList(servers)}});
}

void AppController::requestSnapshot() {
    m_api.get(QStringLiteral("snapshot"), QStringLiteral("/agents/snapshot"));
}

void AppController::clearAvatarCache() {
    m_avatarSources.clear();
    m_avatarUrls.clear();
    m_avatarRequests.clear();
    m_avatarFailures.clear();
    ++m_avatarRevision;
    emit avatarRevisionChanged();
}

void AppController::requestAvatars() {
    const QStringList currentSessions = m_agents.sessions();
    const QSet<QString> sessions(currentSessions.cbegin(), currentSessions.cend());
    for (auto it = m_avatarUrls.begin(); it != m_avatarUrls.end();) {
        if (!sessions.contains(it.key())) {
            m_avatarSources.remove(it.key());
            it = m_avatarUrls.erase(it);
        } else {
            ++it;
        }
    }

    for (const QString& session : sessions) {
        const Agent* agent = m_agents.find(session);
        const QString url = agent == nullptr ? QString{} : avatarUrlForAgent(*agent);
        if (url.isEmpty()) {
            m_avatarUrls.remove(session);
            m_avatarSources.remove(session);
            m_avatarFailures.remove(session);
            continue;
        }
        const bool requestPending = std::ranges::any_of(
            m_avatarRequests, [&session, &url](const QPair<QString, QString>& request) {
                return request.first == session && request.second == url;
            });
        if (m_avatarUrls.value(session) == url &&
            (m_avatarSources.contains(session) || requestPending ||
             m_avatarFailures.value(session) == url)) {
            continue;
        }
        m_avatarUrls.insert(session, url);
        m_avatarSources.remove(session);
        const QString tag = QStringLiteral("avatar:%1").arg(++m_nextAvatarRequest);
        m_avatarRequests.insert(tag, {session, url});
        m_api.getBytes(tag, url);
    }
    ++m_avatarRevision;
    emit avatarRevisionChanged();
}

void AppController::requestRecoverableClips(const QString& session) {
    if (session.isEmpty()) {
        return;
    }
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("session"), session);
    m_api.get(QStringLiteral("recoverable:") + session, QStringLiteral("/clips/recoverable"),
              query);
}

void AppController::requestTail(const QString& requestedSession, bool replace) {
    const QString session = requestedSession.isEmpty() ? m_selectedSession : requestedSession;
    if (session.isEmpty()) {
        return;
    }
    const QString mode = replace ? QStringLiteral("replace") : QStringLiteral("tail");
    if (!beginLogRequest(session, mode)) {
        return;
    }
    ConversationModel* model = ensureConversation(session);
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("session"), session);
    query.addQueryItem(QStringLiteral("limit"), QStringLiteral("100"));
    query.addQueryItem(QStringLiteral("include_automated"), QStringLiteral("0"));
    model->setLoading(true);
    m_api.get((replace ? QStringLiteral("log-replace:") : QStringLiteral("log-tail:")) + session,
              QStringLiteral("/log"), query);
}

void AppController::requestDelta(const QString& requestedSession) {
    const QString session = requestedSession.isEmpty() ? m_selectedSession : requestedSession;
    if (session.isEmpty()) {
        return;
    }
    if (!beginLogRequest(session, QStringLiteral("delta"))) {
        return;
    }
    ConversationModel* model = ensureConversation(session);
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("session"), session);
    query.addQueryItem(QStringLiteral("limit"), QStringLiteral("100"));
    query.addQueryItem(QStringLiteral("include_automated"), QStringLiteral("0"));
    query.addQueryItem(QStringLiteral("after_revision"), QString::number(model->latestRevision()));
    m_api.get(QStringLiteral("log-delta:") + session, QStringLiteral("/log"), query);
}

bool AppController::beginLogRequest(const QString& session, const QString& mode) {
    if (!m_logRequestsInFlight.contains(session)) {
        m_logRequestsInFlight.insert(session);
        return true;
    }
    const QString queued = m_pendingLogMode.value(session);
    if (queued.isEmpty() || mode == QStringLiteral("replace") ||
        (mode == QStringLiteral("tail") && queued != QStringLiteral("replace"))) {
        m_pendingLogMode.insert(session, mode);
    }
    return false;
}

void AppController::continuePendingLogRequest(const QString& session) {
    if (m_logRequestsInFlight.contains(session)) {
        return;
    }
    const QString mode = m_pendingLogMode.take(session);
    if (mode == QStringLiteral("tail")) {
        requestTail(session);
    } else if (mode == QStringLiteral("replace")) {
        requestTail(session, true);
    } else if (mode == QStringLiteral("delta")) {
        requestDelta(session);
    }
}

void AppController::setConnecting(bool connecting) {
    if (m_connecting == connecting) {
        return;
    }
    m_connecting = connecting;
    emit connectingChanged();
}

void AppController::setConnectionState(const QString& state) {
    if (m_connectionState == state) {
        return;
    }
    m_connectionState = state;
    emit connectionStateChanged();
}

void AppController::setErrorMessage(const QString& message) {
    if (m_errorMessage == message) {
        return;
    }
    m_errorMessage = message;
    emit errorMessageChanged();
}

void AppController::handleJson(const QString& tag, const QJsonObject& object) {
    if (tag.startsWith(QStringLiteral("composer-upload:"))) {
        const QVariantMap pending = m_pendingUploads.take(tag);
        if (pending.isEmpty()) {
            return;
        }
        const QString paneId = pending.value(QStringLiteral("pane_id")).toString();
        const QString session = pending.value(QStringLiteral("session")).toString();
        const QString serverPath = object.value(QStringLiteral("path")).toString();
        if (serverPath.isEmpty()) {
            QVariantList attachments = composerAttachments(paneId, session);
            for (QVariant& value : attachments) {
                QVariantMap attachment = value.toMap();
                if (attachment.value(QStringLiteral("id")).toString() ==
                    pending.value(QStringLiteral("id")).toString()) {
                    attachment.insert(QStringLiteral("status"), QStringLiteral("failed"));
                    value = attachment;
                    break;
                }
            }
            storeComposerAttachments(session, attachments);
            setErrorMessage(QStringLiteral("Upload completed without a file path"));
            return;
        }
        QVariantList attachments = composerAttachments(paneId, session);
        QVariantMap attachment = pending;
        attachment.remove(QStringLiteral("pane_id"));
        attachment.remove(QStringLiteral("local_source"));
        attachment.insert(QStringLiteral("path"), serverPath);
        attachment.insert(QStringLiteral("status"), QStringLiteral("ready"));
        const QString serverName = object.value(QStringLiteral("name")).toString();
        if (!serverName.isEmpty()) {
            attachment.insert(QStringLiteral("name"), serverName);
        }
        bool replaced = false;
        for (QVariant& value : attachments) {
            if (value.toMap().value(QStringLiteral("id")).toString() ==
                attachment.value(QStringLiteral("id")).toString()) {
                value = attachment;
                replaced = true;
                break;
            }
        }
        if (!replaced) {
            return;
        }
        storeComposerAttachments(session, attachments);
        return;
    }
    if (tag.startsWith(QStringLiteral("turn-queue:"))) {
        const quint64 generation = tag.section(u':', 1, 1).toULongLong();
        const QString requestedSession = tag.section(u':', 2);
        if (generation != m_turnQueueGeneration || requestedSession != m_turnQueueSession) {
            return;
        }
        m_turnQueueItems = object.value(QStringLiteral("items")).toArray().toVariantList();
        m_turnQueuePaused = object.value(QStringLiteral("paused")).toBool();
        m_turnQueueLoading = false;
        m_turnQueueError.clear();
        emit turnQueueChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("profile:"))) {
        const quint64 generation = tag.section(u':', 1, 1).toULongLong();
        const QString requestedSession = tag.section(u':', 2);
        if (generation != m_profileGeneration || requestedSession != m_profileSession) {
            return;
        }
        m_profileTaskPlan = object.value(QStringLiteral("plan")).toObject().toVariantMap();
        m_profileLoading = false;
        m_profileError.clear();
        emit profileChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("queue-action:"))) {
        const QString session = m_queueActionSessions.take(tag);
        if (!session.isEmpty() && session == m_turnQueueSession) {
            loadTurnQueue(session);
        }
        return;
    }
    if (tag == QStringLiteral("server-info")) {
        m_serverName = object.value(QStringLiteral("name")).toString(QStringLiteral("Clarp"));
        m_serverVersion = object.value(QStringLiteral("clarp_version")).toString();
        emit serverInfoChanged();
        if (m_bearerToken.startsWith(QStringLiteral("cld_"))) {
            m_credentials.store(m_baseUrl, m_bearerToken);
        }
        setConnecting(false);
        requestSnapshot();
        m_api.get(QStringLiteral("model-catalog"), QStringLiteral("/agent-model-options"));
        m_sse.start();
        return;
    }
    if (tag == QStringLiteral("pairing")) {
        const QJsonObject device = object.value(QStringLiteral("device")).toObject();
        const QString token = device.value(QStringLiteral("token")).toString();
        if (token.isEmpty()) {
            setConnecting(false);
            setConnectionState(QStringLiteral("offline"));
            setErrorMessage(QStringLiteral("Pairing response did not contain a device credential"));
            return;
        }
        m_bearerToken = token;
        m_credentials.store(m_baseUrl, token);
        reconnect();
        return;
    }
    if (tag == QStringLiteral("model-catalog")) {
        m_modelCatalog = object.toVariantMap();
        emit modelCatalogChanged();
        return;
    }
    if (tag == QStringLiteral("past-sessions")) {
        m_pastSessions = object.value(QStringLiteral("sessions")).toArray().toVariantList();
        m_pastSessionsLoading = false;
        emit pastSessionsChanged();
        return;
    }
    if (tag == QStringLiteral("directory-suggestions")) {
        m_directorySuggestions = object.value(QStringLiteral("matches")).toArray().toVariantList();
        emit pathsChanged();
        return;
    }
    if (tag == QStringLiteral("favorite-paths")) {
        m_favoritePaths = object.value(QStringLiteral("paths")).toArray().toVariantList();
        emit pathsChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("recoverable:"))) {
        for (const auto value : object.value(QStringLiteral("events")).toArray()) {
            if (value.isObject()) {
                m_audio.enqueueClip(value.toObject());
            }
        }
        return;
    }
    if (tag == QStringLiteral("snapshot")) {
        m_availableMcpServers =
            object.value(QStringLiteral("available_mcp_servers")).toArray().toVariantList();
        m_agents.applySnapshot(object);
        m_archivedAgents.applySnapshot(object);
        requestAvatars();
        QSet<QString> activeNames;
        for (const QString& session : m_agents.sessions()) {
            if (const Agent* agent = m_agents.find(session)) {
                activeNames.insert(displayName(*agent).toCaseFolded());
            }
        }
        m_contacts.applySnapshot(object, activeNames);
        if (m_selectedSession.isEmpty() || m_agents.find(m_selectedSession) == nullptr) {
            const QString first = m_agents.firstSession();
            if (!first.isEmpty()) {
                selectSession(first);
            } else if (!m_selectedSession.isEmpty()) {
                m_selectedSession.clear();
                m_conversation = &m_emptyConversation;
                m_panes.setActiveSession({});
                emit selectedSessionChanged();
                emit conversationChanged();
                refreshSelectedProperties();
            }
        } else {
            refreshSelectedProperties();
        }
        for (auto it = m_conversations.cbegin(); it != m_conversations.cend(); ++it) {
            const Agent* agent = m_agents.find(it.key());
            ConversationModel* model = it.value();
            if (agent == nullptr || model == nullptr) {
                continue;
            }
            if (!model->conversationId().isEmpty() && !agent->conversationId.isEmpty() &&
                model->conversationId() != agent->conversationId) {
                requestTail(it.key());
            } else if (model->latestRevision() != agent->headRevision) {
                requestDelta(it.key());
            }
        }
        return;
    }
    if (tag.startsWith(QStringLiteral("updates:"))) {
        const quint64 generation = tag.section(u':', 1, 1).toULongLong();
        if (generation != m_updatesGeneration) {
            return;
        }
        const QString kind = tag.section(u':', 2, 2);
        if (kind == QStringLiteral("attention")) {
            m_attentionItems = object.value(QStringLiteral("items")).toArray().toVariantList();
        } else if (kind == QStringLiteral("jobs")) {
            m_backgroundJobs = object.value(QStringLiteral("jobs")).toArray().toVariantList();
        } else if (kind == QStringLiteral("artifacts")) {
            m_updateArtifacts = object.value(QStringLiteral("artifacts")).toArray().toVariantList();
        }
        finishUpdateRequest(generation);
        return;
    }
    if (tag == QStringLiteral("update-action:decision") ||
        tag == QStringLiteral("update-action:job-cancel")) {
        loadUpdates();
        return;
    }
    if (tag.startsWith(QStringLiteral("team-list:"))) {
        const quint64 generation = tag.section(u':', 1, 1).toULongLong();
        if (generation != m_teamListGeneration) {
            return;
        }
        m_teams = object.value(QStringLiteral("teams")).toArray().toVariantList();
        bool selectionExists = m_selectedTeamId.isEmpty();
        for (const QVariant& value : std::as_const(m_teams)) {
            if (value.toMap().value(QStringLiteral("team_id")).toString() == m_selectedTeamId) {
                selectionExists = true;
                break;
            }
        }
        m_teamListLoading = false;
        emit teamsChanged();
        if ((!selectionExists || m_selectedTeamId.isEmpty()) && !m_teams.isEmpty()) {
            selectTeam(m_teams.first().toMap().value(QStringLiteral("team_id")).toString());
        } else if (!selectionExists) {
            m_selectedTeamId.clear();
            m_teamMessages.clear();
            emit teamsChanged();
        }
        return;
    }
    if (tag.startsWith(QStringLiteral("team-messages:"))) {
        const quint64 generation = tag.section(u':', 1, 1).toULongLong();
        const QString requestedTeam = tag.section(u':', 2);
        if (generation != m_teamMessagesGeneration || requestedTeam != m_selectedTeamId) {
            return;
        }
        m_teamMessages = object.value(QStringLiteral("messages")).toArray().toVariantList();
        m_teamMessagesLoading = false;
        emit teamsChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("team-action:"))) {
        const QString selected = m_selectedTeamId;
        loadTeams();
        if (!selected.isEmpty()) {
            selectTeam(selected);
        }
        return;
    }
    if (tag.startsWith(QStringLiteral("log-tail:"))) {
        const QString session = tag.sliced(9);
        m_logRequestsInFlight.remove(session);
        ensureConversation(session)->applyLog(object, ConversationModel::LoadKind::Tail);
        continuePendingLogRequest(session);
        return;
    }
    if (tag.startsWith(QStringLiteral("log-replace:"))) {
        const QString session = tag.sliced(12);
        m_logRequestsInFlight.remove(session);
        ensureConversation(session)->applyLog(object, ConversationModel::LoadKind::Replace);
        continuePendingLogRequest(session);
        return;
    }
    if (tag.startsWith(QStringLiteral("log-delta:"))) {
        const QString session = tag.sliced(10);
        m_logRequestsInFlight.remove(session);
        ensureConversation(session)->applyLog(object, ConversationModel::LoadKind::Delta);
        if (object.value(QStringLiteral("has_more")).toBool()) {
            requestDelta(session);
        }
        continuePendingLogRequest(session);
        return;
    }
    if (tag.startsWith(QStringLiteral("log-older:"))) {
        const QString session = tag.sliced(10);
        m_logRequestsInFlight.remove(session);
        ensureConversation(session)->applyLog(object, ConversationModel::LoadKind::Older);
        continuePendingLogRequest(session);
        return;
    }
    if (tag.startsWith(QStringLiteral("send:"))) {
        requestDelta(m_deliverySessions.value(tag.sliced(5), m_selectedSession));
        return;
    }
    if (tag.startsWith(QStringLiteral("agent-create:"))) {
        const QString session = object.value(QStringLiteral("session")).toString();
        requestSnapshot();
        if (!session.isEmpty()) {
            selectSession(session);
        }
        emit agentMutationSucceeded(session);
        return;
    }
    if (tag.startsWith(QStringLiteral("agent-release:")) ||
        tag.startsWith(QStringLiteral("agent-setting:"))) {
        requestSnapshot();
        emit agentMutationSucceeded(tag.section(':', 1));
        return;
    }
    if (tag.startsWith(QStringLiteral("schedule-toggle:"))) {
        requestSnapshot();
        return;
    }
    if (tag.startsWith(QStringLiteral("voices:"))) {
        const QString session = tag.sliced(7);
        const Agent* agent = m_agents.find(session);
        m_voiceBio = object.value(QStringLiteral("bio")).toString();
        m_voices.applyResponse(object, agent == nullptr ? QString{} : agent->voiceId);
        m_voicesLoading = false;
        emit voicesChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("voice-select:"))) {
        const QString session = tag.sliced(13);
        requestSnapshot();
        loadVoices(session);
        emit agentMutationSucceeded(session);
        return;
    }
    if (tag == QStringLiteral("orchestrator-load") || tag == QStringLiteral("orchestrator-save")) {
        const QJsonObject settings = object.value(QStringLiteral("settings")).toObject();
        m_orchestratorSettings = settings.toVariantMap();
        const QJsonArray recent = object.value(QStringLiteral("recent_decisions")).toArray();
        if (!recent.isEmpty()) {
            const QJsonObject decision = recent.first().toObject();
            m_orchestratorLastDecision =
                QStringLiteral("%1: %2 (%3)")
                    .arg(decision.value(QStringLiteral("final_action"))
                             .toString(decision.value(QStringLiteral("decision_kind")).toString()),
                         decision.value(QStringLiteral("target_session"))
                             .toString(QStringLiteral("none")),
                         QString::number(decision.value(QStringLiteral("confidence")).toDouble(),
                                         'f', 2));
        } else if (object.contains(QStringLiteral("recent_decisions"))) {
            m_orchestratorLastDecision = QStringLiteral("No decisions logged yet.");
        }
        m_orchestratorLoading = false;
        emit orchestratorChanged();
    }
}

void AppController::handleBytes(const QString& tag, const QByteArray& bytes,
                                const QByteArray& contentType) {
    if (!tag.startsWith(QStringLiteral("avatar:"))) {
        return;
    }
    const auto request = m_avatarRequests.take(tag);
    const QString session = request.first;
    const QString url = request.second;
    const qsizetype separator = contentType.indexOf(';');
    const QByteArray mime =
        contentType.left(separator < 0 ? contentType.size() : separator).trimmed().toLower();
    const Agent* agent = m_agents.find(session);
    if (session.isEmpty() || agent == nullptr || avatarUrlForAgent(*agent) != url) {
        return;
    }
    if (bytes.isEmpty() || bytes.size() > MaxPortraitBytes || !mime.startsWith("image/")) {
        m_avatarFailures.insert(session, url);
        return;
    }
    const QString dataUrl =
        QStringLiteral("data:%1;base64,%2")
            .arg(QString::fromLatin1(mime), QString::fromLatin1(bytes.toBase64()));
    m_avatarSources.insert(session, QUrl(dataUrl));
    m_avatarFailures.remove(session);
    ++m_avatarRevision;
    emit avatarRevisionChanged();
}

void AppController::handleRequestFailure(const QString& tag, const QString& message,
                                         int statusCode) {
    if (tag.startsWith(QStringLiteral("composer-upload:"))) {
        const QVariantMap pending = m_pendingUploads.take(tag);
        if (!pending.isEmpty()) {
            const QString session = pending.value(QStringLiteral("session")).toString();
            const QString attachmentId = pending.value(QStringLiteral("id")).toString();
            QVariantList attachments = composerAttachments({}, session);
            for (QVariant& value : attachments) {
                QVariantMap attachment = value.toMap();
                if (attachment.value(QStringLiteral("id")).toString() == attachmentId) {
                    attachment.insert(QStringLiteral("status"), QStringLiteral("failed"));
                    value = attachment;
                    break;
                }
            }
            storeComposerAttachments(session, attachments);
        }
        const QString detail = statusCode > 0
                                   ? QStringLiteral("Upload failed: %1 (HTTP %2)")
                                         .arg(message)
                                         .arg(statusCode)
                                   : QStringLiteral("Upload failed: %1").arg(message);
        setErrorMessage(detail);
        return;
    }
    if (tag.startsWith(QStringLiteral("turn-queue:")) ||
        tag.startsWith(QStringLiteral("queue-action:"))) {
        if (tag.startsWith(QStringLiteral("turn-queue:"))) {
            const quint64 generation = tag.section(u':', 1, 1).toULongLong();
            if (generation != m_turnQueueGeneration) {
                return;
            }
            m_turnQueueLoading = false;
        } else {
            m_queueActionSessions.remove(tag);
        }
        m_turnQueueError = statusCode > 0
                               ? QStringLiteral("%1 (HTTP %2)").arg(message).arg(statusCode)
                               : message;
        emit turnQueueChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("profile:"))) {
        const quint64 generation = tag.section(u':', 1, 1).toULongLong();
        if (generation != m_profileGeneration) {
            return;
        }
        m_profileLoading = false;
        m_profileError = statusCode > 0
                             ? QStringLiteral("%1 (HTTP %2)").arg(message).arg(statusCode)
                             : message;
        emit profileChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("avatar:"))) {
        const auto request = m_avatarRequests.take(tag);
        if (!request.first.isEmpty()) {
            m_avatarFailures.insert(request.first, request.second);
        }
        return;
    }
    if (tag.startsWith(QStringLiteral("updates:"))) {
        const quint64 generation = tag.section(u':', 1, 1).toULongLong();
        if (generation != m_updatesGeneration) {
            return;
        }
        m_updatesError =
            statusCode > 0 ? QStringLiteral("%1 (HTTP %2)").arg(message).arg(statusCode) : message;
        finishUpdateRequest(generation);
        return;
    }
    if (tag.startsWith(QStringLiteral("update-action:"))) {
        m_updatesError =
            statusCode > 0 ? QStringLiteral("%1 (HTTP %2)").arg(message).arg(statusCode) : message;
        emit updatesChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("team-list:")) ||
        tag.startsWith(QStringLiteral("team-messages:")) ||
        tag.startsWith(QStringLiteral("team-action:"))) {
        if (tag.startsWith(QStringLiteral("team-list:"))) {
            const quint64 generation = tag.section(u':', 1, 1).toULongLong();
            if (generation != m_teamListGeneration) {
                return;
            }
        } else if (tag.startsWith(QStringLiteral("team-messages:"))) {
            const quint64 generation = tag.section(u':', 1, 1).toULongLong();
            if (generation != m_teamMessagesGeneration) {
                return;
            }
        }
        if (tag.startsWith(QStringLiteral("team-list:"))) {
            m_teamListLoading = false;
        } else if (tag.startsWith(QStringLiteral("team-messages:"))) {
            m_teamMessagesLoading = false;
        } else {
            m_teamListLoading = false;
            m_teamMessagesLoading = false;
        }
        m_teamsError =
            statusCode > 0 ? QStringLiteral("%1 (HTTP %2)").arg(message).arg(statusCode) : message;
        emit teamsChanged();
        return;
    }
    if (tag.startsWith(QStringLiteral("recoverable:"))) {
        return;
    }
    const QString detail =
        statusCode > 0 ? QStringLiteral("%1 (HTTP %2)").arg(message).arg(statusCode) : message;
    setErrorMessage(detail);
    if (tag == QStringLiteral("server-info")) {
        setConnecting(false);
        setConnectionState(statusCode == 401 ? QStringLiteral("unauthorized")
                                             : QStringLiteral("offline"));
    } else if (tag.startsWith(QStringLiteral("log-"))) {
        const QString session = tag.section(':', 1);
        m_logRequestsInFlight.remove(session);
        m_pendingLogMode.remove(session);
        ConversationModel* model = ensureConversation(session);
        model->setLoading(false);
        model->setError(detail);
    } else if (tag.startsWith(QStringLiteral("send:"))) {
        const QString clientId = tag.sliced(5);
        const QString session = m_deliverySessions.take(clientId);
        if (QTimer* timer = m_deliveryTimers.take(clientId)) {
            timer->stop();
            timer->deleteLater();
        }
        ensureConversation(session)->markDeliveryFailed(clientId);
    } else if (tag.startsWith(QStringLiteral("voices:"))) {
        m_voicesLoading = false;
        emit voicesChanged();
    } else if (tag.startsWith(QStringLiteral("orchestrator-"))) {
        m_orchestratorLoading = false;
        emit orchestratorChanged();
    } else if (tag == QStringLiteral("past-sessions")) {
        m_pastSessionsLoading = false;
        emit pastSessionsChanged();
    } else if (tag == QStringLiteral("directory-suggestions")) {
        m_directorySuggestions.clear();
        emit pathsChanged();
    } else if (tag == QStringLiteral("favorite-paths")) {
        m_favoritePaths.clear();
        emit pathsChanged();
    }
}

void AppController::handleSseEvent(const QJsonObject& event) {
    const QString type = event.value(QStringLiteral("type")).toString();
    const QString session = event.value(QStringLiteral("session")).toString();
    if (type == QStringLiteral("agent-roster")) {
        requestSnapshot();
    } else if (type == QStringLiteral("transcript-updated")) {
        if (m_conversations.contains(session)) {
            requestDelta(session);
        }
    } else if (type == QStringLiteral("agent-state")) {
        m_agents.applyStateEvent(event);
        if (ConversationModel* model = m_conversations.value(session, nullptr)) {
            const QString state = event.value(QStringLiteral("kind")).toString();
            if (state == QStringLiteral("thinking")) {
                model->showTransientThinking(agentName(session));
            } else if (state == QStringLiteral("tool") || state == QStringLiteral("waiting") ||
                       state == QStringLiteral("interrupted") || state == QStringLiteral("done") ||
                       state == QStringLiteral("idle")) {
                model->clearRunningActivity();
            }
        }
        if (session == m_selectedSession) {
            refreshSelectedProperties();
        }
    } else if (type == QStringLiteral("agent-activity")) {
        if (ConversationModel* model = m_conversations.value(session, nullptr)) {
            model->applyActivityEvent(event);
        }
    } else if (type == QStringLiteral("agent-focus")) {
        m_agents.applyFocusEvent(event);
    } else if (type == QStringLiteral("queue-updated")) {
        m_agents.applyQueueEvent(event);
        if (session == m_turnQueueSession) {
            loadTurnQueue(session);
        }
    } else if (type == QStringLiteral("user-notification")) {
        m_agents.applyNotificationEvent(event);
        if (session != m_selectedSession) {
            emit notificationRequested(event.value(QStringLiteral("persona")).toString(session),
                                       event.value(QStringLiteral("preview")).toString());
        }
    } else if (type == QStringLiteral("audio")) {
        m_audio.enqueueClip(event);
    } else if (type == QStringLiteral("tts-error")) {
        setErrorMessage(event.value(QStringLiteral("message"))
                            .toString(event.value(QStringLiteral("error")).toString()));
    } else if (type == QStringLiteral("server-version")) {
        const QString version = event.value(QStringLiteral("version")).toString();
        if (!version.isEmpty() && version != m_serverVersion) {
            m_serverVersion = version;
            emit serverInfoChanged();
            requestSnapshot();
        }
    } else if (type == QStringLiteral("remote-action")) {
        const QString action = event.value(QStringLiteral("action")).toString();
        if (action == QStringLiteral("stop-agent")) {
            stopAgent();
        }
    } else if (type == QStringLiteral("artifact-updated") ||
               type == QStringLiteral("attention-updated") ||
               type == QStringLiteral("background-job-updated")) {
        loadUpdates();
    }
}

void AppController::refreshSelectedProperties() { emit selectedAgentChanged(); }

ConversationModel* AppController::ensureConversation(const QString& session) {
    if (session.isEmpty()) {
        return &m_emptyConversation;
    }
    if (ConversationModel* existing = m_conversations.value(session, nullptr)) {
        return existing;
    }
    auto* model = new ConversationModel(this);
    model->openSession(session);
    model->restoreCacheSnapshot(m_transcriptCache.load(m_baseUrl, session));
    connectConversationSignals(model, session);
    m_conversations.insert(session, model);
    return model;
}

void AppController::connectConversationSignals(ConversationModel* model, const QString& session) {
    connect(model, &ConversationModel::replacementRequired, this,
            [this, session] { requestTail(session, true); });
    connect(model, &ConversationModel::deliveryConfirmed, this, [this](const QString& clientId) {
        m_deliverySessions.remove(clientId);
        if (QTimer* timer = m_deliveryTimers.take(clientId)) {
            timer->stop();
            timer->deleteLater();
        }
        if (m_deliveryTimers.isEmpty() && m_sending) {
            m_sending = false;
            emit sendingChanged();
        }
    });
    const auto cacheChanged = [this, session] { scheduleConversationCache(session); };
    connect(model, &QAbstractItemModel::dataChanged, this,
            [cacheChanged](const QModelIndex&, const QModelIndex&, const QList<int>&) {
                cacheChanged();
            });
    connect(model, &QAbstractItemModel::rowsInserted, this,
            [cacheChanged](const QModelIndex&, int, int) { cacheChanged(); });
    connect(model, &QAbstractItemModel::rowsRemoved, this,
            [cacheChanged](const QModelIndex&, int, int) { cacheChanged(); });
    connect(model, &QAbstractItemModel::modelReset, this, cacheChanged);
}

void AppController::scheduleConversationCache(const QString& session) {
    if (!m_cacheEnabled) {
        return;
    }
    QTimer* timer = m_cacheTimers.value(session, nullptr);
    if (timer == nullptr) {
        timer = new QTimer(this);
        timer->setSingleShot(true);
        connect(timer, &QTimer::timeout, this, [this, session] {
            ConversationModel* model = m_conversations.value(session, nullptr);
            if (model != nullptr) {
                (void)m_transcriptCache.save(m_baseUrl, session, model->cacheSnapshot());
            }
        });
        m_cacheTimers.insert(session, timer);
    }
    timer->start(250);
}

QString AppController::defaultToken() const {
    const QString configHome =
        qEnvironmentVariable("XDG_CONFIG_HOME", QDir::home().filePath(QStringLiteral(".config")));
    QFile file(QDir(configHome).filePath(QStringLiteral("clarp/config.toml")));
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return {};
    }
    const QString contents = QString::fromUtf8(file.readAll());
    static const QRegularExpression expression(
        QStringLiteral(R"token(^\s*auth_token\s*=\s*"([^"]*)")token"),
        QRegularExpression::MultilineOption);
    return expression.match(contents).captured(1);
}

} // namespace clarp
