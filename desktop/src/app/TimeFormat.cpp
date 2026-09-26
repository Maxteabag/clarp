#include "app/TimeFormat.h"

#include <QLocale>
#include <algorithm>

namespace clarp {
namespace {

QDateTime parseIso(const QString& timestamp) {
    if (timestamp.isEmpty()) {
        return {};
    }
    QDateTime parsed = QDateTime::fromString(timestamp, Qt::ISODateWithMs);
    if (!parsed.isValid()) {
        parsed = QDateTime::fromString(timestamp, Qt::ISODate);
    }
    return parsed.isValid() ? parsed.toLocalTime() : QDateTime{};
}

QString dayHeading(const QDate& day, const QDate& today) {
    if (day == today) {
        return QStringLiteral("Today");
    }
    if (day == today.addDays(-1)) {
        return QStringLiteral("Yesterday");
    }
    const QLocale locale;
    if (day.year() == today.year()) {
        return locale.toString(day, QStringLiteral("d MMMM"));
    }
    return locale.toString(day, QLocale::ShortFormat);
}

} // namespace

QString chatStamp(qint64 epochMillis, const QDateTime& now) {
    if (epochMillis <= 0) {
        return {};
    }
    const QDateTime moment = QDateTime::fromMSecsSinceEpoch(epochMillis).toLocalTime();
    const QDate day = moment.date();
    const QDate today = now.date();
    const QLocale locale;

    if (day == today) {
        return locale.toString(moment.time(), QLocale::ShortFormat);
    }
    if (day == today.addDays(-1)) {
        return QStringLiteral("Yesterday");
    }
    if (day.daysTo(today) < 7 && day < today) {
        return locale.dayName(day.dayOfWeek(), QLocale::LongFormat);
    }
    if (day.year() == today.year()) {
        return locale.toString(day, QStringLiteral("d MMM"));
    }
    return locale.toString(day, QLocale::ShortFormat);
}

QString clockTime(const QString& timestamp) {
    const QDateTime moment = parseIso(timestamp);
    if (!moment.isValid()) {
        return {};
    }
    return QLocale().toString(moment.time(), QLocale::ShortFormat);
}

QString daySeparator(const QString& timestamp, const QString& previousTimestamp,
                     const QDateTime& now) {
    const QDateTime moment = parseIso(timestamp);
    if (!moment.isValid()) {
        return {};
    }
    const QDateTime previous = parseIso(previousTimestamp);
    if (previous.isValid() && previous.date() == moment.date()) {
        return {};
    }
    return dayHeading(moment.date(), now.date());
}

QString compactDuration(qint64 milliseconds) {
    const qint64 seconds = std::max<qint64>(0, milliseconds / 1000);
    if (seconds < 60) return QStringLiteral("%1s").arg(seconds);
    const qint64 minutes = seconds / 60;
    if (minutes < 60) return QStringLiteral("%1m").arg(minutes);
    const qint64 hours = minutes / 60;
    if (hours < 24) return QStringLiteral("%1h %2m").arg(hours).arg(minutes % 60, 2, 10, QLatin1Char('0'));
    return QStringLiteral("%1d").arg(hours / 24);
}

} // namespace clarp
