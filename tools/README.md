# Tool Installer Cache

This directory documents the host tools needed to build and flash Heimdall
firmware. The `installers/` directory is intentionally ignored by Git so the
downloaded installers and archives can be copied between machines without
being pushed to GitHub.

## Local layout

```text
tools/
  README.md
  installers/
    windows-arm64/
    windows-x86_64/
```

Put each downloaded file in the subdirectory matching its host architecture.
The firmware target architecture is ARM Cortex-M4; that is separate from the
architecture of the computer running the tools.

## Windows ARM64 setup inventory

The current tested host is ARM64 Windows. Some tools are native ARM64, while
the Zephyr SDK and selected utilities are Windows x86-64 binaries running under
Windows emulation.

| Tool | Required version | Host asset | Purpose | Download |
|---|---|---|---|---|
| Python | 3.12 recommended | Windows ARM64 | Zephyr and west Python runtime | [python.org downloads](https://www.python.org/downloads/windows/) |
| west | 1.5.0 | Python package | Workspace and build frontend | [PyPI](https://pypi.org/project/west/1.5.0/) |
| CMake | 3.31.10 | Windows x86-64 package used by this setup | Generate build files | [CMake v3.31.10](https://github.com/Kitware/CMake/releases/tag/v3.31.10) |
| Ninja | 1.13.x | Windows ARM64 | Build executor | [Ninja releases](https://github.com/ninja-build/ninja/releases) |
| DTC | 1.6.x | Windows x86-64 package used by this setup | Compile DeviceTree | [oss-winget DTC](https://github.com/oss-winget/oss-winget-storage) |
| 7-Zip | 24.x or newer | Windows ARM64 | Extract Zephyr archives | [7-Zip](https://www.7-zip.org/download.html) |
| wget | 1.21.x | Windows ARM64 | Zephyr SDK setup dependency | [wget for Windows](https://eternallybored.org/misc/wget/) |
| Zephyr SDK | 0.17.4 | Windows x86-64 | ARM compiler and SDK tools | [SDK release](https://github.com/zephyrproject-rtos/sdk-ng/releases/tag/v0.17.4) |
| ARM toolchain | SDK 0.17.4 | Windows x86-64 | `arm-zephyr-eabi` compiler | [SDK release assets](https://github.com/zephyrproject-rtos/sdk-ng/releases/tag/v0.17.4) |
| J-Link software | Current compatible release | Windows ARM64 or x86-64 | Flash and debug through J9 | [SEGGER downloads](https://www.segger.com/downloads/jlink/) |
| nrfutil | 8.2.0 if needed | Python package | Optional Nordic flashing/package tools | [PyPI](https://pypi.org/project/nrfutil/) |

## Zephyr SDK asset names

For SDK 0.17.4 on Windows, the known assets are:

```text
zephyr-sdk-0.17.4_windows-x86_64_minimal.7z
toolchain_windows-x86_64_arm-zephyr-eabi.7z
```

The minimal archive alone does not contain the ARM compiler. Keep both files in
`tools/installers/windows-x86_64/`.

## Recording a downloaded file

Add a row to the table below whenever an installer is placed in the local
cache. Do not add the installer itself to Git.

| File | Version | Architecture | SHA-256 | Notes |
|---|---|---|---|---|
| _not yet populated_ |  |  |  |  |

On PowerShell, calculate a checksum with:

```powershell
Get-FileHash .\tools\installers\windows-x86_64\filename.7z -Algorithm SHA256
```

## Using the cache on another machine

1. Copy the repository and the ignored `tools/installers/` directory together.
2. Install native host tools for the new computer's architecture where
   available.
3. Use the recorded x86-64 assets when no host-native asset exists; Windows
   ARM can run them under emulation.
4. Install Python and create a dedicated virtual environment.
5. Install west and the Zephyr Python requirements into that same environment.
6. Run `west update` from `firmware/` to restore the pinned source modules.
7. Register the cached Zephyr SDK and build using the commands in
   `docs/firmware-onboarding.md`.

Never put GitHub tokens, SSH keys, `.env` files, or other secrets in this cache.
