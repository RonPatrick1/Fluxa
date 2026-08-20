cmake_minimum_required(VERSION 3.20)
project(fluxa LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(OpenSSL REQUIRED)
find_package(SQLite3 REQUIRED)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(HTTPLIB REQUIRED IMPORTED_TARGET cpp-httplib)

add_executable(fluxa-server fluxa.cpp)
target_compile_options(fluxa-server PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(fluxa-server PRIVATE PkgConfig::HTTPLIB SQLite::SQLite3 OpenSSL::Crypto Threads::Threads)

install(TARGETS fluxa-server RUNTIME DESTINATION bin)
