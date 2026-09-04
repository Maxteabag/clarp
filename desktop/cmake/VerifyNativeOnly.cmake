if(NOT EXISTS "${CLARP_DESKTOP_BINARY}")
    message(FATAL_ERROR "Desktop executable does not exist: ${CLARP_DESKTOP_BINARY}")
endif()

file(GET_RUNTIME_DEPENDENCIES
    EXECUTABLES "${CLARP_DESKTOP_BINARY}"
    RESOLVED_DEPENDENCIES_VAR dependencies
    UNRESOLVED_DEPENDENCIES_VAR unresolved_dependencies
)

foreach(dependency IN LISTS dependencies unresolved_dependencies)
    if(dependency MATCHES "Qt6(WebEngine|WebChannel)|webkit|chromium")
        message(FATAL_ERROR "Web runtime dependency is forbidden: ${dependency}")
    endif()
endforeach()

file(GLOB_RECURSE forbidden_assets
    LIST_DIRECTORIES false
    "${CLARP_DESKTOP_SOURCE}/*.html"
    "${CLARP_DESKTOP_SOURCE}/*.css"
    "${CLARP_DESKTOP_SOURCE}/*.js"
    "${CLARP_DESKTOP_SOURCE}/*.mjs"
    "${CLARP_DESKTOP_SOURCE}/*.ts"
    "${CLARP_DESKTOP_SOURCE}/*.tsx"
)
if(forbidden_assets)
    list(JOIN forbidden_assets "\n  " formatted_assets)
    message(FATAL_ERROR "Web assets are forbidden under desktop/:\n  ${formatted_assets}")
endif()
