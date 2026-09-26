#include "app/StallMonitor.h"
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QStandardPaths>
#include <array>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <execinfo.h>
#include <fcntl.h>
#include <pthread.h>
#include <unistd.h>

namespace clarp {
namespace {
constexpr int MaxFrames = 64;
// Filled by the signal handler on the GUI thread, read by the watchdog.
// A signal handler can only reach process-wide state, so these stay global.
std::array<void*, MaxFrames> gFrames{};  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
std::atomic<int> gFrameCount{0};         // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
std::atomic<bool> gCaptured{false};      // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
const int StackSignal = SIGRTMIN + 7;

void captureStack(int /*signal*/) {
    // backtrace() is warmed up in the constructor, so this does not allocate.
    gFrameCount.store(backtrace(gFrames.data(), MaxFrames));
    gCaptured.store(true);
}

std::int64_t nowNs() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

int openLogForAppend(const QByteArray& path) {
    // POSIX open() is variadic; plain file descriptors keep the write path
    // free of Qt objects that could allocate while the GUI thread is stuck.
    return ::open(path.constData(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);  // NOLINT(cppcoreguidelines-pro-type-vararg)
}

void rotate(const QString& path) {
    const QFileInfo info(path);
    if (info.exists() && info.size() > 4 * 1024 * 1024) {
        QFile::remove(path + QStringLiteral(".1"));
        QFile::rename(path, path + QStringLiteral(".1"));
    }
}
}  // namespace

QString StallMonitor::defaultLogPath() {
    QString base = QStandardPaths::writableLocation(QStandardPaths::GenericStateLocation);
    if (base.isEmpty()) base = QDir::homePath() + QStringLiteral("/.local/state");
    return QDir(base).filePath(QStringLiteral("clarp/desktop-stalls.log"));
}

StallMonitor::StallMonitor(int thresholdMs, QString logPath, QObject* parent, int memoryMb)
    : QObject(parent), m_thresholdMs(thresholdMs), m_memoryMb(memoryMb),
      m_logPath(logPath.isEmpty() ? defaultLogPath() : std::move(logPath)) {
    if (m_thresholdMs <= 0) return;
    QDir().mkpath(QFileInfo(m_logPath).absolutePath());
    // Warm backtrace() so its first call (which loads libgcc) never happens
    // inside the signal handler.
    std::array<void*, 4> warm{};
    backtrace(warm.data(), static_cast<int>(warm.size()));
    struct sigaction action {};
    action.sa_handler = captureStack;
    sigemptyset(&action.sa_mask);
    action.sa_flags = SA_RESTART;
    sigaction(StackSignal, &action, nullptr);

    m_guiThread = pthread_self();
    m_lastBeatNs.store(nowNs());
    m_beat.setInterval(20);
    connect(&m_beat, &QTimer::timeout, this, [this] { m_lastBeatNs.store(nowNs()); });
    m_beat.start();
    m_running.store(true);
    m_watchdog = std::thread([this] { watch(); });
}

StallMonitor::~StallMonitor() {
    m_running.store(false);
    if (m_watchdog.joinable()) m_watchdog.join();
}

namespace {
long residentMb() {
    QFile statm(QStringLiteral("/proc/self/statm"));
    if (!statm.open(QIODevice::ReadOnly)) return 0;
    const QList<QByteArray> fields = statm.readAll().simplified().split(' ');
    if (fields.size() < 2) return 0;
    bool ok = false;
    const long residentPages = fields.at(1).toLong(&ok);
    return ok ? residentPages * (sysconf(_SC_PAGESIZE) / 1024) / 1024 : 0;
}
}  // namespace

bool StallMonitor::captureGuiStack() const {
    gCaptured.store(false);
    pthread_kill(m_guiThread, StackSignal);
    for (int wait = 0; wait < 20 && !gCaptured.load(); ++wait)
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    return gCaptured.load();
}

void StallMonitor::watch() {
    bool inStall = false;
    std::int64_t stallStartNs = 0;
    long nextMemoryMb = m_memoryMb;
    int tick = 0;
    while (m_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
        if (m_memoryMb > 0 && ++tick % 20 == 0) {
            const long rss = residentMb();
            if (rss >= nextMemoryMb) {
                writeStall(rss, captureGuiStack() ? gFrameCount.load() : 0, "memory");
                nextMemoryMb = rss + 512;
            }
        }
        const std::int64_t beat = m_lastBeatNs.load();
        const std::int64_t gapMs = (nowNs() - beat) / 1'000'000;
        if (!inStall && gapMs > m_thresholdMs) {
            inStall = true;
            stallStartNs = beat;
            writeStall(gapMs, captureGuiStack() ? gFrameCount.load() : 0);
        } else if (inStall && beat != stallStartNs) {
            // The GUI thread beat again: the stall is over.
            inStall = false;
            const std::int64_t totalMs = (beat - stallStartNs) / 1'000'000;
            m_stalls.fetch_add(1);
            int longest = m_longestMs.load();
            while (totalMs > longest && !m_longestMs.compare_exchange_weak(longest, static_cast<int>(totalMs))) {}
            writeEnd(totalMs);
        }
    }
}

void StallMonitor::writeStall(std::int64_t gapMs, int frames, const char* reason) {
    rotate(m_logPath);
    const QByteArray path = m_logPath.toLocal8Bit();
    const int fd = openLogForAppend(path);
    if (fd < 0) return;
    const bool memory = std::strcmp(reason, "memory") == 0;
    const QByteArray header = QStringLiteral("== %1 at %2 pid=%3 %4 build=%5\n")
                                  .arg(QString::fromLatin1(reason),
                                       QDateTime::currentDateTime().toString(Qt::ISODateWithMs),
                                       QString::number(QCoreApplication::applicationPid()),
                                       memory ? QStringLiteral("rss=%1MB").arg(gapMs)
                                              : QStringLiteral("blocked>=%1ms").arg(gapMs),
                                       QCoreApplication::applicationVersion())
                                  .toUtf8();
    [[maybe_unused]] const auto written = ::write(fd, header.constData(), static_cast<size_t>(header.size()));
    if (frames > 0) {
        backtrace_symbols_fd(gFrames.data(), frames, fd);
    } else {
        const QByteArray missing = QByteArrayLiteral("(stack not captured)\n");
        [[maybe_unused]] const auto none = ::write(fd, missing.constData(), static_cast<size_t>(missing.size()));
    }
    ::close(fd);
    const QByteArray notice = QStringLiteral("clarp %1: %2 %3, GUI stack in %4\n")
                                  .arg(QString::fromLatin1(reason),
                                       memory ? QStringLiteral("resident MB") : QStringLiteral("GUI thread blocked ms"),
                                       QString::number(gapMs), m_logPath)
                                  .toUtf8();
    [[maybe_unused]] const auto shown = ::write(STDERR_FILENO, notice.constData(), static_cast<size_t>(notice.size()));
}

void StallMonitor::writeEnd(std::int64_t totalMs) {
    const QByteArray line = QStringLiteral("-- stall ended after %1 ms\n").arg(totalMs).toUtf8();
    const QByteArray path = m_logPath.toLocal8Bit();
    const int fd = openLogForAppend(path);
    if (fd < 0) return;
    [[maybe_unused]] const auto written = ::write(fd, line.constData(), static_cast<size_t>(line.size()));
    ::close(fd);
}
}  // namespace clarp
