#pragma once

#include "protocol/ProtocolTypes.h"
#include <QStringList>

namespace clarp {
struct TerminalLaunch {
    QString program;
    QStringList arguments;
    QString error;
};
[[nodiscard]] TerminalLaunch nativeTerminalLaunch(const Agent& agent);
} // namespace clarp
