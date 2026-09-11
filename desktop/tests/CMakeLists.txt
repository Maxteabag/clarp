find_package(Python3 REQUIRED COMPONENTS Interpreter)
find_program(CLARP_DBUS_RUN_SESSION dbus-run-session REQUIRED)

add_executable(clarp-palette-tests tst_palette.cpp)
target_include_directories(clarp-palette-tests PRIVATE ../src)
target_link_libraries(clarp-palette-tests PRIVATE Qt6::Gui Qt6::Test)
clarp_apply_quality_settings(clarp-palette-tests)
add_test(NAME clarp-desktop-palette COMMAND clarp-palette-tests)

add_executable(clarp-desktop-tests
    ../src/app/PreviewVersions.cpp
    ../src/app/PreviewVersions.h
    tst_native_core.cpp
    ../src/terminal/TerminalLaunch.cpp
    ../src/app/CredentialStore.cpp
    ../src/app/CredentialStore.h
    ../src/app/AppController.cpp
    ../src/app/AppController.h
    ../src/app/WorkspaceContext.cpp
    ../src/app/WorkspaceContext.h
    ../src/app/TranscriptCache.cpp
    ../src/app/TranscriptCache.h
    ../src/app/ToolNarrator.cpp
    ../src/app/ToolNarrator.h
    ../src/app/TimeFormat.cpp
    ../src/models/AgentFilterModel.cpp
    ../src/models/AgentFilterModel.h
    ../src/media/PortraitImage.cpp
    ../src/media/AudioController.cpp
    ../src/media/AudioController.h
    ../src/media/AudioCoordinator.cpp
    ../src/media/AudioCoordinator.h
    ../src/media/RecordingSession.cpp
    ../src/media/RecordingSession.h
    ../src/media/WavEncoder.cpp
    ../src/media/WavEncoder.h
    ../src/models/AgentListModel.cpp
    ../src/models/AgentListModel.h
    ../src/models/ConversationPresentationModel.cpp
    ../src/models/ConversationPresentationModel.h
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
    Qt6::Gui
    Qt6::DBus
    Qt6::Multimedia
    Qt6::Network
    Qt6::Qml
    Qt6::Test
)
clarp_apply_quality_settings(clarp-desktop-tests)

add_executable(clarp-tool-narrator-tests tst_tool_narrator.cpp
    ../src/network/ApiClient.cpp ../src/network/ApiClient.h
    ../src/app/ToolNarrator.cpp ../src/app/ToolNarrator.h)
target_include_directories(clarp-tool-narrator-tests PRIVATE ../src)
target_link_libraries(clarp-tool-narrator-tests PRIVATE Qt6::Core Qt6::Test Qt6::Qml Qt6::Network)
clarp_apply_quality_settings(clarp-tool-narrator-tests)
add_test(NAME clarp-desktop-tool-narrator COMMAND clarp-tool-narrator-tests)
set_tests_properties(clarp-desktop-tool-narrator PROPERTIES TIMEOUT 20)

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/core/data" "${CMAKE_CURRENT_BINARY_DIR}/core/runtime")
file(CHMOD "${CMAKE_CURRENT_BINARY_DIR}/core/runtime" PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE)
add_test(NAME clarp-desktop-core COMMAND ${CLARP_DBUS_RUN_SESSION} -- $<TARGET_FILE:clarp-desktop-tests>)
set_tests_properties(clarp-desktop-core PROPERTIES
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose;XDG_DATA_HOME=${CMAKE_CURRENT_BINARY_DIR}/core/data;XDG_RUNTIME_DIR=${CMAKE_CURRENT_BINARY_DIR}/core/runtime"
)

add_test(
    NAME clarp-desktop-native-only
    COMMAND ${CMAKE_COMMAND}
        -DCLARP_DESKTOP_BINARY=$<TARGET_FILE:clarp-desktop>
        -DCLARP_DESKTOP_SOURCE=${CMAKE_SOURCE_DIR}
        -P ${CMAKE_SOURCE_DIR}/cmake/VerifyNativeOnly.cmake
)

