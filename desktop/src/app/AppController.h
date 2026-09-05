#pragma once

#include "app/CredentialStore.h"
#include "app/TranscriptCache.h"
#include "media/AudioController.h"
#include "models/AgentListModel.h"
#include "models/ContactListModel.h"
#include "models/ConversationModel.h"
#include "models/PaneTreeModel.h"
#include "models/VoiceListModel.h"
#include "network/ApiClient.h"
#include "network/SseClient.h"

#include <QHash>
#include <QObject>
#include <QSet>
#include <QTemporaryDir>
#include <QTimer>
#include <QUrl>
#include <QVariantMap>
#include <QtQmlIntegration>

namespace clarp {

class AppController : public QObject {
    Q_OBJECT
    QML_ELEMENT

    Q_PROPERTY(clarp::AgentListModel* agents READ agents CONSTANT)
    Q_PROPERTY(clarp::AgentListModel* archivedAgents READ archivedAgents CONSTANT)
    Q_PROPERTY(clarp::AudioController* audio READ audio CONSTANT)
    Q_PROPERTY(clarp::ConversationModel* conversation READ conversation NOTIFY conversationChanged)
    Q_PROPERTY(clarp::ContactListModel* contacts READ contacts CONSTANT)
    Q_PROPERTY(clarp::PaneTreeModel* panes READ panes CONSTANT)
    Q_PROPERTY(clarp::VoiceListModel* voices READ voices CONSTANT)
    Q_PROPERTY(QString voiceBio READ voiceBio NOTIFY voicesChanged)
    Q_PROPERTY(bool voicesLoading READ voicesLoading NOTIFY voicesChanged)
    Q_PROPERTY(
        QVariantMap orchestratorSettings READ orchestratorSettings NOTIFY orchestratorChanged)
    Q_PROPERTY(
        QString orchestratorLastDecision READ orchestratorLastDecision NOTIFY orchestratorChanged)
    Q_PROPERTY(bool orchestratorLoading READ orchestratorLoading NOTIFY orchestratorChanged)
    Q_PROPERTY(QVariantList backendOptions READ backendOptions NOTIFY modelCatalogChanged)
    Q_PROPERTY(QVariantList availableMcpServers READ availableMcpServers NOTIFY agentRevisionChanged)
    Q_PROPERTY(quint64 agentRevision READ agentRevision NOTIFY agentRevisionChanged)
    Q_PROPERTY(quint64 avatarRevision READ avatarRevision NOTIFY avatarRevisionChanged)
    Q_PROPERTY(quint64 mediaRevision READ mediaRevision NOTIFY mediaChanged)
    Q_PROPERTY(QVariantList pastSessions READ pastSessions NOTIFY pastSessionsChanged)
    Q_PROPERTY(bool pastSessionsLoading READ pastSessionsLoading NOTIFY pastSessionsChanged)
    Q_PROPERTY(QVariantList directorySuggestions READ directorySuggestions NOTIFY pathsChanged)
    Q_PROPERTY(QVariantList favoritePaths READ favoritePaths NOTIFY pathsChanged)
    Q_PROPERTY(QString lastWorkingDirectory READ lastWorkingDirectory NOTIFY launchDefaultsChanged)
    Q_PROPERTY(QString lastBackend READ lastBackend NOTIFY launchDefaultsChanged)
    Q_PROPERTY(bool hasStoredCredential READ hasStoredCredential NOTIFY hasStoredCredentialChanged)
    Q_PROPERTY(QString baseUrl READ baseUrl WRITE setBaseUrl NOTIFY baseUrlChanged)
    Q_PROPERTY(QString selectedSession READ selectedSession NOTIFY selectedSessionChanged)
    Q_PROPERTY(QString selectedName READ selectedName NOTIFY selectedAgentChanged)
    Q_PROPERTY(QString selectedState READ selectedState NOTIFY selectedAgentChanged)
    Q_PROPERTY(QString selectedBackend READ selectedBackend NOTIFY selectedAgentChanged)
    Q_PROPERTY(bool connected READ connected NOTIFY connectedChanged)
    Q_PROPERTY(bool connecting READ connecting NOTIFY connectingChanged)
    Q_PROPERTY(bool sending READ sending NOTIFY sendingChanged)
    Q_PROPERTY(bool muted READ muted WRITE setMuted NOTIFY mutedChanged)
    Q_PROPERTY(bool toolsVisible READ toolsVisible WRITE setToolsVisible NOTIFY toolsVisibleChanged)
    Q_PROPERTY(bool timestampsVisible READ timestampsVisible WRITE setTimestampsVisible
                   NOTIFY timestampsVisibleChanged)
    Q_PROPERTY(bool sharedFilesystem READ sharedFilesystem WRITE setSharedFilesystem
                   NOTIFY sharedFilesystemChanged)
    Q_PROPERTY(QString connectionState READ connectionState NOTIFY connectionStateChanged)
    Q_PROPERTY(QString errorMessage READ errorMessage NOTIFY errorMessageChanged)
    Q_PROPERTY(QString serverName READ serverName NOTIFY serverInfoChanged)
    Q_PROPERTY(QString serverVersion READ serverVersion NOTIFY serverInfoChanged)
    Q_PROPERTY(QString composerFocusPane READ composerFocusPane NOTIFY composerFocusPaneChanged)
    Q_PROPERTY(quint64 composerRevision READ composerRevision NOTIFY composerRevisionChanged)
    Q_PROPERTY(QVariantList attentionItems READ attentionItems NOTIFY updatesChanged)
    Q_PROPERTY(QVariantList backgroundJobs READ backgroundJobs NOTIFY updatesChanged)
    Q_PROPERTY(QVariantList updateArtifacts READ updateArtifacts NOTIFY updatesChanged)
    Q_PROPERTY(bool updatesLoading READ updatesLoading NOTIFY updatesChanged)
    Q_PROPERTY(QString updatesError READ updatesError NOTIFY updatesChanged)
    Q_PROPERTY(int attentionCount READ attentionCount NOTIFY updatesChanged)
    Q_PROPERTY(QVariantList teams READ teams NOTIFY teamsChanged)
    Q_PROPERTY(QVariantList teamMessages READ teamMessages NOTIFY teamsChanged)
    Q_PROPERTY(QString selectedTeamId READ selectedTeamId NOTIFY teamsChanged)
    Q_PROPERTY(bool teamsLoading READ teamsLoading NOTIFY teamsChanged)
    Q_PROPERTY(QString teamsError READ teamsError NOTIFY teamsChanged)
    Q_PROPERTY(QVariantList turnQueueItems READ turnQueueItems NOTIFY turnQueueChanged)
    Q_PROPERTY(QString turnQueueSession READ turnQueueSession NOTIFY turnQueueChanged)
    Q_PROPERTY(bool turnQueuePaused READ turnQueuePaused NOTIFY turnQueueChanged)
    Q_PROPERTY(bool turnQueueLoading READ turnQueueLoading NOTIFY turnQueueChanged)
    Q_PROPERTY(QString turnQueueError READ turnQueueError NOTIFY turnQueueChanged)
    Q_PROPERTY(QVariantMap profileTaskPlan READ profileTaskPlan NOTIFY profileChanged)
    Q_PROPERTY(QString profileSession READ profileSession NOTIFY profileChanged)
    Q_PROPERTY(bool profileLoading READ profileLoading NOTIFY profileChanged)
    Q_PROPERTY(QString profileError READ profileError NOTIFY profileChanged)
    Q_PROPERTY(QVariantList profilePrompts READ profilePrompts NOTIFY profileChanged)
    Q_PROPERTY(bool profilePromptsHaveMore READ profilePromptsHaveMore NOTIFY profileChanged)
    Q_PROPERTY(bool profilePromptsLoading READ profilePromptsLoading NOTIFY profileChanged)
    Q_PROPERTY(QVariantMap profileHeartbeat READ profileHeartbeat NOTIFY profileChanged)
    Q_PROPERTY(QVariantMap diagnosticsHealth READ diagnosticsHealth NOTIFY settingsStatusChanged)
    Q_PROPERTY(QVariantMap transcriptionCapabilities READ transcriptionCapabilities
                   NOTIFY settingsStatusChanged)
    Q_PROPERTY(QVariantMap ttsProviderStatus READ ttsProviderStatus NOTIFY settingsStatusChanged)
    Q_PROPERTY(bool settingsStatusLoading READ settingsStatusLoading NOTIFY settingsStatusChanged)

