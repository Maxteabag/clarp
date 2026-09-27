#include "launcher/InstanceForward.h"

#include <array>
#include <cerrno>
#include <charconv>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <poll.h>
#include <span>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <vector>

namespace clarp::launcher {
namespace {

constexpr int ReplyTimeoutMs = 2'000;
constexpr uint32_t FnvOffset = 2166136261U;
constexpr uint32_t FnvPrime = 16777619U;

std::string getenvString(const char* name) {
    const char* value = std::getenv(name);
    return value == nullptr ? std::string{} : std::string{value};
}

void appendHash(uint32_t& hash, std::string_view value) {
    for (const char character : value) {
        const auto byte = static_cast<unsigned char>(character);
        hash ^= byte;
        hash *= FnvPrime;
    }
}

std::string hexHash(uint32_t hash) {
    std::array<char, 8> buffer{};
    constexpr std::string_view digits = "0123456789abcdef";
    for (int index = 7; index >= 0; --index) {
        buffer.at(static_cast<size_t>(index)) = digits.at(hash & 0x0fU);
        hash >>= 4U;
    }
    return {buffer.data(), buffer.size()};
}

void appendBigEndianLength(std::string& payload, size_t length) {
    payload.push_back(static_cast<char>((length >> 24U) & 0xffU));
    payload.push_back(static_cast<char>((length >> 16U) & 0xffU));
    payload.push_back(static_cast<char>((length >> 8U) & 0xffU));
    payload.push_back(static_cast<char>(length & 0xffU));
}

} // namespace

std::string currentExecutablePath() {
    std::array<char, 4096> path{};
    const ssize_t length = readlink("/proc/self/exe", path.data(), path.size() - 1);
    if (length <= 0) return {};
    return {path.data(), static_cast<size_t>(length)};
}

std::string realExecutablePath(std::string_view launcherPath) {
    const size_t slash = launcherPath.rfind('/');
    const std::string directory = slash == std::string_view::npos
        ? std::string{"."}
        : std::string{launcherPath.substr(0, slash)};
    return directory + "/clarp-desktop-real";
}

bool runsAlone(const std::vector<std::string>& arguments) {
    static constexpr std::array separateFlags{"-h", "--help", "--help-all", "-v", "--version",
                                              "--preview-versions"};
    for (const std::string& argument : arguments) {
        for (const char* flag : separateFlags) {
            if (argument == flag) return true;
        }
    }
    return !getenvString("CLARP_SEPARATE_PROCESS").empty()
        || !getenvString("CLARP_SCREENSHOT_PATH").empty()
        || getenvString("CLARP_RESTORE_DESKTOP") == "1";
}

std::string instanceSocketPathForExecutable(
    const std::string& executablePath, const std::vector<std::string>& arguments) {
    const std::string runtime = getenvString("XDG_RUNTIME_DIR");
    if (runtime.empty() || executablePath.empty() || runsAlone(arguments)) return {};
    struct stat info {};
    if (stat(executablePath.c_str(), &info) != 0) return {};
    uint32_t hash = FnvOffset;
    appendHash(hash, executablePath);
    appendHash(hash, std::string_view{"\0", 1});
    appendHash(hash, std::to_string(static_cast<long long>(info.st_mtim.tv_sec)));
    appendHash(hash, ".");
    appendHash(hash, std::to_string(static_cast<long long>(info.st_mtim.tv_nsec)));
    for (const char* name : {"CLARP_INSTANCE_NAME", "CLARP_BASE_URL", "CLARP_TOKEN",
                             "CLARP_SHARED_FILESYSTEM_HOST", "XDG_CONFIG_HOME", "WAYLAND_DISPLAY",
                             "DISPLAY", "QT_QPA_PLATFORM", "CLARP_RENDERER", "QT_QUICK_BACKEND"}) {
        appendHash(hash, std::string_view{"\0", 1});
        appendHash(hash, name);
        appendHash(hash, "=");
        appendHash(hash, getenvString(name));
    }
    return runtime + "/clarp-desktop-" + hexHash(hash) + ".sock";
}

ForwardResult forwardToRunningInstance(
    const std::string& socketPath, const std::vector<std::string>& arguments) {
    if (socketPath.empty()) return ForwardResult::NoServer;
    sockaddr_un address{};
    if (socketPath.size() >= sizeof(address.sun_path)) return ForwardResult::NoServer;
    address.sun_family = AF_UNIX;
    std::memcpy(static_cast<void*>(&address.sun_path[0]), socketPath.data(), socketPath.size());
    const int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return ForwardResult::NoServer;
    const auto closeAndReturn = [fd](ForwardResult result) {
        close(fd);
        return result;
    };
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast): POSIX socket API
    if (connect(fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0)
        return closeAndReturn(ForwardResult::NoServer);

    std::string body;
    for (const std::string& argument : arguments) {
        body += argument;
        body.push_back('\0');
    }
    std::string payload;
    appendBigEndianLength(payload, body.size());
    payload += body;
    size_t written = 0;
    while (written < payload.size()) {
        const std::span remaining(payload.data(), payload.size());
        const auto unwritten = remaining.subspan(written);
        const ssize_t sent = send(fd, unwritten.data(), unwritten.size(), MSG_NOSIGNAL);
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) return closeAndReturn(ForwardResult::FailedAfterConnect);
        written += static_cast<size_t>(sent);
    }
    pollfd wait{.fd = fd, .events = POLLIN, .revents = 0};
    if (poll(&wait, 1, ReplyTimeoutMs) <= 0) return closeAndReturn(ForwardResult::FailedAfterConnect);
    std::array<char, 8> reply{};
    const ssize_t received = recv(fd, reply.data(), reply.size(), 0);
    return closeAndReturn(received >= 2 && reply[0] == 'o' && reply[1] == 'k'
        ? ForwardResult::Accepted
        : ForwardResult::FailedAfterConnect);
}

int runLauncher(std::span<char*> argv) {
    std::vector<std::string> arguments;
    for (char* argument : argv.subspan(1))
        arguments.emplace_back(argument);
    const std::string realBinary = realExecutablePath(currentExecutablePath());
    if (!runsAlone(arguments)) {
        const ForwardResult forwarded =
            forwardToRunningInstance(instanceSocketPathForExecutable(realBinary, arguments), arguments);
        if (forwarded == ForwardResult::Accepted) return 0;
        if (forwarded == ForwardResult::FailedAfterConnect) return 1;
    }
    std::vector<std::string> execStorage;
    execStorage.reserve(arguments.size() + 1U);
    execStorage.push_back(realBinary);
    for (const std::string& argument : arguments)
        execStorage.push_back(argument);
    std::vector<char*> execArguments;
    execArguments.reserve(execStorage.size() + 1U);
    for (std::string& argument : execStorage)
        execArguments.push_back(argument.data());
    execArguments.push_back(nullptr);
    execve(realBinary.c_str(), execArguments.data(), environ);
    return 127;
}

} // namespace clarp::launcher