# Exercise the actual window command and SplitView layout with an offline fixture.
string(MD5 clarp_test_instance "${CMAKE_CURRENT_BINARY_DIR}")
foreach(sidebar_toggles IN ITEMS 1 2)
    set(sidebar_test "clarp-desktop-sidebar-${sidebar_toggles}")
    file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/${sidebar_test}/config")
    add_test(NAME ${sidebar_test} COMMAND clarp-desktop)
    set_tests_properties(${sidebar_test} PROPERTIES
        TIMEOUT 15
        ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=${sidebar_test}-${clarp_test_instance};XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/${sidebar_test}/config;CLARP_SCREENSHOT_SCENARIO=markdown;CLARP_SCREENSHOT_SIDEBAR_TOGGLES=${sidebar_toggles};CLARP_SCREENSHOT_REQUIRE_COMPOSER_FOCUS=1;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/${sidebar_test}/capture.png"
    )
endforeach()
set_property(TEST clarp-desktop-sidebar-2 APPEND PROPERTY ENVIRONMENT
    "CLARP_SCREENSHOT_SIDEBAR_WIDTH=310")

find_program(CLARP_QML_TEST_RUNNER qmltestrunner HINTS "${QT6_INSTALL_PREFIX}/lib/qt6/bin" REQUIRED)
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/palette-popup/config")
string(MD5 clarp_palette_instance "${CMAKE_CURRENT_BINARY_DIR}")
add_test(NAME clarp-desktop-palette-popup COMMAND clarp-desktop)
set_tests_properties(clarp-desktop-palette-popup PROPERTIES
    TIMEOUT 15
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=palette-${clarp_palette_instance};XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/palette-popup/config;CLARP_SCREENSHOT_SCENARIO=tool-spacing;CLARP_SCREENSHOT_VIEW=orchestrator;CLARP_SCREENSHOT_OPEN_POPUP=orchestratorProvider;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/palette-popup/capture.png"
)
add_test(NAME clarp-desktop-activity-layout
    COMMAND ${CLARP_QML_TEST_RUNNER} -input "${CMAKE_CURRENT_SOURCE_DIR}/qml")
set_tests_properties(clarp-desktop-activity-layout PROPERTIES
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose"
    TIMEOUT 20)

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/tool-presentation/config")
add_test(NAME clarp-desktop-tool-presentation COMMAND clarp-desktop)
set_tests_properties(clarp-desktop-tool-presentation PROPERTIES
    TIMEOUT 15
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=clarp-desktop-tool-presentation-${clarp_test_instance};XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/tool-presentation/config;CLARP_SCREENSHOT_SCENARIO=tool-spacing;CLARP_SCREENSHOT_EXPAND_TOOLS=1;CLARP_SCREENSHOT_REQUIRE_COMPOSER_FOCUS=1;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/tool-presentation/capture.png"
)

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/settings-keyboard/config")
string(MD5 clarp_settings_keyboard_instance "${CMAKE_CURRENT_BINARY_DIR}")
add_test(NAME clarp-desktop-settings-keyboard COMMAND clarp-desktop)
set_tests_properties(clarp-desktop-settings-keyboard PROPERTIES
    TIMEOUT 15
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=settings-keyboard-${clarp_settings_keyboard_instance};XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/settings-keyboard/config;CLARP_SCREENSHOT_SCENARIO=markdown;CLARP_SCREENSHOT_SETTINGS_KEYBOARD=1;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/settings-keyboard/capture.png"
)

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/context-keyboard/config/MaxTeaBag")
# A saved expanded sidebar must not override the collapsed startup contract.
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/context-keyboard/config/MaxTeaBag/ClarpScreenshot.conf"
    "[appearance]\nsidebarVisible=true\n")
