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
    common-python/
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
| Ninja | 1.13.2 | Windows ARM64 | Build executor | [Ninja releases](https://github.com/ninja-build/ninja/releases) |
| DTC | 1.6.x | Windows x86-64 package used by this setup | Compile DeviceTree | [oss-winget DTC](https://github.com/oss-winget/oss-winget-storage) |
| 7-Zip | 26.02 cached; 24.09 used | Windows ARM64 | Extract Zephyr archives | [7-Zip](https://www.7-zip.org/download.html) |
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

## Cached files

The following files are currently present in the local ignored cache. Do not
add the installer files themselves to Git.

| File | Version | Architecture | SHA-256 | Notes |
|---|---|---|---|---|
| `common-python/west-1.5.0-py3-none-any.whl` | 1.5.0 | Python | `F71411D11ED9ED00847405C6A83600374C2E0CF8676ED5AF5D8B572F92C4765A` | Workspace tool |
| `common-python/pyserial-3.5-py2.py3-none-any.whl` | 3.5 | Python | `C4451DB6BA391CA6CA299FB3EC7BAE67A5C55DDE170964C7A14CEEFEC02F2CF0` | Host serial tools |
| `common-python/patool-4.0.1-py2.py3-none-any.whl` | 4.0.1 | Python | `A7430EB08EDCBD71FEAF9C40F55C46F6A0AC385DC68DD0F5010CFA4AD2E9341A` | SDK extraction helper |
| `common-python/jsonschema-4.26.0-py3-none-any.whl` | 4.26.0 | Python | `D489F15263B8D200F8387E64B4C3A75F06629559FB73DEB8FDFB525F2DAB50CE` | Zephyr board discovery |
| `common-python/pykwalify-1.8.0-py2.py3-none-any.whl` | 1.8.0 | Python | `731DFA87338CCA9F559D1FCA2BDEA37299116E3139B73F78CA90A543722D6651` | west dependency |
| `common-python/docopt-0.6.2.tar.gz` | 0.6.2 | Python source | `49B3A825280BD66B3AA83585EF59C4A8C82F2C8A522DBE754A8BC8D08C85C491` | pykwalify dependency |
| `common-python/pyyaml-6.0.3-cp310-cp310-win_amd64.whl` | 6.0.3 | CPython 3.10 x86-64 | `BDB2C67C6C1390B63C6FF89F210C8FD09D9A1217A465701EAC7316313C915E4C` | Regenerate for Python 3.12 |
| `common-python/packaging-26.2-py3-none-any.whl` | 26.2 | Python | `5FC45236B9446107FF2415CE77C807CEE2862CB6FAC22B8A73826D0693B0980E` | west dependency |
| `common-python/colorama-0.4.6-py2.py3-none-any.whl` | 0.4.6 | Python | `4F1D9991F5ACC0CA119F9D443620B77F9D6B33703E51011C16BAF57AFB285FC6` | west dependency |
| `common-python/ruamel_yaml-0.19.1-py3-none-any.whl` | 0.19.1 | Python | `27592957FEDF6E0B62F281E96EFFD28043345E0E66001F97683AA9A40C667C93` | pykwalify dependency |
| `windows-arm64/7z2602-arm64.exe` | 26.02 | Windows ARM64 | `7C6FDE79ED5E11B81C7BB6573B7962D3B6322AA5FCE69C33ED19F672B55173AB` | Archive extractor |
| `windows-arm64/ninja-1.13.2-winarm64.zip` | 1.13.2 | Windows ARM64 | `E52F0BDEF9DFB1003229DBD6508A508C4073FD017247002ADC66E5E806CB0391` | Build executor |
| `windows-arm64/wget-1.21.4-winarm64.exe` | 1.21.4 | Windows ARM64 | `356DF847B5BE2478B74ECBE9AE0B2150EF328B10073F93B0E1719E4C88BADA02` | SDK setup dependency |
| `windows-x86_64/cmake-3.31.10-py3-none-win_amd64.whl` | 3.31.10 | Windows x86-64 | `F1EA1FE826355560E8976C3D5794D9357444209BC0E0D56676C71E6A571FD474` | Pinned CMake |
| `windows-x86_64/dtc-1.6.1-msys2-x86_64.zip` | 1.6.1 | Windows x86-64 | `7AAC366F989FD2450D5E641E118734653EA29D0DDB7DBFA33521D57AFE852AE3` | DeviceTree compiler |
| `windows-x86_64/zephyr-sdk-0.17.4_windows-x86_64_minimal.7z` | 0.17.4 | Windows x86-64 | `3A1B7DE85811296A7193D010882A61D8AF1DDA7B2319AF30EB04665F6BBF1F99` | SDK base archive |
| `windows-x86_64/toolchain_windows-x86_64_arm-zephyr-eabi.7z` | 0.17.4 | Windows x86-64 | `22F8BE7A2762A5FE7C9C0F465F79F5E6ABAC204CC13405C9F243D8596C10B08D` | ARM compiler |

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