  public:
    explicit AppController(QObject* parent = nullptr);
    ~AppController() override;

    [[nodiscard]] AgentListModel* agents();
    [[nodiscard]] AgentListModel* archivedAgents();
    [[nodiscard]] AudioController* audio();
    [[nodiscard]] ConversationModel* conversation();
    [[nodiscard]] ContactListModel* contacts();
    [[nodiscard]] PaneTreeModel* panes();
    [[nodiscard]] VoiceListModel* voices();
    [[nodiscard]] QString voiceBio() const;
    [[nodiscard]] bool voicesLoading() const;
    [[nodiscard]] QVariantMap orchestratorSettings() const;
    [[nodiscard]] QString orchestratorLastDecision() const;
    [[nodiscard]] bool orchestratorLoading() const;
    [[nodiscard]] QVariantList backendOptions() const;
    [[nodiscard]] QVariantList availableMcpServers() const;
    [[nodiscard]] quint64 agentRevision() const;
    [[nodiscard]] quint64 avatarRevision() const;
    [[nodiscard]] quint64 mediaRevision() const;
    [[nodiscard]] QVariantList pastSessions() const;
    [[nodiscard]] bool pastSessionsLoading() const;
    [[nodiscard]] QVariantList directorySuggestions() const;
    [[nodiscard]] QVariantList favoritePaths() const;
    [[nodiscard]] QString lastWorkingDirectory() const;
    [[nodiscard]] QString lastBackend() const;
    [[nodiscard]] bool hasStoredCredential() const;
    Q_INVOKABLE [[nodiscard]] ConversationModel* conversationForSession(const QString& session);
    Q_INVOKABLE [[nodiscard]] QUrl avatarSource(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QVariantList mediaForSession(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QUrl mediaSource(const QString& assetId) const;
    Q_INVOKABLE [[nodiscard]] QString resolveMediaMarkdown(const QString& markdown) const;
    Q_INVOKABLE [[nodiscard]] QStringList markdownDisplayBlocks(const QString& markdown) const;
    Q_INVOKABLE [[nodiscard]] QVariantList artifactsForSession(const QString& session) const;
    [[nodiscard]] QString baseUrl() const;
    [[nodiscard]] QString selectedSession() const;
    [[nodiscard]] QString selectedName() const;
    [[nodiscard]] QString selectedState() const;
    [[nodiscard]] QString selectedBackend() const;
    [[nodiscard]] bool connected() const;
    [[nodiscard]] bool connecting() const;
    [[nodiscard]] bool sending() const;
    [[nodiscard]] bool muted() const;
    [[nodiscard]] bool toolsVisible() const;
    [[nodiscard]] bool timestampsVisible() const;
    [[nodiscard]] bool sharedFilesystem() const;
    [[nodiscard]] QString connectionState() const;
    [[nodiscard]] QString errorMessage() const;
    [[nodiscard]] QString serverName() const;
    [[nodiscard]] QString serverVersion() const;
    [[nodiscard]] QString composerFocusPane() const;
    [[nodiscard]] quint64 composerRevision() const;
    [[nodiscard]] QVariantList attentionItems() const;
    [[nodiscard]] QVariantList backgroundJobs() const;
    [[nodiscard]] QVariantList updateArtifacts() const;
    [[nodiscard]] bool updatesLoading() const;
    [[nodiscard]] QString updatesError() const;
    [[nodiscard]] int attentionCount() const;
    [[nodiscard]] QVariantList teams() const;
    [[nodiscard]] QVariantList teamMessages() const;
    [[nodiscard]] QString selectedTeamId() const;
    [[nodiscard]] bool teamsLoading() const;
    [[nodiscard]] QString teamsError() const;
    [[nodiscard]] QVariantList turnQueueItems() const;
    [[nodiscard]] QString turnQueueSession() const;
    [[nodiscard]] bool turnQueuePaused() const;
    [[nodiscard]] bool turnQueueLoading() const;
    [[nodiscard]] QString turnQueueError() const;
    [[nodiscard]] QVariantMap profileTaskPlan() const;
    [[nodiscard]] QString profileSession() const;
    [[nodiscard]] bool profileLoading() const;
    [[nodiscard]] QString profileError() const;
    [[nodiscard]] QVariantList profilePrompts() const;
    [[nodiscard]] bool profilePromptsHaveMore() const;
    [[nodiscard]] bool profilePromptsLoading() const;
    [[nodiscard]] QVariantMap profileHeartbeat() const;
    [[nodiscard]] QVariantMap diagnosticsHealth() const;
    [[nodiscard]] QVariantMap transcriptionCapabilities() const;
    [[nodiscard]] QVariantMap ttsProviderStatus() const;
    [[nodiscard]] bool settingsStatusLoading() const;

    void setBaseUrl(const QString& value);
    void setMuted(bool muted);
    void setToolsVisible(bool visible);
    void setTimestampsVisible(bool visible);
    void setSharedFilesystem(bool shared);

    Q_INVOKABLE void connectToServer(const QString& url, const QString& token);
    Q_INVOKABLE void pairDevice(const QString& url, const QString& code);
    Q_INVOKABLE void forgetCredential();
    Q_INVOKABLE void reconnect();
    Q_INVOKABLE void selectSession(const QString& session);
    Q_INVOKABLE void refreshConversation();
    Q_INVOKABLE void loadOlder();
    Q_INVOKABLE void sendMessage(const QString& text, bool queueIfBusy = false);
    Q_INVOKABLE void sendMessageTo(const QString& session, const QString& text,
                                   bool queueIfBusy = false);
    Q_INVOKABLE void retryFailedMessage(const QString& session, const QString& messageId);
    Q_INVOKABLE void stopAgent();
    Q_INVOKABLE void stopSession(const QString& session);
    Q_INVOKABLE void toggleRecordingForSession(const QString& session);
    Q_INVOKABLE void refreshSession(const QString& session);
    Q_INVOKABLE void loadOlderSession(const QString& session);
    Q_INVOKABLE [[nodiscard]] QString agentName(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentState(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] int agentQueueCount(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QVariantMap agentDetails(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentBackend(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentWorkingDirectory(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentModel(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentEffort(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentNameById(const QString& agentId) const;
    Q_INVOKABLE [[nodiscard]] QString teamNameById(const QString& teamId) const;
    Q_INVOKABLE [[nodiscard]] QVariantList teamAgentChoices() const;
    Q_INVOKABLE [[nodiscard]] QVariantList matchingAgents(const QString& query) const;
    Q_INVOKABLE [[nodiscard]] QVariantList modelsForBackend(const QString& backend) const;
    Q_INVOKABLE [[nodiscard]] QVariantList effortsForModel(const QString& backend,
                                                           const QString& model) const;
    Q_INVOKABLE [[nodiscard]] bool backendSupportsResume(const QString& backend) const;
    Q_INVOKABLE [[nodiscard]] bool backendSupportsFork(const QString& backend) const;
    Q_INVOKABLE void loadPastSessions(const QString& workingDirectory, const QString& backend,
                                      bool allProjects = false);
    Q_INVOKABLE void loadDirectorySuggestions(const QString& path);
    Q_INVOKABLE void loadFavoritePaths();
    Q_INVOKABLE void refreshAgents();
    Q_INVOKABLE void createAgent(const QString& name, const QString& workingDirectory,
                                 const QString& backend, const QString& model,
                                 const QString& effort, const QString& replaceSession = {},
                                 const QString& mode = {}, const QString& pastSessionId = {},
                                 const QVariantList& mcpServers = {});
    Q_INVOKABLE void releaseAgent(const QString& session);
    Q_INVOKABLE void setAgentHeartbeat(const QString& session, bool enabled);
    Q_INVOKABLE void setAgentDreaming(const QString& session, bool enabled);
    Q_INVOKABLE void setAgentPushMuted(const QString& session, bool muted);
    Q_INVOKABLE void archiveAgent(const QString& session);
    Q_INVOKABLE void setAgentArchived(const QString& session, bool archived);
    Q_INVOKABLE void setScheduleEnabled(const QString& scheduleId, bool enabled);
    Q_INVOKABLE void loadVoices(const QString& session);
    Q_INVOKABLE void previewVoice(const QString& session, const QString& agentName,
                                  const QString& voiceId);
    Q_INVOKABLE void chooseVoice(const QString& session, const QString& voiceId);
    Q_INVOKABLE void loadOrchestrator();
    Q_INVOKABLE void saveOrchestrator(bool enabled, bool fallbackOnly, double confidence,
                                      const QString& provider, const QString& model,
                                      const QString& effort, int timeoutMs);
    Q_INVOKABLE void clearError();
    Q_INVOKABLE [[nodiscard]] QUrl resourceUrl(const QString& path) const;
    Q_INVOKABLE [[nodiscard]] QString paneDraft(const QString& paneId,
                                                const QString& session) const;
    Q_INVOKABLE void setPaneDraft(const QString& paneId, const QString& session,
                                  const QString& text);
    Q_INVOKABLE [[nodiscard]] QVariantList composerAttachments(const QString& paneId,
                                                               const QString& session) const;
    Q_INVOKABLE [[nodiscard]] bool composerCanSend(const QString& paneId,
                                                   const QString& session) const;
    Q_INVOKABLE void attachLocalFile(const QString& paneId, const QString& session,
                                     const QUrl& fileUrl);
    Q_INVOKABLE void removeComposerAttachment(const QString& paneId, const QString& session,
                                              const QString& attachmentId);
    Q_INVOKABLE bool sendComposerMessage(const QString& paneId, const QString& session,
                                         const QString& text, bool queueIfBusy = false);
    Q_INVOKABLE void requestComposerFocus(const QString& paneId);
    Q_INVOKABLE void loadUpdates();
    Q_INVOKABLE void resolveDecision(const QString& decisionId, const QString& choice,
                                     int revision);
    Q_INVOKABLE void cancelBackgroundJob(const QString& jobId);
    Q_INVOKABLE [[nodiscard]] bool updateActionPending(const QString& kind,
                                                       const QString& id) const;
    Q_INVOKABLE [[nodiscard]] double backgroundJobProgress(const QVariantMap& job) const;
    Q_INVOKABLE void loadTeams();
    Q_INVOKABLE void selectTeam(const QString& teamId);
    Q_INVOKABLE void createTeam(const QString& name, const QString& color = {});
    Q_INVOKABLE void updateTeam(const QString& teamId, const QString& name,
                                const QString& color, const QString& leaderAgentId);
    Q_INVOKABLE void addTeamMember(const QString& teamId, const QString& agentId);
    Q_INVOKABLE void removeTeamMember(const QString& teamId, const QString& agentId);
    Q_INVOKABLE void setTeamNudging(const QString& teamId, bool enabled);
    Q_INVOKABLE void deleteTeam(const QString& teamId);
    Q_INVOKABLE void loadTurnQueue(const QString& session);
    Q_INVOKABLE void updateQueuedTurn(const QString& queueId, const QString& text);
    Q_INVOKABLE void deleteQueuedTurn(const QString& queueId);
    Q_INVOKABLE void sendQueuedTurn(const QString& queueId);
    Q_INVOKABLE void openAgentFiles(const QString& session);
    Q_INVOKABLE void openAgentTerminal(const QString& session);
    Q_INVOKABLE void loadAgentProfile(const QString& session);
    Q_INVOKABLE void setAgentLlm(const QString& session, const QString& model,
                                 const QString& effort);
    Q_INVOKABLE void compactSession(const QString& session);
    Q_INVOKABLE void setAgentMcp(const QString& session, const QVariantList& servers);
    Q_INVOKABLE void loadMedia(const QString& session);
    Q_INVOKABLE void loadPromptHistory(const QString& session, bool loadMore = false);
    Q_INVOKABLE void loadSettingsStatus();
    Q_INVOKABLE void setTtsProviders(const QString& provider, const QString& fallback,
                                     const QString& voice = {});
    Q_INVOKABLE void loadMessageToolDetails(const QString& session,
                                            const QString& messageId);

  signals:
    void baseUrlChanged();
    void selectedSessionChanged();
    void selectedAgentChanged();
    void connectedChanged();
    void connectingChanged();
    void sendingChanged();
    void mutedChanged();
    void toolsVisibleChanged();
    void timestampsVisibleChanged();
    void sharedFilesystemChanged();
    void connectionStateChanged();
    void errorMessageChanged();
    void serverInfoChanged();
    void notificationRequested(const QString& title, const QString& body);
    void agentMutationSucceeded(const QString& session);
    void conversationChanged();
    void voicesChanged();
    void orchestratorChanged();
    void modelCatalogChanged();
    void agentRevisionChanged();
    void avatarRevisionChanged();
    void mediaChanged();
    void pastSessionsChanged();
    void pathsChanged();
    void launchDefaultsChanged();
    void hasStoredCredentialChanged();
    void composerFocusPaneChanged();
    void composerRevisionChanged();
    void draftChanged(const QString& session, const QString& text,
                      const QString& originPaneId);
    void updatesChanged();
    void teamsChanged();
    void turnQueueChanged();
    void profileChanged();
    void settingsStatusChanged();

  private:
    void requestSnapshot();
    void requestAvatars();
    void clearAvatarCache();
    void requestRecoverableClips(const QString& session);
    void requestTail(const QString& session = {}, bool replace = false);
    void requestDelta(const QString& session = {});
    [[nodiscard]] bool beginLogRequest(const QString& session, const QString& mode);
    void continuePendingLogRequest(const QString& session);
    void resetTransientRequestState();
    void storeComposerAttachments(const QString& session, const QVariantList& attachments);
    void setConnecting(bool connecting);
    void setConnectionState(const QString& state);
    void setErrorMessage(const QString& message);
    void handleJson(const QString& tag, const QJsonObject& object);
    void handleBytes(const QString& tag, const QByteArray& bytes, const QByteArray& contentType);
    void handleRequestFailure(const QString& tag, const QString& message, int statusCode);
    void handleSseEvent(const QJsonObject& event);
    void finishUpdateRequest(quint64 generation);
    void refreshSelectedProperties();
    void sendMessageInternal(const QString& targetSession, const QString& text, bool queueIfBusy,
                             const QString& traceId, const QString& transcriptionId,
                             bool handsFree);
    [[nodiscard]] ConversationModel* ensureConversation(const QString& session);
    void connectConversationSignals(ConversationModel* model, const QString& session);
    void scheduleConversationCache(const QString& session);
    [[nodiscard]] QString defaultToken() const;

    ApiClient m_api;
    SseClient m_sse;
    CredentialStore m_credentials;
    TranscriptCache m_transcriptCache;
    QTemporaryDir m_mediaDirectory;
    AudioController m_audio;
    AgentListModel m_agents;
    AgentListModel m_archivedAgents;
    ContactListModel m_contacts;
    PaneTreeModel m_panes;
    VoiceListModel m_voices;
    ConversationModel m_emptyConversation;
    ConversationModel* m_conversation = nullptr;
    QHash<QString, ConversationModel*> m_conversations;
    QString m_baseUrl;
    QString m_bearerToken;
    QString m_selectedSession;
    QString m_connectionState = QStringLiteral("offline");
    QString m_errorMessage;
    QString m_serverName;
    QString m_serverVersion;
    QString m_voiceBio;
    QString m_voiceSession;
    QVariantMap m_orchestratorSettings;
    QVariantMap m_modelCatalog;
    QVariantList m_pastSessions;
    QVariantList m_directorySuggestions;
    QVariantList m_favoritePaths;
    QVariantList m_availableMcpServers;
    QString m_lastWorkingDirectory;
    QString m_lastBackend;
    QString m_orchestratorLastDecision;
    QString m_composerFocusPane;
    QString m_sharedFilesystemHostOverride;
    QHash<QString, QTimer*> m_deliveryTimers;
    QHash<QString, QTimer*> m_cacheTimers;
    QHash<QString, QString> m_deliverySessions;
    QSet<QString> m_logRequestsInFlight;
    QSet<QString> m_pendingUpdateActions;
    QHash<QString, QString> m_pendingLogMode;
    QHash<QString, QUrl> m_avatarSources;
    QHash<QString, QString> m_avatarUrls;
    QHash<QString, QPair<QString, QString>> m_avatarRequests;
    QHash<QString, QString> m_avatarFailures;
    QHash<QString, QVariantList> m_mediaAssets;
    QHash<QString, QUrl> m_mediaSources;
    QHash<QString, quint64> m_mediaGenerations;
    QHash<QString, QVariantMap> m_mediaListRequests;
    QHash<QString, QVariantMap> m_mediaContentRequests;
    QHash<QString, QVariantMap> m_pendingUploads;
    QVariantList m_attentionItems;
    QVariantList m_backgroundJobs;
    QVariantList m_updateArtifacts;
    QString m_updatesError;
    QVariantList m_teams;
    QVariantList m_teamMessages;
    QString m_selectedTeamId;
    QString m_teamsError;
    QVariantList m_turnQueueItems;
    QString m_turnQueueSession;
    QString m_turnQueueError;
    QHash<QString, QString> m_queueActionSessions;
    QVariantMap m_profileTaskPlan;
    QVariantMap m_profileHeartbeat;
    QVariantMap m_diagnosticsHealth;
    QVariantMap m_transcriptionCapabilities;
    QVariantMap m_ttsProviderStatus;
    QVariantList m_profilePrompts;
    QString m_profileSession;
    QString m_profileError;
    QString m_profilePromptCursor;
    QHash<QString, QVariantMap> m_promptHistoryRequests;
    QHash<QString, QVariantMap> m_toolDetailRequests;
    bool m_connecting = false;
    bool m_sending = false;
    bool m_muted = false;
    bool m_toolsVisible = false;
    bool m_timestampsVisible = false;
    bool m_sharedFilesystem = false;
    bool m_voicesLoading = false;
    bool m_orchestratorLoading = false;
    bool m_pastSessionsLoading = false;
    bool m_hasStoredCredential = false;
    quint64 m_agentRevision = 0;
    quint64 m_avatarRevision = 0;
    quint64 m_mediaRevision = 0;
    quint64 m_composerRevision = 0;
    quint64 m_nextAvatarRequest = 0;
    int m_updateRequestsPending = 0;
    quint64 m_updatesGeneration = 0;
    quint64 m_teamListGeneration = 0;
    quint64 m_teamMessagesGeneration = 0;
    quint64 m_turnQueueGeneration = 0;
    quint64 m_profileGeneration = 0;
    quint64 m_promptHistoryGeneration = 0;
    quint64 m_settingsStatusGeneration = 0;
    int m_settingsStatusPending = 0;
    bool m_teamListLoading = false;
    bool m_teamMessagesLoading = false;
    bool m_turnQueuePaused = false;
    bool m_turnQueueLoading = false;
    bool m_profileLoading = false;
    bool m_profilePromptsHaveMore = false;
    bool m_profilePromptsLoading = false;
    bool m_cacheEnabled = true;
};

} // namespace clarp
