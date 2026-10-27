# Docker sandbox

`SandboxShellTool` exposes the existing `run_shell(command)` action but runs each command in a new restricted local Linux container. Only task files persist between calls. The unified workflow `PatientStateAgent` does not automatically enable this sandbox; the advanced `UnitPatientStateAgent` opts into it through `sandbox_config`. Omitting that configuration retains local Shell execution. A configured sandbox never falls back to host execution.

## Prepare the image

Install and start Docker Desktop on macOS, or a local Docker Engine on Linux. Run BPCAgent as a non-root user. The runtime finds `docker` on PATH, under `~/.docker/bin`, or inside `/Applications/Docker.app`. `SandboxConfig(docker_path=...)` can specify another executable. It uses the current saved Docker context, pins that context for the task, and only accepts a Unix-socket endpoint and Linux engine. It deliberately does not forward `DOCKER_HOST` or other process environment overrides. Use `docker context use desktop-linux` on macOS if needed.

From the BPCAgent repository:

```bash
docker build --platform linux/arm64 -t bpcagent-sandbox:0.1 src/sandbox/image
```

On x86 Linux use `--platform linux/amd64`. The Dockerfile pins the Python base image digest and installs no additional Python packages. Add dependencies to the image explicitly if a future task needs them. The runtime does not build, download or install anything, and pins a task to the inspected local image ID. Dockerfile is kept in the source repository, not included as a wheel resource.

If Docker Hub authentication/download times out, check Docker Desktop's proxy and the build client's proxy configuration. The successful build on the development machine needed the existing local proxy supplied to that build command through `HTTP_PROXY` and `HTTPS_PROXY`; runtime containers remain offline. Do not add model keys or patient data to the build context.

## Run the demo

`code/test.py` already passes `sandbox_config=SandboxConfig(image="bpcagent-sandbox:0.1")`. In the configured Conda environment, with Docker Desktop running:

```bash
python test.py
```

This command uses the real model configuration from `BPCAgent/.env`. Docker integration tests below use mocked models and synthetic data instead.

Task layout:

```text
code/workspace/YYYYMMDD_HHMMSS_patient-id_cutoff-day/
  input/       # host extraction, container read-only
  scripts/     # generated standardize_tm.py
  output/      # tumor_markers_standardized.json
  tmp/         # container temporary files
```

Only these four directories are mounted, at `/workspace/<name>`. Each command begins at `/workspace`; `cd`, exports and background processes do not carry over. Python is `/usr/local/bin/python`. The prompt specifies script and output paths, and validation requires a saved, nonempty normalization script. `source_state` remains the input filename. The host validates the JSON; it never executes the generated script.

The demo stores the overall root as `task_dir` and passes `task_dir/input` to the unchanged parent as `work_dir`. Keep these two attributes distinct. The parent still owns extraction tools; the second stage temporarily replaces the registry with `SandboxShellTool` and restores it afterwards.

## Limits and failure behavior

`SandboxConfig` defaults: 60 seconds per command, 15 seconds per Docker control operation, 512 MiB memory (no additional swap), one CPU, 64 PIDs, 32 KiB each for stdout/stderr, 256 MiB total task files, 10,000 directory entries and 16 MiB per validated artifact. Timeouts include command startup but separate Docker creation/inspection/cleanup calls have their own control deadlines.

Network mode is `none`, rootfs is read-only, capabilities are dropped, privilege escalation is disabled, and the container runs with the non-root host UID/GID so bind-mount ownership remains usable. No host home/project, credential directory, raw dataset or Docker socket is mounted. Writable data mounts are only scripts/output/tmp. Normal container devices and shared-memory pseudo-filesystems still exist; this is not a promise that every possible filesystem write is eliminated. `/tmp` is not separately opened for writing; use `/workspace/tmp`.

The workspace size/file-count guard samples while commands run and checks again afterwards. **It is a soft limit, not a filesystem quota:** rapid writes may overshoot it. Links and special files are rejected by the workspace guard; host artifact reads use no-follow, nonblocking open, regular-file checks and size limits. Commands cannot race a directory symlink into the host's recursive file-size scan. Do not reuse task directories or modify them concurrently from other host programs.

Commands run through argument arrays with `shell=False` on the host; only the container's non-login Bash interprets the command. stdout/stderr are continuously drained with bounded in-memory retention; Docker log persistence is disabled for these command containers.

Normal completion and timeout force-remove the entire command container, including descendants. A timeout returns `timed_out=true`, exit code 124, and permits a fresh command only after cleanup. Resource/OOM status is recorded. Infrastructure or cleanup failure marks the sandbox unusable and stops the demo. Ordinary shell errors remain visible through nonzero `exit_code`, allowing model correction.

The host logs final state, validation attempts, image ID, context, limits, per-command container names and cleanup status to the existing `task_dir/logs/UnitPatientStateAgent_*.json`. No `run_result.json` is created. A stopped Docker daemon or force-killed host process can prevent cleanup; consult `active_containers`/execution records in the log. All containers have `bpcagent.sandbox.session=<id>` labels. Inspect only the affected session before removing its containers; never globally prune Docker resources for cleanup.

Container isolation does not replace semantic validation and does not change which data the host sends to the configured model service.

## Tests

Run ordinary offline tests (Docker tests explicitly show as skipped):

```bash
python -B -m unittest discover -s tests
```

Run real Docker tests separately after building the image:

```bash
BPC_RUN_DOCKER_TESTS=1 python -B -m unittest discover -s tests -p test_sandbox_integration.py -v
```

When enabled, unavailable Docker or image is a test failure, not a silent skip. Tests cover RO/RW mounts, non-root/capability settings, disabled network and rootfs writes, persistence, other-task invisibility, timeout/background cleanup, output truncation, memory exhaustion, soft disk guards, and a full synthetic extraction/normalization/repair workflow. They never invoke a real model or load the patient dataset.

Not included in this first version: persistent per-task containers, remote execution, automatic image builds, hard disk quotas, automatic recovery after host/daemon crashes, Windows hosts, and merging the demo into PatientStateAgent.
