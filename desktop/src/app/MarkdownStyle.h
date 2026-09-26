#pragma once
#include <QColor>
#include <QString>
#include <QTextDocument>
#include <QVariantMap>

namespace clarp {
// How a chat message's Markdown should look once Qt has imported it. Qt's
// importer scales headings by its own HTML defaults (h1 about twice the body
// size) and leaves code, quotes, lists and tables unstyled; this restyles the
// document in place with sizes that suit a chat column.
struct MarkdownStyleOptions {
    int bodyPixelSize = 15;
    QString monoFamily = QStringLiteral("JetBrains Mono");
    QColor codeBackground = QColor(QStringLiteral("#20212e"));
    QColor codeText;                 // Invalid keeps the body colour.
    QColor quoteText = QColor(QStringLiteral("#9ca1bd"));
    QColor link = QColor(QStringLiteral("#82aaff"));
    QColor rule = QColor(QStringLiteral("#303342"));
    QString bodyFamily;              // Empty keeps the viewer's font.
};

MarkdownStyleOptions markdownStyleOptions(const QVariantMap& values);

// Heading sizes as a multiple of the body size: h1 1.3, h2 1.18, h3 1.08,
// then body size in bold. Returns true when the document changed.
bool applyMarkdownStyle(QTextDocument* document, const MarkdownStyleOptions& options);

// Parse, restyle and serialise in one step, so a viewer can be given text
// whose layout is already final. Restyling a TextEdit after it has been laid
// out changes its height, and a ListView then shifts every row below it,
// which is what made scrolling up through a transcript jump.
QString styledMarkdownHtml(const QString& markdown, const MarkdownStyleOptions& options);
}  // namespace clarp
