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

## UNO Q application tooling

The production UNO Q service is a native Debian ARM64 binary with embedded web
assets. Rust and Node are build-time tools only and are not required on the
deployed UNO Q.

| Tool | Required version | Host asset | Purpose | Download |
|---|---|---|---|---|
| Rust | 1.93.1 | Windows ARM64 development host / Linux ARM64 builder | Backend, protocol, DSP, and API build | [Rust 1.93.1](https://blog.rust-lang.org/2026/02/12/Rust-1.93.1/) |
| Cargo | 1.93.1 | Installed with Rust | Rust dependency and build frontend | [rustup](https://rustup.rs/) |
| Node.js | 24.18.0 | Windows development host | Svelte dashboard build | [Node.js downloads](https://nodejs.org/en/download) |
| npm | 11.16.0 | Installed with Node.js | Locked frontend dependencies | [npm CLI](https://github.com/npm/cli) |
| Svelte | 5.56.8 | npm package | Dashboard component runtime/compiler | [npm](https://www.npmjs.com/package/svelte/v/5.56.8) |
| Vite | 6.4.3 | npm package | Dashboard development and production bundling | [npm](https://www.npmjs.com/package/vite/v/6.4.3) |
| TypeScript | 5.7.3 | npm package | Dashboard type checking | [npm](https://www.npmjs.com/package/typescript/v/5.7.3) |
| FlatBuffers JS | 25.9.23 | npm package, reserved for generated schemas | Browser binary telemetry decoder | [npm](https://www.npmjs.com/package/flatbuffers/v/25.9.23) |
| FlatBuffers Rust | 25.12.19 | Cargo crate | Server binary telemetry encoder | [crates.io](https://crates.io/crates/flatbuffers/25.12.19) |
| Zig | 0.15.2 | Windows ARM64 / Linux ARM64 | Cross compiler/linker on the Snapdragon host and native linker on UNO Q | [Zig 0.15.2](https://ziglang.org/download/0.15.2/release-notes.html) |

Exact application dependency checksums are locked by `unoq/Cargo.lock` and
`unoq/dashboard/package-lock.json`. Before acquiring an ARM64 build container
or standalone `flatc`, record its immutable digest or archive SHA-256 here and
retain the redistributable artifact under `tools/installers/linux-arm64/`.

### Repeatable Windows ARM64 build path

The UNO Q service must be built on Windows, not on the deployed board. The
tracked wrappers under `unoq/tools/` use Rust
`1.93.1-aarch64-pc-windows-gnullvm` and Zig `0.15.2` for two distinct links:

| Script | Purpose |
|---|---|
| `unoq/tools/test-host.ps1` | Runs Rust tests as Windows ARM64 executables. |
| `unoq/tools/test-linux-arm64.ps1` | Cross-compiles test binaries for UNO Q without running them on Windows. |
| `unoq/tools/build-linux-arm64.ps1 -Release` | Builds `heimdall-service` for `aarch64-unknown-linux-gnu`. |
| `unoq/tools/build-windows-server.ps1` | Finite optimized native Windows server build. |
| `unoq/tools/install-windows-server-task.ps1` | Stops, atomically deploys, and starts the direct executable as a Windows logon task. |
| `unoq/tools/install-windows-firewall.ps1` | Creates elevated TCP/UDP rules for all hotspot firewall profiles. |
| `unoq/tools/ensure-wireguard-endpoint-route.ps1` | Pins the current WireGuard endpoint through the physical Wi-Fi gateway so Mobile Hotspot cannot capture the tunnel transport. |
| `unoq/tools/enable-direct-ethernet.ps1` | Assigns the laptop side of the dedicated UNO Q Ethernet link to `192.168.250.1/30` without a gateway or DNS. |
| `unoq/tools/run-windows-server.ps1` | Runs an already-built server in the foreground for diagnostics. |

The Rust target `aarch64-unknown-linux-gnu` must be installed for the pinned
toolchain. The scripts expect Zig at
`tools/installers/windows-arm64/zig-aarch64-windows-0.15.2/zig.exe`; set the
per-session `HEIMDALL_ZIG` environment variable to an extracted cached copy
elsewhere when necessary. `zig-host-link.ps1` is required because Cargo also
builds Windows ARM64 proc-macros/build scripts before it can cross-link Linux.

## Radar-map host tooling

The experimental replay mapper under `host-tools/radar-map/` runs downstream
of the UNO Q. It deliberately uses the standard library for HTTP serving and
keeps its required numerical dependency small.

| Tool | Required version | Host asset | Purpose | Download |
|---|---|---|---|---|
| NumPy | 2.2.6 | CPython 3.10 Windows x86-64 wheel, validated under ARM64 emulation; no Windows ARM64 wheel is published for this release | Voxel arrays, CIR statistics, and `.npy` export | [official wheel](https://files.pythonhosted.org/packages/a3/dd/4b822569d6b96c39d1215dbae0582fd99954dcbcf0c1a13c61783feaca3f/numpy-2.2.6-cp310-cp310-win_amd64.whl) |

The validated wheel is cached under `windows-x86_64/`. Zarr export is optional
and is not yet a pinned deployment dependency.

## Neural-recon host tooling

The `neural-recon/` subfolder runs on the same validated CPython 3.10 Windows
x86-64 interpreter as radar-map (under ARM64 emulation). Exact pins are in
`neural-recon/requirements.lock`; torch is the CPU-only PyPI Windows build
(CUDA training host is a Phase 6 decision point).

| Tool | Required version | Host asset | Purpose | Download |
|---|---|---|---|---|
| NumPy | 2.2.6 | CPython 3.10 Windows x86-64 wheel (cached, same asset as radar-map) | Array numerics | [official wheel](https://files.pythonhosted.org/packages/a3/dd/4b822569d6b96c39d1215dbae0582fd99954dcbcf0c1a13c61783feaca3f/numpy-2.2.6-cp310-cp310-win_amd64.whl) |
| SciPy | 1.15.3 | CPython 3.10 Windows x86-64 wheel | Hungarian assignment, signal processing | [PyPI](https://pypi.org/project/scipy/1.15.3/) |
| PyTorch | 2.13.0 (CPU) | CPython 3.10 Windows x86-64 wheel | Differentiable renderer and network | [PyPI](https://pypi.org/project/torch/2.13.0/) |
| PyYAML | 6.0.3 | CPython 3.10 Windows x86-64 wheel (cached, same asset as west setup) | Config files | [PyPI](https://pypi.org/project/PyYAML/6.0.3/) |
| pytest | 9.1.1 | pure-Python wheel | Test entry | [PyPI](https://pypi.org/project/pytest/9.1.1/) |

## Vast.ai host tooling

`tools/venv-vastai/` (gitignored) holds the `vastai` CLI used for the Phase 6
compute-host decision (renting CUDA instances for curriculum runs 2-4). It is
a Python package installed into a dedicated virtual environment; no
host-native binary is required. Invoke it as
`& tools/venv-vastai/Scripts/vastai.exe ...`. The API key is supplied via the
`vastai set api-key` command; do not store the key in the repository.

| Tool | Required version | Host asset | Purpose | Download |
|---|---|---|---|---|
| vastai | 1.5.2 | pure-Python wheel | Rent/provision CUDA training instances | [PyPI](https://pypi.org/project/vastai/1.5.2/) |

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
| `windows-arm64/zig-aarch64-windows-0.15.2.zip` | 0.15.2 | Windows ARM64 | `B926465F8872BF983422257CD9EC248BB2B270996FBE8D57872CCA13B56FC370` | Official UNO Q Linux ARM64 cross-linker |
| `windows-x86_64/cmake-3.31.10-py3-none-win_amd64.whl` | 3.31.10 | Windows x86-64 | `F1EA1FE826355560E8976C3D5794D9357444209BC0E0D56676C71E6A571FD474` | Pinned CMake |
| `windows-x86_64/numpy-2.2.6-cp310-cp310-win_amd64.whl` | 2.2.6 | CPython 3.10 Windows x86-64 | `F0FD6321B839904E15C46E0D257FDD101DD7F530FE03FD6359C1EA63738703F3` | Radar-map numerical runtime under Windows ARM64 emulation |
| `windows-x86_64/dtc-1.6.1-msys2-x86_64.zip` | 1.6.1 | Windows x86-64 | `7AAC366F989FD2450D5E641E118734653EA29D0DDB7DBFA33521D57AFE852AE3` | DeviceTree compiler |
| `windows-x86_64/zephyr-sdk-0.17.4_windows-x86_64_minimal.7z` | 0.17.4 | Windows x86-64 | `3A1B7DE85811296A7193D010882A61D8AF1DDA7B2319AF30EB04665F6BBF1F99` | SDK base archive |
| `windows-x86_64/toolchain_windows-x86_64_arm-zephyr-eabi.7z` | 0.17.4 | Windows x86-64 | `22F8BE7A2762A5FE7C9C0F465F79F5E6ABAC204CC13405C9F243D8596C10B08D` | ARM compiler |
| `windows-arm64/JLink_Windows_V962_arm64.exe` | 9.62 | Windows ARM64 | `0C79A68C64FF654787A31CFEBAA8D2A93CC3D8D7F67AF2D4558831C18B489F8B` | SEGGER J-Link installer |
| `windows-x86_64/JLink_Windows_V962_x86_64.exe` | 9.62 | Windows x86-64 | `50F44E977285D76D45BB0BAEBE4C7867C96E6C9167112248093C3B18D7A7A137` | SEGGER fallback installer |
| `windows-x86_64/zadig-2.9.exe` | 2.9 | Windows x86-64 | `4ECAA95DF3DA3621486A043AEF8B3050B8BAFE7C901402871E816229EF82039B` | WinUSB workaround; use only for J-Link MI_02 |
| `linux-arm64/JLink_Linux_V962_arm64.deb` | 9.62 | Linux ARM64 | `F4BD3F3DC7EAD379EB9BC7BDF858CF8B1296FB82573B5A04B5A7248AA8877F74` | UNO Q J-Link package |
| `linux-arm64/rustup-init-aarch64-unknown-linux-gnu` | current rustup bootstrap, fetched 2026-07-27 | Linux ARM64 | `9732D6C5E2A098D3521FCA8145D826AE0AAA067EF2385EAD08E6FEAC88FA5792` | Official Rust bootstrap; install pinned Rust 1.93.1 |
| `common-python/torch-2.13.0-cp310-cp310-win_amd64.whl` | 2.13.0 | CPython 3.10 Windows x86-64 | `2BD30B6B730D987FA386CE3898933762C5CB8CC82EB0535211D787CC3CE2DFEB` | Neural-recon CPU-only torch |
| `common-python/vastai-1.5.2-py3-none-any.whl` | 1.5.2 | Python | `1BFC01EA5020D83086EFD5BAFCE8A82A7B548034327FAFD8F0E9C2113271ABA3` | Vast.ai CLI for Phase 6 CUDA host |
| `common-python/scipy-1.15.3-cp310-cp310-win_amd64.whl` | 1.15.3 | CPython 3.10 Windows x86-64 | `9D61E97B186A57350F6D6FD72640F9E99D5A4A2B8FBF4B9EE9A841EAB327DC13` | Neural-recon scipy |
| `common-python/pytest-9.1.1-py3-none-any.whl` | 9.1.1 | Python | `37A86B45EFB9A47A61A36449063E8E18D0CAB3161329FC099EB21783169C4F0C` | Neural-recon test entry |
| `linux-arm64/zig-aarch64-linux-0.15.2.tar.xz` | 0.15.2 | Linux ARM64 | `958ED7D1E00D0EA76590D27666EFBF7A932281B3D7BA0C6B01B0FF26498F667F` | Native compiler/linker without root installation |

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
