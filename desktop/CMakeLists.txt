cmake_minimum_required(VERSION 3.22)

set(CLARP_DESKTOP_VERSION "" CACHE STRING "Clarp release version")
if(CLARP_DESKTOP_VERSION STREQUAL "")
    file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/../pyproject.toml"
        clarp_project_version_line
        REGEX "^version = \"[0-9]+\\.[0-9]+\\.[0-9]+\"$"
        LIMIT_COUNT 1
    )
    string(REGEX REPLACE
        "^version = \"([0-9]+\\.[0-9]+\\.[0-9]+)\"$"
        "\\1"
        CLARP_DESKTOP_VERSION
        "${clarp_project_version_line}"
    )
endif()
if(NOT CLARP_DESKTOP_VERSION MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+$")
    message(FATAL_ERROR "CLARP_DESKTOP_VERSION must be a semantic version")
endif()

project(ClarpDesktop VERSION ${CLARP_DESKTOP_VERSION} LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(CLARP_ENABLE_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)
option(CLARP_ENABLE_CLANG_TIDY "Run clang-tidy as part of compilation" OFF)
option(CLARP_WARNINGS_AS_ERRORS "Treat compiler warnings as errors" OFF)

find_package(Qt6 6.11 REQUIRED COMPONENTS
    Core
    DBus
    Gui
    Multimedia
    Network
    Qml
    Quick
    QuickControls2
    Svg
    Test
    Widgets
)

qt_standard_project_setup(REQUIRES 6.11)

include(cmake/ClarpQuality.cmake)

add_executable(clarp-desktop)

target_sources(clarp-desktop PRIVATE
    src/main.cpp
    src/app/AppController.cpp
    src/app/AppController.h
    src/app/CredentialStore.cpp
    src/app/CredentialStore.h
    src/app/TranscriptCache.cpp
    src/app/TranscriptCache.h
    src/media/AudioController.cpp
    src/media/AudioController.h
    src/media/WavEncoder.cpp
    src/media/WavEncoder.h
    src/models/AgentListModel.cpp
    src/models/AgentListModel.h
    src/models/ConversationModel.cpp
    src/models/ConversationModel.h
    src/models/ContactListModel.cpp
    src/models/ContactListModel.h
    src/models/PaneTreeModel.cpp
    src/models/PaneTreeModel.h
    src/models/VoiceListModel.cpp
    src/models/VoiceListModel.h
    src/network/ApiClient.cpp
    src/network/ApiClient.h
    src/network/SseClient.cpp
    src/network/SseClient.h
    src/network/SseParser.cpp
    src/network/SseParser.h
    src/platform/DesktopIntegration.cpp
    src/platform/DesktopIntegration.h
    src/platform/MprisIntegration.cpp
    src/platform/MprisIntegration.h
    src/protocol/ProtocolTypes.cpp
    src/protocol/ProtocolTypes.h
)

qt_add_qml_module(clarp-desktop
    URI Clarp.Desktop
    VERSION 1.0
    QML_FILES
        qml/Main.qml
        qml/components/AgentRail.qml
        qml/components/AgentOverview.qml
        qml/components/AgentAvatar.qml
        qml/components/Composer.qml
        qml/components/ConnectionPage.qml
        qml/components/ConversationPane.qml
        qml/components/DisplayCellCard.qml
        qml/components/MessageDelegate.qml
        qml/components/OrchestratorDialog.qml
        qml/components/PaneLeaf.qml
        qml/components/AgentProfilePanel.qml
        qml/components/QuickSwitcher.qml
        qml/components/QueueDialog.qml
        qml/components/ShortcutBar.qml
        qml/components/SettingsPanel.qml
        qml/components/StatusPill.qml
        qml/components/StartAgentDialog.qml
        qml/components/ToolCard.qml
        qml/components/TeamsPanel.qml
        qml/components/UpdatesPanel.qml
        qml/components/VoiceDialog.qml
        qml/components/Workspace.qml
    RESOURCES
        resources/clarp.svg
)

target_include_directories(clarp-desktop PRIVATE src src/app)
target_compile_definitions(clarp-desktop PRIVATE
    CLARP_DESKTOP_VERSION="${PROJECT_VERSION}"
)

target_link_libraries(clarp-desktop PRIVATE
    Qt6::Core
    Qt6::DBus
    Qt6::Gui
    Qt6::Multimedia
    Qt6::Network
    Qt6::Qml
    Qt6::Quick
    Qt6::QuickControls2
    Qt6::Svg
    Qt6::Widgets
)

clarp_apply_quality_settings(clarp-desktop)

include(GNUInstallDirs)
install(TARGETS clarp-desktop
    BUNDLE DESTINATION .
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)
install(FILES packaging/linux/com.maxteabag.Clarp.desktop
    DESTINATION ${CMAKE_INSTALL_DATADIR}/applications
)
install(FILES packaging/linux/com.maxteabag.Clarp.metainfo.xml
    DESTINATION ${CMAKE_INSTALL_DATADIR}/metainfo
    RENAME com.maxteabag.Clarp.appdata.xml
)
install(FILES resources/clarp.svg
    DESTINATION ${CMAKE_INSTALL_DATADIR}/icons/hicolor/scalable/apps
    RENAME com.maxteabag.Clarp.svg
)
install(FILES
    ${CMAKE_SOURCE_DIR}/../LICENSE.md
    ${CMAKE_SOURCE_DIR}/THIRD_PARTY_NOTICES.md
    DESTINATION ${CMAKE_INSTALL_DATADIR}/licenses/clarp-desktop
)

include(CTest)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
