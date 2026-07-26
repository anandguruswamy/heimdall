# Heimdall beacon configuration verification and header generation.
#
# The configuration tool is authoritative for the values that get flashed, but
# this build step independently re-derives every one of them and fails the
# configure stage on any disagreement. Formula drift between the browser tool
# and the reference model therefore becomes a build error rather than a silent
# wrong value in flashed firmware.
#
# See docs/protocol-decisions.md item 27 and contracts/beacon-v1.md.
#
# Override the configuration with:
#   west build -- -DHEIMDALL_CONFIG_FILE=/path/to/beacon-config.json

set(HEIMDALL_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../..")

set(HEIMDALL_CONFIG_FILE
    "${HEIMDALL_REPO_ROOT}/deployment/beacon-config.example.json"
    CACHE FILEPATH "Heimdall beacon configuration to build against")

set(HEIMDALL_CONFIG_TOOL "${HEIMDALL_REPO_ROOT}/tools/config/heimdall_config.py")
set(HEIMDALL_GENERATED_DIR "${CMAKE_CURRENT_BINARY_DIR}/heimdall_generated")
set(HEIMDALL_GENERATED_HEADER "${HEIMDALL_GENERATED_DIR}/heimdall_beacon_config.h")

if(NOT EXISTS "${HEIMDALL_CONFIG_FILE}")
  message(FATAL_ERROR
    "Heimdall beacon configuration not found:\n"
    "  ${HEIMDALL_CONFIG_FILE}\n"
    "Set -DHEIMDALL_CONFIG_FILE=<path> to a configuration exported by the "
    "Heimdall configuration tool.")
endif()

if(NOT EXISTS "${HEIMDALL_CONFIG_TOOL}")
  message(FATAL_ERROR "Heimdall config tool missing: ${HEIMDALL_CONFIG_TOOL}")
endif()

find_package(Python3 REQUIRED COMPONENTS Interpreter)

# Re-run configuration whenever the config or the reference model changes, so a
# stale generated header can never survive an edit to either.
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
             "${HEIMDALL_CONFIG_FILE}" "${HEIMDALL_CONFIG_TOOL}")

execute_process(
  COMMAND "${Python3_EXECUTABLE}" "${HEIMDALL_CONFIG_TOOL}"
          verify "${HEIMDALL_CONFIG_FILE}"
          --emit-header "${HEIMDALL_GENERATED_HEADER}"
  RESULT_VARIABLE HEIMDALL_CONFIG_RESULT
  OUTPUT_VARIABLE HEIMDALL_CONFIG_STDOUT
  ERROR_VARIABLE HEIMDALL_CONFIG_STDERR)

if(NOT HEIMDALL_CONFIG_RESULT EQUAL 0)
  message(FATAL_ERROR
    "Heimdall beacon configuration verification FAILED\n"
    "  configuration : ${HEIMDALL_CONFIG_FILE}\n"
    "  model         : ${HEIMDALL_CONFIG_TOOL}\n"
    "\n${HEIMDALL_CONFIG_STDERR}${HEIMDALL_CONFIG_STDOUT}")
endif()

string(STRIP "${HEIMDALL_CONFIG_STDOUT}" HEIMDALL_CONFIG_SUMMARY)
message(STATUS "Heimdall beacon config: ${HEIMDALL_CONFIG_FILE}")
foreach(_line ${HEIMDALL_CONFIG_SUMMARY})
  message(STATUS "Heimdall ${_line}")
endforeach()

target_include_directories(app PRIVATE "${HEIMDALL_GENERATED_DIR}")
