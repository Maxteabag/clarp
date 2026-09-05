#pragma once
#include <QPalette>

namespace clarp {
inline QPalette desktopPalette(QPalette inherited) {
    // Set every control role in every state; an inactive window or popup must
    // not fall back to a light desktop theme.
    const auto set = [&inherited](QPalette::ColorRole role, const char* color) {
        inherited.setColor(QPalette::All, role, QColor(QString::fromLatin1(color)));
    };
    set(QPalette::Window, "#1a1b26");
    set(QPalette::Base, "#1a1b26");
    set(QPalette::AlternateBase, "#20212e");
    set(QPalette::Button, "#292b3a");
    set(QPalette::ToolTipBase, "#292b3a");
    for (const auto role : {QPalette::WindowText, QPalette::Text, QPalette::ButtonText,
                           QPalette::ToolTipText, QPalette::BrightText}) set(role, "#c0caf5");
    set(QPalette::PlaceholderText, "#8d93b0");
    set(QPalette::BrightText, "#1a1b26");
    set(QPalette::Highlight, "#bb9af7");
    set(QPalette::Accent, "#bb9af7");
    set(QPalette::HighlightedText, "#1a1b26");
    set(QPalette::Link, "#7aa2f7");
    set(QPalette::LinkVisited, "#bb9af7");
    set(QPalette::Light, "#565b76");
    set(QPalette::Midlight, "#41445a");
    set(QPalette::Mid, "#41445a");
    set(QPalette::Dark, "#bb9af7");
    set(QPalette::Shadow, "#14151d");
    for (const auto role : {QPalette::WindowText, QPalette::Text, QPalette::ButtonText})
        inherited.setColor(QPalette::Disabled, role, QColor(QStringLiteral("#8d93b0")));
    return inherited;
}
}
