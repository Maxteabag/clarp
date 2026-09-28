cmake_minimum_required(VERSION 3.24)
project(ClarpJobReducerProbe LANGUAGES CXX)
find_package(Qt6 REQUIRED COMPONENTS Core)
set(CMAKE_AUTOMOC ON)
add_executable(job-reducer
    background_job_reducer.cpp
    ../../desktop/src/models/BackgroundJobTracker.cpp
    ../../desktop/src/models/BackgroundJobTracker.h)
target_compile_features(job-reducer PRIVATE cxx_std_20)
target_include_directories(job-reducer PRIVATE ../../desktop/src)
target_link_libraries(job-reducer PRIVATE Qt6::Core)
