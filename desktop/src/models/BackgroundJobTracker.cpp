#include "models/BackgroundJobTracker.h"

#include <QJsonArray>
#include <QJsonValue>
#include <algorithm>

namespace clarp {
namespace {

qint64 millis(const QJsonObject& job, const char* key) {
    const QJsonValue value = job.value(QLatin1StringView(key));
    return value.isDouble() ? value.toInteger() : 0;
}

} // namespace

BackgroundJobTracker::BackgroundJobTracker(QObject* parent) : QObject(parent) {}

bool BackgroundJobTracker::isActiveStatus(const QString& status) {
    return status == QStringLiteral("queued") || status == QStringLiteral("running");
}

bool BackgroundJobTracker::isSubAgent(const QJsonObject& job) {
    return job.value(QStringLiteral("kind")).toString() == QStringLiteral("sub-agent");
}

void BackgroundJobTracker::applyList(const QJsonObject& response) {
    QHash<QString, QJsonObject> next;
    for (const QJsonValue& value : response.value(QStringLiteral("jobs")).toArray()) {
        const QJsonObject job = value.toObject();
        const QString jobId = job.value(QStringLiteral("job_id")).toString();
        if (!jobId.isEmpty() && isActiveStatus(job.value(QStringLiteral("status")).toString())) {
            next.insert(jobId, job);
        }
    }
    const bool changedSet = !m_loaded || next != m_active;
    m_active = std::move(next);
    m_loaded = true;
    if (changedSet) emit changed();
}

bool BackgroundJobTracker::applyEvent(const QJsonObject& event) {
    QJsonObject job = event.value(QStringLiteral("job")).toObject();
    if (job.isEmpty()) job = event;
    const QString jobId = job.value(QStringLiteral("job_id")).toString(
        event.value(QStringLiteral("job_id")).toString());
    if (jobId.isEmpty()) return false;
    if (!job.contains(QStringLiteral("status")) && event.contains(QStringLiteral("status"))) {
        job.insert(QStringLiteral("status"), event.value(QStringLiteral("status")));
    }
    const auto existing = m_active.constFind(jobId);
    // Events can be replayed after a reconnect; never let an older update
    // resurrect or rewind a job the tracker already knows more recently.
    if (existing != m_active.cend() &&
        millis(job, "updated_at") < millis(*existing, "updated_at")) {
        return false;
    }
    if (isActiveStatus(job.value(QStringLiteral("status")).toString())) {
        if (existing != m_active.cend() && *existing == job) return false;
        m_active.insert(jobId, job);
    } else if (m_active.remove(jobId) == 0) {
        return false;
    }
    emit changed();
    return true;
}

void BackgroundJobTracker::clear() {
    const bool had = m_loaded || !m_active.isEmpty();
    m_active.clear();
    m_loaded = false;
    if (had) emit changed();
}

QList<QJsonObject> BackgroundJobTracker::activeJobs(const QString& agentId,
                                                    const QString& session) const {
    QList<QJsonObject> result;
    for (const QJsonObject& job : m_active) {
        const QString owner = job.value(QStringLiteral("agent_id")).toString();
        if ((!agentId.isEmpty() && owner == agentId) ||
            (owner.isEmpty() && !session.isEmpty() &&
             job.value(QStringLiteral("session")).toString() == session)) {
            result.append(job);
        }
    }
    std::ranges::sort(result, [](const QJsonObject& left, const QJsonObject& right) {
        const qint64 a = millis(left, "started_at");
        const qint64 b = millis(right, "started_at");
        if (a != b) return a < b;
        return left.value(QStringLiteral("job_id")).toString() <
               right.value(QStringLiteral("job_id")).toString();
    });
    return result;
}

QHash<QString, BackgroundJobCounts> BackgroundJobTracker::countsByAgent() const {
    QHash<QString, BackgroundJobCounts> counts;
    for (const QJsonObject& job : m_active) {
        const QString owner = job.value(QStringLiteral("agent_id")).toString();
        if (owner.isEmpty()) continue;
        BackgroundJobCounts& entry = counts[owner];
        ++entry.total;
        if (isSubAgent(job)) ++entry.subAgents;
    }
    return counts;
}

} // namespace clarp
