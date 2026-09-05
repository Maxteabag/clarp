add_executable(clarp-desktop-tests
    tst_native_core.cpp
    ../src/terminal/TerminalLaunch.cpp
    ../src/app/CredentialStore.cpp
    ../src/app/CredentialStore.h
    ../src/app/AppController.cpp
    ../src/app/AppController.h
    ../src/app/TranscriptCache.cpp
    ../src/app/TranscriptCache.h
    ../src/app/ToolNarrator.cpp
    ../src/app/ToolNarrator.h
    ../src/media/AudioController.cpp
    ../src/media/AudioController.h
    ../src/media/WavEncoder.cpp
    ../src/media/WavEncoder.h
    ../src/models/AgentListModel.cpp
    ../src/models/AgentListModel.h
    ../src/models/ConversationModel.cpp
    ../src/models/ConversationModel.h
    ../src/models/ContactListModel.cpp
    ../src/models/ContactListModel.h
    ../src/models/PaneTreeModel.cpp
    ../src/models/PaneTreeModel.h
    ../src/network/SseParser.cpp
    ../src/network/SseParser.h
    ../src/network/ApiClient.cpp
    ../src/network/ApiClient.h
    ../src/network/SseClient.cpp
    ../src/network/SseClient.h
    ../src/protocol/ProtocolTypes.cpp
    ../src/protocol/ProtocolTypes.h
    ../src/models/VoiceListModel.cpp
    ../src/models/VoiceListModel.h
)

target_include_directories(clarp-desktop-tests PRIVATE ../src)
target_compile_definitions(clarp-desktop-tests PRIVATE
    CLARP_CONTRACT_DIR="${CMAKE_SOURCE_DIR}/../contract"
)
target_link_libraries(clarp-desktop-tests PRIVATE
    Qt6::Core
    Qt6::DBus
    Qt6::Multimedia
    Qt6::Network
    Qt6::Qml
    Qt6::Test
)
clarp_apply_quality_settings(clarp-desktop-tests)

add_executable(clarp-tool-narrator-tests tst_tool_narrator.cpp
    ../src/app/ToolNarrator.cpp ../src/app/ToolNarrator.h)
target_include_directories(clarp-tool-narrator-tests PRIVATE ../src)
target_link_libraries(clarp-tool-narrator-tests PRIVATE Qt6::Core Qt6::Test Qt6::Qml)
clarp_apply_quality_settings(clarp-tool-narrator-tests)
add_test(NAME clarp-desktop-tool-narrator COMMAND clarp-tool-narrator-tests)
set_tests_properties(clarp-desktop-tool-narrator PROPERTIES TIMEOUT 20)

add_test(NAME clarp-desktop-core COMMAND clarp-desktop-tests)
set_tests_properties(clarp-desktop-core PROPERTIES
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software"
)

add_test(
    NAME clarp-desktop-native-only
    COMMAND ${CMAKE_COMMAND}
        -DCLARP_DESKTOP_BINARY=$<TARGET_FILE:clarp-desktop>
        -DCLARP_DESKTOP_SOURCE=${CMAKE_SOURCE_DIR}
        -P ${CMAKE_SOURCE_DIR}/cmake/VerifyNativeOnly.cmake
)

# Exercise the actual window command and SplitView layout with an offline fixture.
foreach(sidebar_toggles IN ITEMS 1 2)
    set(sidebar_test "clarp-desktop-sidebar-${sidebar_toggles}")
    file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/${sidebar_test}/config")
    add_test(NAME ${sidebar_test} COMMAND clarp-desktop)
    set_tests_properties(${sidebar_test} PROPERTIES
        TIMEOUT 15
        ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=${sidebar_test};XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/${sidebar_test}/config;CLARP_SCREENSHOT_SCENARIO=markdown;CLARP_SCREENSHOT_SIDEBAR_TOGGLES=${sidebar_toggles};CLARP_SCREENSHOT_REQUIRE_COMPOSER_FOCUS=1;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/${sidebar_test}/capture.png"
    )
endforeach()
set_property(TEST clarp-desktop-sidebar-2 APPEND PROPERTY ENVIRONMENT
    "CLARP_SCREENSHOT_SIDEBAR_WIDTH=310")

find_program(CLARP_QML_TEST_RUNNER qmltestrunner HINTS "${QT6_INSTALL_PREFIX}/lib/qt6/bin" REQUIRED)
add_test(NAME clarp-desktop-activity-layout
    COMMAND ${CLARP_QML_TEST_RUNNER} -input "${CMAKE_CURRENT_SOURCE_DIR}/qml")
set_tests_properties(clarp-desktop-activity-layout PROPERTIES
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software"
    TIMEOUT 20)

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/tool-presentation/config")
add_test(NAME clarp-desktop-tool-presentation COMMAND clarp-desktop)
set_tests_properties(clarp-desktop-tool-presentation PROPERTIES
    TIMEOUT 15
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=clarp-desktop-tool-presentation;XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/tool-presentation/config;CLARP_SCREENSHOT_SCENARIO=tool-spacing;CLARP_SCREENSHOT_EXPAND_TOOLS=1;CLARP_SCREENSHOT_REQUIRE_COMPOSER_FOCUS=1;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/tool-presentation/capture.png"
)
