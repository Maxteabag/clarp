function(clarp_apply_quality_settings target)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /permissive-)
        if(CLARP_WARNINGS_AS_ERRORS)
            target_compile_options(${target} PRIVATE /WX)
        endif()
    else()
        target_compile_options(${target} PRIVATE
            -Wall
            -Wextra
            -Wpedantic
            -Wconversion
            -Wsign-conversion
            -Wshadow
            -Wnon-virtual-dtor
            -Wold-style-cast
            -Woverloaded-virtual
        )
        if(CLARP_WARNINGS_AS_ERRORS)
            target_compile_options(${target} PRIVATE -Werror)
        endif()
    endif()

    if(CLARP_ENABLE_SANITIZERS)
        if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            message(FATAL_ERROR "Sanitizer preset requires Clang or GCC")
        endif()
        target_compile_options(${target} PRIVATE
            -fsanitize=address,undefined
            -fno-omit-frame-pointer
        )
        target_link_options(${target} PRIVATE -fsanitize=address,undefined)
    endif()

    if(CLARP_ENABLE_CLANG_TIDY)
        find_program(CLARP_CLANG_TIDY clang-tidy REQUIRED)
        set_property(TARGET ${target} PROPERTY CXX_CLANG_TIDY
            "${CLARP_CLANG_TIDY};--warnings-as-errors=*"
        )
    endif()
endfunction()
