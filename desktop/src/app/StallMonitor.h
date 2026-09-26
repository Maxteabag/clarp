#pragma once
#include <QObject>
#include <QString>
#include <QTimer>
#include <atomic>
#include <cstdint>
#include <thread>

namespace clarp {
// Catches the moments the desktop feels laggy: a watchdog thread expects the
// GUI thread to beat every 20 ms. When it misses for longer than the
// threshold, the watchdog interrupts the GUI thread, captures its call stack
// right there, and appends it to the stall log; when the GUI thread comes
// back it logs how long the stall lasted. Symbol names come from the
// executable's exported symbols (ENABLE_EXPORTS) and the Qt libraries.
class StallMonitor final : public QObject {
    Q_OBJECT
  public:
    // thresholdMs <= 0 disables the monitor. logPath empty uses the default
    // under the user's state directory.
    // memoryMb > 0 also captures the GUI stack when resident memory first
    // crosses that size and every further 512 MB, so a runaway allocation is
    // caught before the launcher's 3 GiB limit kills the process.
    explicit StallMonitor(int thresholdMs, QString logPath = {}, QObject* parent = nullptr,
                          int memoryMb = 1536);
    ~StallMonitor() override;
    StallMonitor(const StallMonitor&) = delete;
    StallMonitor& operator=(const StallMonitor&) = delete;

    [[nodiscard]] QString logPath() const { return m_logPath; }
    [[nodiscard]] int stallCount() const { return m_stalls.load(); }
    [[nodiscard]] int longestStallMs() const { return m_longestMs.load(); }
    // Counters for the periodic diagnostics line; resets the longest stall.
    [[nodiscard]] int takeLongestStallMs() { return m_longestMs.exchange(0); }
    [[nodiscard]] static QString defaultLogPath();

  private:
    void watch();
    void writeStall(std::int64_t gapMs, int frames, const char* reason = "stall");
    [[nodiscard]] bool captureGuiStack() const;
    void writeEnd(std::int64_t totalMs);

    int m_thresholdMs;
    int m_memoryMb;
    QString m_logPath;
    QTimer m_beat;
    std::atomic<std::int64_t> m_lastBeatNs{0};
    std::atomic<bool> m_running{false};
    std::atomic<int> m_stalls{0};
    std::atomic<int> m_longestMs{0};
    std::thread m_watchdog;
    unsigned long m_guiThread = 0;
};
}  // namespace clarp
