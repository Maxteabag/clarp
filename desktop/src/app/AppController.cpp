#include "app/AppController.h"

#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QSettings>
#include <QUrlQuery>
#include <QUuid>
#include <algorithm>
#include <utility>

namespace clarp {
namespace {

constexpr int DeliveryTimeoutMs = 20'000;

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

} // namespace

AppController::AppController(QObject* parent)
    : QObject(parent), m_credentials(this), m_audio(this), m_agents(this),
      m_archivedAgents(true, this), m_contacts(this), m_panes(this), m_voices(this),
      m_emptyConversation(this), m_conversation(&m_emptyConversation) {
    QSettings settings;
    m_baseUrl = normalizedBaseUrl(
        qEnvironmentVariable("CLARP_BASE_URL", settings
                                                   .value(QStringLiteral("connection/baseUrl"),
                                                          QStringLiteral("http://127.0.0.1:7682"))
                                                   .toString()));
    m_muted = settings.value(QStringLiteral("audio/muted"), false).toBool();
    m_toolsVisible = settings.value(QStringLiteral("conversation/toolsVisible"), false).toBool();
    m_lastWorkingDirectory =
        settings.value(QStringLiteral("launch/workingDirectory"), QStringLiteral("~")).toString();
    m_lastBackend = settings.value(QStringLiteral("launch/backend")).toString();
    m_bearerToken = qEnvironmentVariable("CLARP_TOKEN", defaultToken());

    connect(&m_api, &ApiClient::jsonReceived, this, &AppController::handleJson);
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

quint64 AppController::agentRevision() const { return m_agentRevision; }

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

QString AppController::connectionState() const { return m_connectionState; }

QString AppController::errorMessage() const { return m_errorMessage; }

QString AppController::serverName() const { return m_serverName; }

QString AppController::serverVersion() const { return m_serverVersion; }

void AppController::setBaseUrl(const QString& value) {
    const QString normalized = normalizedBaseUrl(value);
    if (m_baseUrl == normalized) {
        return;
    }
    m_baseUrl = normalized;
    QSettings().setValue(QStringLiteral("connection/baseUrl"), m_baseUrl);
    emit baseUrlChanged();
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
    m_api.setEndpoint(endpoint, m_bearerToken);
    m_sse.setEndpoint(endpoint, m_bearerToken);
    m_audio.setEndpoint(endpoint, m_bearerToken);
    m_audio.setMuted(m_muted);
    setErrorMessage({});
    setConnecting(true);
    setConnectionState(QStringLiteral("connecting"));
    m_api.get(QStringLiteral("server-info"), QStringLiteral("/server-info"));
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

void AppController::requestSnapshot() {
    m_api.get(QStringLiteral("snapshot"), QStringLiteral("/agents/snapshot"));
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

void AppController::requestTail(const QString& requestedSession) {
    const QString session = requestedSession.isEmpty() ? m_selectedSession : requestedSession;
    if (session.isEmpty()) {
        return;
    }
    if (!beginLogRequest(session, QStringLiteral("tail"))) {
        return;
    }
    ConversationModel* model = ensureConversation(session);
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("session"), session);
    query.addQueryItem(QStringLiteral("limit"), QStringLiteral("100"));
    query.addQueryItem(QStringLiteral("include_automated"), QStringLiteral("0"));
    model->setLoading(true);
    m_api.get(QStringLiteral("log-tail:") + session, QStringLiteral("/log"), query);
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
    if (mode == QStringLiteral("tail") || queued.isEmpty()) {
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
        m_agents.applySnapshot(object);
        m_archivedAgents.applySnapshot(object);
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
    if (tag.startsWith(QStringLiteral("log-tail:"))) {
        const QString session = tag.sliced(9);
        m_logRequestsInFlight.remove(session);
        ensureConversation(session)->applyLog(object, ConversationModel::LoadKind::Tail);
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

void AppController::handleRequestFailure(const QString& tag, const QString& message,
                                         int statusCode) {
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
    connectConversationSignals(model, session);
    m_conversations.insert(session, model);
    return model;
}

void AppController::connectConversationSignals(ConversationModel* model, const QString& session) {
    connect(model, &ConversationModel::replacementRequired, this,
            [this, session] { requestTail(session); });
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
