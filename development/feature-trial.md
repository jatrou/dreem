# ARM feature fixture trial

The trial bundle runs the five independent file-analysis programs on synthetic
recordings, checks their output, and measures each child process. It is a
preparatory execution test for the headset. It does not install a service,
change firmware, access sensor device nodes, or open personal recordings.
The original `nano_core` source is not required for these programs.

## Build and run

On the Linux development host, install `cc`, `arm-linux-gnueabihf-gcc`, its
binutils, and `qemu-arm` for emulation. Choose a new private directory outside
the repository; its sibling archive must also be absent:

```sh
python3 development/build_feature_trial.py /private/work/feature-trial
/private/work/feature-trial/run_feature_trial.sh --emulated /usr/bin/qemu-arm
python3 -m unittest tests.test_feature_trial -v
```

The builder selects `cc` and `arm-linux-gnueabihf-gcc` explicitly, builds the
programs, and checks that the ARM files are 32-bit little-endian executables
without a dynamic interpreter. It creates expected output with the host
builds, records source/compiler/binary hashes, and rejects a source change
during the build. `base_git_commit` identifies the starting checkout commit;
the source hashes identify the exact inputs, including any uncommitted work.

The directory and `.tar.gz` archive contain six static ARM executables, a
shell runner, synthetic fixtures, reference output, the build log, a manifest,
and checksums. The archive has fixed timestamps and ownership and contains
only regular files. It contains no vendor firmware or personal recording.
The checksum list detects accidental changes; it is not a digital signature.

Once authenticated recovery access is available, place the directory in an
owner-only location on writable storage and invoke its runner without the
emulation option:

```sh
/private/writable/feature-trial/run_feature_trial.sh
```

This is an invocation example, not a claim that this path exists on the
headset. The runner uses `sha256sum`, `mktemp`, `uname`, `pidof`, `awk`, `grep`,
and `cmp` from the target's system PATH. It creates one fresh `results.*`
directory inside the bundle per invocation. Native mode requires ARMv7,
the reviewed `/usr/bin/nano_core` file hash, and exactly one recorder process.
It checks the recorder's PID and process start time again after the cases.
It neither starts nor restarts the recorder to satisfy these conditions.

## Cases and results

| Case | Required result |
| --- | --- |
| EEG quality | Successful analysis of 500 synthetic four-channel rows |
| Motion quality | Successful analysis of 100 synthetic motion rows |
| [Optical quality](optical-quality.md) | Successful analysis of 100 synthetic red/infrared rows, including zero, ceiling and out-of-range cases |
| Recorded sensor health | Successful parsing of good, bad and unknown health changes |
| Session motion | Successful cross-file alignment and health filtering |
| Recovered session | Explicit rejection with status 1 because alignment is unsupported |

Each case has a 20-second supervision deadline. A successful case must have
the expected exit status, no supervisor/exec error or interruption, exact
stdout and stderr agreement with its host reference, and unchanged bundle
checksums. An unexpected result stops the runner. Only after all six cases
pass does it write `result.json`. Partial results remain available for diagnosis.

Each `*.metrics.json` records elapsed monotonic time, child user/system CPU
time, peak resident memory in KiB, exit/signal state, and timeout/error flags.
In emulated mode these are host measurements of QEMU running the ARM program;
they are **not headset CPU, RAM, power, or battery measurements**. Native
measurements describe these short fixture runs, not an overnight workload.

`recorder_process_preserved: true` means the same PID and start time were
observed before and after a native run. It does not show that samples were
unaffected or that the recorder remained healthy. Emulated runs set that field
to `null`. Both modes explicitly report
`native_recording_fidelity_tested: false`.

## Supervisor behavior

`trial_exec.c` forks one child, lowers its scheduling priority, disables core
dumps and directly executes the requested program. Reports are new mode-600
files; existing paths and symlinks are rejected. The supervisor records actual
`wait4` usage and distinguishes child failure, failed execution, signals,
interruption and timeout. It restores the default `SIGCHLD` disposition before
launch so an inherited ignored disposition cannot discard the child status.

On timeout or a handled interruption it kills its own still-owned child and
process group and reaps the child. It never signals an already reaped PID.
The child also requests termination if its direct parent dies. Exit codes are
124 for timeout, 125 for supervisor/report failure, 127 for setup/exec failure,
and otherwise the child's exit status or `128 + signal`; invalid arguments
return 2. Consumers must inspect the JSON as well as the process status.

This is intended for the bundled single-process programs. It does not promise
cleanup of arbitrary detached descendants after a successful leader exit or
after abrupt supervisor death. Deadline checks occur at roughly 10 ms
intervals and depend on scheduling; uninterruptible kernel I/O can delay
termination/reaping. It is not a real-time execution guarantee or a security
sandbox, and it does not impose a memory or storage quota.

## Verification and remaining work

On October 3, 2026 (America/New_York), all 42 combined feature tests passed.
The 11 trial tests cover host
and ARM-supervised execution, error/signal distinction, process-group timeout
and sibling preservation, interruption/parent death, inherited `SIGCHLD`,
exclusive reports, the complete six-case emulated run, private archive
contents, changed-input rejection and deliberately wrong expected output.
The eight optical-monitor tests and the existing 23 EEG/motion/event/session
feature tests also pass. A fresh private six-case bundle was built and its
complete emulated run passed; source hashes identify the qualified inputs.

The current access check found no reachable recovery shell, connected Android
recovery device, or Bluetooth adapter on the Linux host. No bundle has been
deployed. The native success branch, target utility compatibility, headset
resource use, overnight recording fidelity and added-sensor electrical behavior
remain unverified. Existing recovery installation records do not establish
current connectivity. No network or recovery configuration was changed.

Project-authored trial source is Apache-2.0. Generated static executables also
link the toolchain's runtime libraries. This is a private testing package;
public binary distribution requires satisfying those libraries' applicable
license/source/relinking obligations separately. See
[third-party notices](../THIRD_PARTY_NOTICES.md#feature-trial-tooling).
