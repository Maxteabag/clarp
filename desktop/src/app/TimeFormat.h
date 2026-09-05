#pragma once

#include <QDateTime>
#include <QString>

namespace clarp {

/// Chat-list stamp for an epoch-milliseconds instant (the server's
/// `last_activity` convention):
///   today     -> short time    ("18:10")
///   yesterday -> "Yesterday"
///   < 7 days  -> weekday       ("Thursday")
///   this year -> day + month   ("12 Jun")
///   older     -> short date    ("12.06.2026")
/// Returns an empty string for a missing or zero instant.
[[nodiscard]] QString chatStamp(qint64 epochMillis,
                                const QDateTime& now = QDateTime::currentDateTime());

/// Short clock time for an ISO-8601 message timestamp, or an empty string when
/// the timestamp is missing or unparseable.
[[nodiscard]] QString clockTime(const QString& timestamp);

/// Heading for the day a message belongs to ("Today", "Yesterday", "12 June",
/// or a short date), or an empty string when it falls on the same day as
/// `previousTimestamp`. An unparseable timestamp never opens a day.
[[nodiscard]] QString daySeparator(const QString& timestamp, const QString& previousTimestamp,
                                   const QDateTime& now = QDateTime::currentDateTime());

} // namespace clarp
