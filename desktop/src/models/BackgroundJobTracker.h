#pragma once

#include <QHash>
#include <QJsonObject>
#include <QList>
#include <QObject>
#include <QString>

namespace clarp {

struct BackgroundJobCounts {
    int total = 0;
    int subAgents = 0;

    friend bool operator==(const BackgroundJobCounts&, const BackgroundJobCounts&) = default;
};

/// Active background jobs per agent, fed by `GET /background-jobs` and the
/// `background-job-updated` SSE event. Only queued and running jobs are kept:
/// the agent list and chat header show what is still running, not history.
class BackgroundJobTracker : public QObject {
    Q_OBJECT

  public:
    explicit BackgroundJobTracker(QObject* parent = nullptr);

    // A full `/background-jobs` response replaces everything known so far.
    void applyList(const QJsonObject& response);
    // Returns true when the event changed the active set.
    bool applyEvent(const QJsonObject& event);
    void clear();

    // False until the first list arrives; callers then fall back to the
    // snapshot's `background_jobs` counts.
    [[nodiscard]] bool loaded() const { return m_loaded; }
    [[nodiscard]] QList<QJsonObject> activeJobs(const QString& agentId,
                                                const QString& session = {}) const;
    [[nodiscard]] QHash<QString, BackgroundJobCounts> countsByAgent() const;

    [[nodiscard]] static bool isActiveStatus(const QString& status);
    [[nodiscard]] static bool isSubAgent(const QJsonObject& job);

  signals:
    void changed();

  private:
    QHash<QString, QJsonObject> m_active;
    bool m_loaded = false;
};

} // namespace clarp
