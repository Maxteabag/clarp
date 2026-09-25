#pragma once
#include "ReadingTheme.h"
#include <QPalette>

namespace clarp {
// Set every control role in every state from a reading theme; an inactive
// window or popup must not fall back to the platform theme. Basic uses
// `dark` for checked control fills and `brightText` for checked button text.
inline QPalette desktopPalette(QPalette inherited, const ReadingTheme& theme) {
    const auto set = [&inherited](QPalette::ColorRole role, const QString& color) {
        inherited.setColor(QPalette::All, role, QColor(color));
    };
    set(QPalette::Window, theme.window);
    set(QPalette::Base, theme.window);
    set(QPalette::AlternateBase, theme.raised);
    set(QPalette::Button, theme.control);
    set(QPalette::ToolTipBase, theme.control);
    for (const auto role : {QPalette::WindowText, QPalette::Text, QPalette::ButtonText,
                           QPalette::ToolTipText}) set(role, theme.chromeText);
    set(QPalette::PlaceholderText, theme.mutedText);
    set(QPalette::BrightText, theme.accentText);
    set(QPalette::Highlight, theme.accent);
    set(QPalette::Accent, theme.accent);
    set(QPalette::HighlightedText, theme.accentText);
    set(QPalette::Link, theme.link);
    set(QPalette::LinkVisited, theme.accent);
    set(QPalette::Light, theme.light ? QStringLiteral("#d8cbaa") : QStringLiteral("#565b76"));
    set(QPalette::Midlight, theme.border);
    set(QPalette::Mid, theme.border);
    set(QPalette::Dark, theme.accent);
    set(QPalette::Shadow, theme.shadow);
    for (const auto role : {QPalette::WindowText, QPalette::Text, QPalette::ButtonText})
        inherited.setColor(QPalette::Disabled, role, QColor(theme.mutedText));
    return inherited;
}

inline QPalette desktopPalette(QPalette inherited) {
    return desktopPalette(std::move(inherited), readingTheme(defaultReadingThemeId()));
}
}