add_test(NAME clarp-desktop-context-keyboard COMMAND clarp-desktop)
set_tests_properties(clarp-desktop-context-keyboard PROPERTIES TIMEOUT 15
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose;QT_FORCE_STDERR_LOGGING=1;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=context-${clarp_settings_keyboard_instance};XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/context-keyboard/config;CLARP_SCREENSHOT_SCENARIO=preview-versions;CLARP_SCREENSHOT_CONTEXT_KEYBOARD=1;CLARP_SCREENSHOT_DELAY_MS=7000;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/context-keyboard/capture.png")

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/pair-sidebar/config")
add_test(NAME clarp-desktop-pair-sidebar COMMAND clarp-desktop)
set_tests_properties(clarp-desktop-pair-sidebar PROPERTIES TIMEOUT 15
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose;QT_FORCE_STDERR_LOGGING=1;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=pair-sidebar-${clarp_settings_keyboard_instance};XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/pair-sidebar/config;CLARP_SCREENSHOT_SCENARIO=markdown;CLARP_SCREENSHOT_PAIR_SIDEBAR=1;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/pair-sidebar/capture.png")

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/ready-reply/config")
add_test(NAME clarp-desktop-ready-reply COMMAND clarp-desktop)
set_tests_properties(clarp-desktop-ready-reply PROPERTIES TIMEOUT 15
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_IM_MODULE=compose;QT_FORCE_STDERR_LOGGING=1;CLARP_BASE_URL=http://127.0.0.1:1;CLARP_TOKEN=offline-fixture;CLARP_INSTANCE_NAME=ready-${clarp_settings_keyboard_instance};XDG_CONFIG_HOME=${CMAKE_CURRENT_BINARY_DIR}/ready-reply/config;CLARP_SCREENSHOT_SCENARIO=markdown;CLARP_SCREENSHOT_READY_REPLY=1;CLARP_SCREENSHOT_PATH=${CMAKE_CURRENT_BINARY_DIR}/ready-reply/capture.png")

find_package(Python3 COMPONENTS Interpreter)
if(Python3_Interpreter_FOUND)
    # Renders the report fixture with every remote-resource vector aimed at a
    # local listener and fails if the client touches it. Includes its own
    # control render, so a silent pass cannot come from a broken probe.
    add_test(NAME clarp-desktop-report-no-fetch
        COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/report_no_fetch.py
            $<TARGET_FILE:clarp-desktop>
            ${CMAKE_CURRENT_SOURCE_DIR}/../src/main.cpp
            ${CLARP_QML_TEST_RUNNER})
    set_tests_properties(clarp-desktop-report-no-fetch PROPERTIES TIMEOUT 180)
endif()

add_executable(clarp-desktop-presence-tests tst_desktop_presence.cpp
    ../src/platform/DesktopPresence.cpp ../src/platform/DesktopPresence.h)
target_include_directories(clarp-desktop-presence-tests PRIVATE ../src)
target_link_libraries(clarp-desktop-presence-tests PRIVATE Qt6::Quick Qt6::DBus Qt6::Test)
clarp_apply_quality_settings(clarp-desktop-presence-tests)
add_test(NAME clarp-desktop-presence COMMAND clarp-desktop-presence-tests)
set_tests_properties(clarp-desktop-presence PROPERTIES
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_FORCE_STDERR_LOGGING=1" TIMEOUT 15)

add_test(NAME clarp-desktop-multiple-instances
    COMMAND ${CLARP_DBUS_RUN_SESSION} -- ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/multiple_instances.py $<TARGET_FILE:clarp-desktop>)
set_tests_properties(clarp-desktop-multiple-instances PROPERTIES TIMEOUT 35)

add_executable(clarp-audio-coordination-probe audio_coordination_probe.cpp
    ../src/media/AudioCoordinator.cpp ../src/media/AudioCoordinator.h
    ../src/media/RecordingSession.cpp ../src/media/RecordingSession.h)
target_include_directories(clarp-audio-coordination-probe PRIVATE ../src)
target_link_libraries(clarp-audio-coordination-probe PRIVATE Qt6::Core Qt6::DBus)
clarp_apply_quality_settings(clarp-audio-coordination-probe)
add_test(NAME clarp-desktop-audio-coordination
    COMMAND ${CLARP_DBUS_RUN_SESSION} -- ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/audio_coordination.py $<TARGET_FILE:clarp-audio-coordination-probe>)
set_tests_properties(clarp-desktop-audio-coordination PROPERTIES TIMEOUT 60)

add_executable(clarp-workspace-context-tests tst_workspace_context.cpp
    ../src/app/WorkspaceContext.cpp ../src/app/WorkspaceContext.h)
target_include_directories(clarp-workspace-context-tests PRIVATE ../src)
target_link_libraries(clarp-workspace-context-tests PRIVATE Qt6::Core Qt6::Test)
clarp_apply_quality_settings(clarp-workspace-context-tests)
add_test(NAME clarp-desktop-workspace-context COMMAND clarp-workspace-context-tests)
