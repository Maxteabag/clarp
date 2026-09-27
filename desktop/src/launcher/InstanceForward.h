#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace clarp::launcher {

enum class ForwardResult {
    Accepted,
    NoServer,
    FailedAfterConnect,
};

[[nodiscard]] std::string realExecutablePath(std::string_view launcherPath);
[[nodiscard]] std::string currentExecutablePath();
[[nodiscard]] bool runsAlone(const std::vector<std::string>& arguments);
[[nodiscard]] std::string instanceSocketPathForExecutable(
    const std::string& executablePath, const std::vector<std::string>& arguments);
[[nodiscard]] ForwardResult forwardToRunningInstance(
    const std::string& socketPath, const std::vector<std::string>& arguments);
[[nodiscard]] int runLauncher(int argc, char* argv[]);

} // namespace clarp::launcher
