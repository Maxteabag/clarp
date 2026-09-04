add_executable(clarp-desktop-tests
    tst_native_core.cpp
    ../src/app/CredentialStore.cpp
    ../src/app/CredentialStore.h
    ../src/app/AppController.cpp
    ../src/app/AppController.h
    ../src/app/TranscriptCache.cpp
    ../src/app/TranscriptCache.h
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
