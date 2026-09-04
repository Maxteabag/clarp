#include "media/WavEncoder.h"

#include <QBuffer>
#include <QDataStream>
#include <limits>

namespace clarp {

QByteArray encodeWav(const QByteArray& pcm, const QAudioFormat& format) {
    if (!format.isValid() || pcm.isEmpty() || format.bytesPerSample() <= 0 ||
        pcm.size() > std::numeric_limits<quint32>::max() - 44U) {
        return {};
    }

    QByteArray wav;
    QBuffer buffer(&wav);
    if (!buffer.open(QIODevice::WriteOnly)) {
        return {};
    }
    QDataStream stream(&buffer);
    stream.setByteOrder(QDataStream::LittleEndian);

    const quint16 formatCode = format.sampleFormat() == QAudioFormat::Float ? 3U : 1U;
    const quint16 channels = static_cast<quint16>(format.channelCount());
    const quint32 sampleRate = static_cast<quint32>(format.sampleRate());
    const quint16 bitsPerSample = static_cast<quint16>(format.bytesPerSample() * 8);
    const quint16 blockAlign = static_cast<quint16>(channels * format.bytesPerSample());
    const quint32 byteRate = sampleRate * blockAlign;
    const quint32 dataSize = static_cast<quint32>(pcm.size());

    stream.writeRawData("RIFF", 4);
    stream << static_cast<quint32>(36U + dataSize);
    stream.writeRawData("WAVE", 4);
    stream.writeRawData("fmt ", 4);
    stream << static_cast<quint32>(16U);
    stream << formatCode;
    stream << channels;
    stream << sampleRate;
    stream << byteRate;
    stream << blockAlign;
    stream << bitsPerSample;
    stream.writeRawData("data", 4);
    stream << dataSize;
    stream.writeRawData(pcm.constData(), pcm.size());
    return wav;
}

} // namespace clarp
