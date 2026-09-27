#include "launcher/InstanceForward.h"

int main(int argc, char* argv[]) { // NOLINT(cppcoreguidelines-avoid-c-arrays,modernize-avoid-c-arrays)
    return clarp::launcher::runLauncher({argv, static_cast<size_t>(argc)});
}
