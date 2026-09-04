#pragma once

#include "app/CredentialStore.h"
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
    Q_PROPERTY(quint64 agentRevision READ agentRevision NOTIFY agentRevisionChanged)
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
    Q_PROPERTY(QString connectionState READ connectionState NOTIFY connectionStateChanged)
    Q_PROPERTY(QString errorMessage READ errorMessage NOTIFY errorMessageChanged)
    Q_PROPERTY(QString serverName READ serverName NOTIFY serverInfoChanged)
    Q_PROPERTY(QString serverVersion READ serverVersion NOTIFY serverInfoChanged)

  public:
    explicit AppController(QObject* parent = nullptr);

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
    [[nodiscard]] quint64 agentRevision() const;
    [[nodiscard]] QVariantList pastSessions() const;
    [[nodiscard]] bool pastSessionsLoading() const;
    [[nodiscard]] QVariantList directorySuggestions() const;
    [[nodiscard]] QVariantList favoritePaths() const;
    [[nodiscard]] QString lastWorkingDirectory() const;
    [[nodiscard]] QString lastBackend() const;
    [[nodiscard]] bool hasStoredCredential() const;
    Q_INVOKABLE [[nodiscard]] ConversationModel* conversationForSession(const QString& session);
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
    [[nodiscard]] QString connectionState() const;
    [[nodiscard]] QString errorMessage() const;
    [[nodiscard]] QString serverName() const;
    [[nodiscard]] QString serverVersion() const;

    void setBaseUrl(const QString& value);
    void setMuted(bool muted);
    void setToolsVisible(bool visible);

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
    Q_INVOKABLE void stopAgent();
    Q_INVOKABLE void stopSession(const QString& session);
    Q_INVOKABLE void refreshSession(const QString& session);
    Q_INVOKABLE void loadOlderSession(const QString& session);
    Q_INVOKABLE [[nodiscard]] QString agentName(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentState(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentBackend(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentWorkingDirectory(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentModel(const QString& session) const;
    Q_INVOKABLE [[nodiscard]] QString agentEffort(const QString& session) const;
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
                                 const QString& mode = {}, const QString& pastSessionId = {});
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

  signals:
    void baseUrlChanged();
    void selectedSessionChanged();
    void selectedAgentChanged();
    void connectedChanged();
    void connectingChanged();
    void sendingChanged();
    void mutedChanged();
    void toolsVisibleChanged();
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
    void pastSessionsChanged();
    void pathsChanged();
    void launchDefaultsChanged();
    void hasStoredCredentialChanged();

  private:
    void requestSnapshot();
    void requestRecoverableClips(const QString& session);
    void requestTail(const QString& session = {});
    void requestDelta(const QString& session = {});
    [[nodiscard]] bool beginLogRequest(const QString& session, const QString& mode);
    void continuePendingLogRequest(const QString& session);
    void setConnecting(bool connecting);
    void setConnectionState(const QString& state);
    void setErrorMessage(const QString& message);
    void handleJson(const QString& tag, const QJsonObject& object);
    void handleRequestFailure(const QString& tag, const QString& message, int statusCode);
    void handleSseEvent(const QJsonObject& event);
    void refreshSelectedProperties();
    void sendMessageInternal(const QString& targetSession, const QString& text, bool queueIfBusy,
                             const QString& traceId, const QString& transcriptionId,
                             bool handsFree);
    [[nodiscard]] ConversationModel* ensureConversation(const QString& session);
    void connectConversationSignals(ConversationModel* model, const QString& session);
    [[nodiscard]] QString defaultToken() const;

    ApiClient m_api;
    SseClient m_sse;
    CredentialStore m_credentials;
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
    QString m_lastWorkingDirectory;
    QString m_lastBackend;
    QString m_orchestratorLastDecision;
    QHash<QString, QTimer*> m_deliveryTimers;
    QHash<QString, QString> m_deliverySessions;
    QSet<QString> m_logRequestsInFlight;
    QHash<QString, QString> m_pendingLogMode;
    bool m_connecting = false;
    bool m_sending = false;
    bool m_muted = false;
    bool m_toolsVisible = false;
    bool m_voicesLoading = false;
    bool m_orchestratorLoading = false;
    bool m_pastSessionsLoading = false;
    bool m_hasStoredCredential = false;
    quint64 m_agentRevision = 0;
};

} // namespace clarp
