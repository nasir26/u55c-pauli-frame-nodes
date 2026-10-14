cmake_minimum_required(VERSION 3.16)
project(stim_u55c LANGUAGES CXX)

# kernel/ has real C++ sources as of Phase 2, but its C-sim testbenches
# are currently built and run directly by pytest (a plain g++ subprocess
# call per test -- see tests/test_kernel_tier2.py and
# tests/test_kernel_unit_testbenches.py), not through this file. host/
# (XRT runtime, linked against Stim via FetchContent) doesn't exist yet
# (Phase 4+) and is what actually needs a real CMake build -- this file
# gets meaningful targets then, rather than duplicating the pytest-driven
# build kernel/ already has.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
