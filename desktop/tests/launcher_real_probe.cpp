#include <filesystem>
#include <iostream>

int main(int argc, char* argv[]) {
    std::cout << "real-probe argc=" << argc
              << " argv0=" << std::filesystem::path(argv[0]).filename().string();
    for (int index = 1; index < argc; ++index) {
        std::cout << " argv" << index << "=" << argv[index];
    }
    std::cout << '\n';
    return 0;
}
