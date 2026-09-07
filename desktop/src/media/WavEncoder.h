#pragma once

#include <QAudioFormat>
#include <QByteArray>

namespace clarp {

[[nodiscard]] QByteArray encodeWav(const QByteArray& pcm, const QAudioFormat& format);

} // namespace clarp
