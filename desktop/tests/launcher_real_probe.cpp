#include <filesystem>
#include <iostream>
#include <span>

int main(int argc, char* argv[]) { // NOLINT(cppcoreguidelines-avoid-c-arrays,modernize-avoid-c-arrays)
    const std::span arguments(argv, static_cast<size_t>(argc));
    std::cout << "real-probe argc=" << argc
              << " argv0=" << std::filesystem::path(arguments.front()).filename().string();
    for (int index = 1; index < argc; ++index) {
        std::cout << " argv" << index << "=" << arguments[static_cast<size_t>(index)];
    }
    std::cout << '\n';
    return 0;
}
