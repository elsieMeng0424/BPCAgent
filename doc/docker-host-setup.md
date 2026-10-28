# Docker setup on the current workstation

Checked on 2026-10-28 for account `u2021202188` (UID/GID `2202412`) on
Rocky Linux 8.10, x86_64. Docker binaries can be installed under this account;
running a usable daemon still depends on host configuration.

## Installed user-owned binaries

Docker 29.9.2 and its matching rootless extras were downloaded from the official
[stable x86_64 directory](https://download.docker.com/linux/static/stable/x86_64/)
and unpacked into `/home/u2021202188/.local/opt/docker/29.9.2/bin`.
The client is also linked from `.local/bin/docker` and `.docker/bin/docker`, so
the current shell and BPCAgent's existing executable lookup can find it.

Verified executable versions: Docker client 29.9.2 (`a82e528`), daemon 29.9.2
(`0367cbe`), and RootlessKit 3.3.0. No daemon was started or system service installed.
`docker info` still fails because no daemon socket exists. The rootless prerequisite
check still fails with the host requirements described below.

To repeat the prerequisite check after administrator configuration:

```bash
export PATH=/home/u2021202188/.local/opt/docker/29.9.2/bin:$PATH
dockerd-rootless-setuptool.sh check
```

Static binaries need explicit version updates; the distribution package manager
does not update this installation. Existing Docker client configuration and shell
startup files were left in place.

## Observed prerequisites

| Check | Result |
| --- | --- |
| Non-interactive sudo | Not available; sudo requests a password |
| Unprivileged user namespaces | `unshare -Ur true` succeeds |
| `newuidmap` / `newgidmap` | Present, with the required setuid/setgid file capabilities |
| `/etc/subuid` / `/etc/subgid` | Both empty; no subordinate range allocated to this account |
| User systemd | `systemctl --user` cannot connect to the user bus |
| Cgroups | v1 |
| Docker daemon socket | No `/var/run/docker.sock` |
| Home filesystem | Lustre; choose and validate suitable local daemon storage separately |

The official Docker 29.9.2 rootless setup tool's `check` command additionally
reports missing `iptables` and requests the `nf_tables` kernel module. Its
generated administrator commands were inspected, not executed.

## Administrator work

Preferred request: provide an administrator-managed Docker Engine or another
approved compatible environment for account `u2021202188`. BPCAgent needs a local
Unix-socket Linux engine, disabled container networking, and enforceable memory,
CPU and PID limits. Follow the cluster's access policy; Docker socket access is
highly privileged and should not be granted casually.

For Rootless Docker, an administrator must allocate at least 65,536 subordinate
UIDs and GIDs to this account. Select ranges that do not overlap host/cluster
identities or another account's allocations. Do not copy the setup tool's example
range `100000:65536` without checking the cluster's identity policy.

The resulting entries have this form, with independently selected start values:

```text
# /etc/subuid
u2021202188:ADMIN_SELECTED_UID_START:65536
# /etc/subgid
u2021202188:ADMIN_SELECTED_GID_START:65536
```

For the default rootless networking setup, the reported prerequisite commands are:

```bash
sudo dnf install -y iptables
sudo modprobe nf_tables
```

The administrator should verify the correct module for this host's kernel/network
configuration. Once the mappings and prerequisites exist, the user can run
`dockerd-rootless-setuptool.sh check` and then set up a user-owned daemon. Without
user systemd, Docker documents direct startup using `dockerd-rootless.sh` and a
private runtime directory; lack of user systemd alone is not an absolute blocker.

Rootless Docker's memory/CPU/PID cgroup controls require cgroup v2 and systemd.
On this cgroup v1 machine they cannot provide the resource limits expected by
BPCAgent's current sandbox. A compatible host configuration, or an
administrator-managed Docker Engine, is needed for those limits.

BPCAgent also currently passes the host UID/GID to `docker create --user` for
bind-mount ownership. Rootless Docker uses remapped IDs: the current high host UID
must not simply be reused as a container UID under a 65,536-ID mapping. Validate
and adapt this ownership policy before declaring rootless sandbox support.

## Project verification after the daemon is available

The project currently runs patient Shell commands locally via
`config/tools.jsonc` (`patient_shell.mode="local"`, `allow_unsafe_local=true`).
This needs no Docker but does not isolate host files or networking. Set the mode
to `sandbox` when the daemon and image are ready, then recreate the workflow
configuration. Setting `disabled` removes the Shell tool entirely. See
[tool configuration](../config/README.md).

The existing adapter accepts a local Unix-socket Docker context. Build the image
and run the integration suite as the ordinary project user:

```bash
docker info
docker build --platform linux/amd64 -t bpcagent-sandbox:0.1 src/sandbox/image
BPC_RUN_DOCKER_TESTS=1 .venv/bin/python -m pytest -q tests/test_sandbox_integration.py
```

A successful `docker --version` only verifies the client executable. The daemon,
image, mounts, network isolation and resource constraints still need verification.

Sources: [Docker rootless prerequisites](https://docs.docker.com/engine/security/rootless/),
[rootless service and resource limits](https://docs.docker.com/engine/security/rootless/tips/),
[static binary installation](https://docs.docker.com/engine/install/binaries/).
