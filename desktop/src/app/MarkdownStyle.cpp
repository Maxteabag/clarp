#include "app/MarkdownStyle.h"
#include <QTextBlock>
#include <QTextCursor>
#include <QTextFrame>
#include <QTextList>
#include <QTextTable>
#include <algorithm>
#include <cmath>

namespace clarp {
namespace {
constexpr const char* StyledStamp = "clarpMarkdownStyle";

double headingScale(int level) {
    switch (level) {
    case 1: return 1.3;
    case 2: return 1.18;
    case 3: return 1.08;
    default: return 1.0;
    }
}

QString styleStamp(const QTextDocument* document, const MarkdownStyleOptions& options) {
    // Reformatting does not change the text, so the text plus the options is
    // enough to recognise a document this pass already styled.
    return QString::number(qHash(document->toPlainText())) + QLatin1Char('|')
        + QString::number(options.bodyPixelSize) + QLatin1Char('|') + options.monoFamily
        + QLatin1Char('|') + options.codeBackground.name(QColor::HexArgb)
        + QLatin1Char('|') + options.quoteText.name(QColor::HexArgb)
        + QLatin1Char('|') + options.link.name(QColor::HexArgb);
}

bool isCodeBlock(const QTextBlock& block) {
    const QTextBlockFormat format = block.blockFormat();
    return format.hasProperty(QTextFormat::BlockCodeLanguage) || format.hasProperty(QTextFormat::BlockCodeFence)
        || (format.nonBreakableLines() && block.charFormat().fontFixedPitch());
}

bool isQuote(const QTextBlock& block) {
    return block.blockFormat().hasProperty(QTextFormat::BlockQuoteLevel)
        && block.blockFormat().intProperty(QTextFormat::BlockQuoteLevel) > 0;
}
}  // namespace

MarkdownStyleOptions markdownStyleOptions(const QVariantMap& values) {
    MarkdownStyleOptions options;
    if (values.contains(QStringLiteral("bodyPixelSize")))
        options.bodyPixelSize = std::clamp(values.value(QStringLiteral("bodyPixelSize")).toInt(), 8, 64);
    const auto colour = [&values](const char* key, QColor& target) {
        const QString value = values.value(QString::fromLatin1(key)).toString();
        if (!value.isEmpty() && QColor::isValidColorName(value)) target = QColor(value);
    };
    colour("codeBackground", options.codeBackground);
    colour("codeText", options.codeText);
    colour("quoteText", options.quoteText);
    colour("link", options.link);
    colour("rule", options.rule);
    const QString mono = values.value(QStringLiteral("monoFamily")).toString();
    if (!mono.isEmpty()) options.monoFamily = mono;
    options.bodyFamily = values.value(QStringLiteral("bodyFamily")).toString();
    return options;
}

bool applyMarkdownStyle(QTextDocument* document, const MarkdownStyleOptions& options) {
    if (document == nullptr) return false;
    const QString stamp = styleStamp(document, options);
    if (document->property(StyledStamp).toString() == stamp) return false;
    // Format edits emit the document's change signals synchronously, and the
    // TextEdit above forwards them as textChanged, which calls back in here.
    // Stamp first so that re-entry is the no-op above, not a second pass.
    document->setProperty(StyledStamp, stamp);

    const double body = options.bodyPixelSize;
    const int codeSize = std::max(8, static_cast<int>(std::lround(body * 0.9)));
    QTextCursor cursor(document);
    cursor.beginEditBlock();
    for (QTextBlock block = document->begin(); block.isValid(); block = block.next()) {
        QTextBlockFormat blockFormat = block.blockFormat();
        const int heading = blockFormat.headingLevel();
        const bool code = isCodeBlock(block);
        const bool quote = isQuote(block);
        const bool listItem = block.textList() != nullptr;

        // Paragraph rhythm: a little air above headings, none inside code.
        blockFormat.setTopMargin(heading > 0 ? (block.previous().isValid() ? body * 0.55 : 0) : code ? 0 : body * 0.15);
        blockFormat.setBottomMargin(heading > 0 ? body * 0.2 : code ? 0 : body * 0.15);
        if (code) {
            blockFormat.setBackground(options.codeBackground);
            blockFormat.setLeftMargin(body * 0.6);
            blockFormat.setRightMargin(body * 0.4);
            blockFormat.setNonBreakableLines(true);
        } else if (quote) {
            blockFormat.setLeftMargin(body * 0.9);
        } else if (listItem) {
            blockFormat.setTopMargin(body * 0.05);
            blockFormat.setBottomMargin(body * 0.05);
        }
        cursor.setPosition(block.position());
        cursor.setBlockFormat(blockFormat);

        // Character formats: heading size, code font, quote tint, link colour.
        for (QTextBlock::iterator it = block.begin(); !it.atEnd(); ++it) {
            const QTextFragment fragment = it.fragment();
            if (!fragment.isValid()) continue;
            QTextCharFormat charFormat = fragment.charFormat();
            bool changed = false;
            if (heading > 0) {
                // Clear, not zero: the HTML exporter writes a zero adjustment as
                // font-size:medium, which then overrides the pixel size below.
                charFormat.clearProperty(QTextFormat::FontSizeAdjustment);
                charFormat.setProperty(QTextFormat::FontPixelSize, static_cast<int>(std::lround(body * headingScale(heading))));
                charFormat.setFontWeight(QFont::DemiBold);
                changed = true;
            }
            if (code || charFormat.fontFixedPitch()) {
                charFormat.setFontFamilies({options.monoFamily});
                charFormat.setProperty(QTextFormat::FontPixelSize, codeSize);
                if (!code) charFormat.setBackground(options.codeBackground);  // inline code
                if (options.codeText.isValid()) charFormat.setForeground(options.codeText);
                changed = true;
            }
            if (quote && !code) {
                charFormat.setForeground(options.quoteText);
                changed = true;
            }
            if (charFormat.isAnchor()) {
                charFormat.setForeground(options.link);
                charFormat.setFontUnderline(true);
                changed = true;
            }
            if (changed) {
                cursor.setPosition(fragment.position());
                cursor.setPosition(fragment.position() + fragment.length(), QTextCursor::KeepAnchor);
                cursor.setCharFormat(charFormat);
            }
        }
    }
    // Lists: tighter indent than Qt's default 40px.
    for (QTextBlock block = document->begin(); block.isValid(); block = block.next()) {
        if (QTextList* list = block.textList()) {
            QTextListFormat listFormat = list->format();
            listFormat.setIndent(1);
            list->setFormat(listFormat);
        }
    }
    // Tables: readable cell padding and a hairline border.
    for (QTextFrame* frame : document->rootFrame()->childFrames()) {
        if (auto* table = qobject_cast<QTextTable*>(frame)) {
            QTextTableFormat tableFormat = table->format();
            tableFormat.setCellPadding(body * 0.3);
            tableFormat.setCellSpacing(0);
            tableFormat.setBorder(0.5);
            tableFormat.setBorderBrush(options.rule);
            tableFormat.setBorderStyle(QTextFrameFormat::BorderStyle_Solid);
            table->setFormat(tableFormat);
        }
    }
    cursor.endEditBlock();
    return true;
}
QString styledMarkdownHtml(const QString& markdown, const MarkdownStyleOptions& options) {
    QTextDocument document;
    QFont font = document.defaultFont();
    font.setFamilies({options.bodyFamily.isEmpty() ? QStringLiteral("sans-serif") : options.bodyFamily});
    font.setPixelSize(options.bodyPixelSize);
    document.setDefaultFont(font);
    document.setMarkdown(markdown, QTextDocument::MarkdownDialectGitHub);
    applyMarkdownStyle(&document, options);
    // Export headings as plain paragraphs. Their size and weight already sit
    // on the text; an <h2> tag would make the viewer apply its own heading
    // scale on top of that pixel size (h2 rendered at ~1.5x instead of 1.18x).
    QTextCursor cursor(&document);
    for (QTextBlock block = document.begin(); block.isValid(); block = block.next()) {
        QTextBlockFormat format = block.blockFormat();
        if (format.headingLevel() == 0) continue;
        format.setHeadingLevel(0);
        cursor.setPosition(block.position());
        cursor.setBlockFormat(format);
    }
    return document.toHtml();
}
}  // namespace clarp
