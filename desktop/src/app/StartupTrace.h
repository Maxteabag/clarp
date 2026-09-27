#pragma once

#include <QByteArray>
#include <QFile>
#include <QtGlobal>
#include <ctime>
#include <unistd.h>

namespace clarp {

// CLARP_STARTUP_TRACE=1 prints startup milestones as milliseconds since the
// kernel created the process, so dynamic linking and static initialisation
// before main() are part of the number the user actually waits for.
class StartupTrace {
  public:
    static bool enabled() {
        static const bool on = qEnvironmentVariableIsSet("CLARP_STARTUP_TRACE");
        return on;
    }

    static double processAgeMs() {
        QFile stat(QStringLiteral("/proc/self/stat"));
        if (!stat.open(QIODevice::ReadOnly)) return -1;
        const QByteArray line = stat.readAll();
        // Field 22 (starttime) follows the parenthesised command name.
        const qsizetype close = line.lastIndexOf(')');
        if (close < 0) return -1;
        const QList<QByteArray> fields = line.mid(close + 2).split(' ');
        constexpr int StartTimeIndex = 22 - 3;
        if (fields.size() <= StartTimeIndex) return -1;
        const double startSeconds =
            fields.at(StartTimeIndex).toDouble() / static_cast<double>(sysconf(_SC_CLK_TCK));
        timespec now{};
        clock_gettime(CLOCK_BOOTTIME, &now);
        const double nowSeconds = static_cast<double>(now.tv_sec) + static_cast<double>(now.tv_nsec) / 1e9;
        return (nowSeconds - startSeconds) * 1000.0;
    }

    static void mark(const char* milestone) {
        if (!enabled()) return;
        // CPU time is far less sensitive to machine load than wall time.
        const auto cpuMs = [](clockid_t clock) {
            timespec value{};
            clock_gettime(clock, &value);
            return static_cast<double>(value.tv_sec) * 1000.0 + static_cast<double>(value.tv_nsec) / 1e6;
        };
        qInfo("startup %s ms=%.0f cpu=%.0f gui=%.0f", milestone, processAgeMs(),
              cpuMs(CLOCK_PROCESS_CPUTIME_ID), cpuMs(CLOCK_THREAD_CPUTIME_ID));
    }
};

} // namespace clarp
