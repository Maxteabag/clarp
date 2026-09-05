#include "media/PortraitImage.h"
#include <QBuffer>
#include <QImageReader>
#include <QPainter>
#include <algorithm>

namespace clarp {
QByteArray roundedPortrait(const QByteArray& bytes) {
    QBuffer input;
    input.setData(bytes);
    if (!input.open(QIODevice::ReadOnly)) return {};
    QImageReader reader(&input);
    const QSize size = reader.size();
    if (!size.isValid() || size.width() > 4096 || size.height() > 4096) return {};
    reader.setAutoTransform(true);
    reader.setScaledSize(size.scaled(QSize(384, 384), Qt::KeepAspectRatio));
    const QImage source = reader.read();
    if (source.isNull()) return {};
    QImage result(192, 192, QImage::Format_ARGB32_Premultiplied);
    result.fill(Qt::transparent);
    QImage mask(result.size(), result.format());
    mask.fill(Qt::transparent);
    { QPainter painter(&mask);
      painter.setRenderHint(QPainter::Antialiasing);
      painter.setPen(Qt::NoPen);
      painter.setBrush(Qt::white);
      painter.drawEllipse(QRectF(0, 0, 192, 192)); }
    { QPainter painter(&result);
      painter.setRenderHint(QPainter::SmoothPixmapTransform);
      const int side = std::min(source.width(), source.height());
      painter.drawImage(QRect(0, 0, 192, 192), source,
          QRect((source.width() - side) / 2, (source.height() - side) / 2, side, side));
      painter.setCompositionMode(QPainter::CompositionMode_DestinationIn);
      painter.drawImage(0, 0, mask); }
    QByteArray png;
    QBuffer output(&png);
    if (!output.open(QIODevice::WriteOnly) || !result.save(&output, "PNG")) return {};
    return png;
}
}
