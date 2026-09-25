# DoTasks SSH deployment

This follows CarryOn's deployment architecture: GitHub-hosted tests/builds, a dedicated SSH key, a forced command receiving a bounded artifact on stdin, and operator-installed server helpers. No GitHub Runner or Alibaba account credential is needed on the server or in the workflow. The existing Docker application and data volume remain in use.

## Current state and authorization

These files prepare the migration; they do not mean it is installed. Do not disable the old Runner until the new receiver has passed a real hosted workflow deployment. Installation changes privileged SSH/sudo configuration and needs operator approval. Commit/push and application deployment are separate authorization boundaries. This migration does not install Laya or tune other services' resource limits.

## One-time installation

1. Keep `DEPLOY_ENABLED=false`. Generate a dedicated Ed25519 deployment key for this project. Do not reuse CarryOn's or MarkFix's key.
2. Review `bootstrap.sh`, `ssh-entry.py`, `receive.py`, and `activate.py`. On the existing DoTasks host, run `DEPLOY_PUBLIC_KEY="<public key>" bash deployment/bootstrap.sh` as root from the reviewed checkout. It creates `dotasks-deploy`, a root-owned forced command and a sudo rule permitting only the receiver. It copies the current Compose file into `/usr/local/libexec/dotasks-deploy/compose.yaml`; it does not restart the application or replace `.env`, accounts or volumes. Existing provisioning is refused rather than overwritten.
3. Verify the server's SSH host public key using the authenticated Alibaba CLI (as with CarryOn), then configure repository secrets `DEPLOY_SSH_KEY` and `DEPLOY_KNOWN_HOSTS`, and variable `DEPLOY_HOST`. Never use unverified `ssh-keyscan` output or disable host verification.
4. Review the concrete change and enable `DEPLOY_ENABLED=true`. A `main` push deploys; a manual main run also requires the `deploy` input. Record the successful workflow and verify the site's account endpoint.
5. Only after success and after checking both running and queued workflows, stop and disable the old DoTasks Runner unit. Keep its registration/files initially so an operator can restore it. Never interrupt a busy Runner.

## Receiver boundary

The key accepts only `site <40-character commit SHA>`, without a shell, SCP, terminal or forwarding. The deployment user is not in the Docker group. Its single sudo entry calls a root-owned Python receiver whose argument parser also validates the request. The receiver can load and run this application's code; treat its key as access to DoTasks application data. It does not accept arbitrary scripts or Compose files.

Uploads are streamed to a private temporary directory, capped at 2 GiB compressed and 4 GiB expanded. Flat regular ZIP files only; duplicate names, links and traversal are rejected. Every image checksum is verified. Docker tags must be `dotasks-cloud:<commit>`; the image archive is normalized to one manifest and its blobs before load, so extra tag mappings cannot modify another application's tags. The loaded image is re-exported to verify its config digest, then activated by the Docker engine's immutable image ID. This accommodates both classic and containerd image stores. Allow free disk space for upload, extraction, normalization, re-export and retained backups; check capacity before provisioning.

The receiver holds a transfer lock, while activation holds `deploy.lock`. The operator-installed Compose file remains authoritative for volumes, ports and environment. Updates to receiver/Compose must be separately reviewed and installed. Ordinary application uploads cannot replace these privileged files.

## Verification and recovery

Activation refuses known active task/decomposition runs, saves `.env` and Compose, stops DoTasks, and copies `/data/dotasks` into a private backup. It preserves unknown environment settings and account mode, updating only `DOTASKS_IMAGE`. It starts only DoTasks with `--pull never`, verifies the running image ID and an authenticated health endpoint (or multi-account auth status), then records `source.sha`.

If the stopped-data backup fails, the previous image is restarted because new code has not run. Once the new application starts, it may migrate SQLite. A startup/HTTP failure therefore stops the new service and retains its current data plus the backup; it does not automatically reconnect old code to possibly migrated data. Inspect and repair forward, or separately authorize restoration of a matched code/data backup after accounting for new writes. Never delete the volume or restore only the environment file and assume the database was rolled back.

To pause deployment, set `DEPLOY_ENABLED=false`. Runner recovery is `systemctl enable --now` for the existing DoTasks Runner unit after reviewing queued jobs; workflow recovery additionally requires restoring its reviewed prior workflow. CarryOn's Python-only automatic code rollback is not safe to copy over DoTasks database migrations.

## Local checks

Run `./scripts/test tests.test_ssh_deployment tests.test_ssh_activation tests.test_github_deploy_workflow`, then the project suite. The receiver tests cover malformed input, checksum and image identity mismatches, pinned SSH host verification, preserved configuration and failure recovery. Real Docker transport and image-ID Compose checks supplement these tests. Local checks do not establish target-host installation or production deployment.

## Current host operations

The 2026-09-25 resource/log/loopback rollout is documented in [host operations](host/README.md) and [delivery evidence](../docs/host-optimization-2026-09-25.md). The old `scripts/deploy-aliyun-cli.py` entry point is retired; daily application releases use the restricted SSH workflow. Host configuration changes do not bypass the operator-owned configuration boundary. Business backup scheduling was explicitly declined for this noncritical host; existing deployment recovery behavior is unchanged.
