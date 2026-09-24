# Runner migration review — 2026-09-24

## Contract and scope

The current requested state follows CarryOn's existing deployment pattern: hosted CI builds, dedicated SSH keys, forced receivers and operator-installed helpers. DoTasks and MarkFix retain their Docker services and existing data contracts. MarkFix site and desktop distribution both migrate; Laya deployment and per-service resource tuning are deferred. The exploratory OIDC changes were removed before delivery.

Implementation risk is R2 (deployment authorization boundary). The user explicitly authorized commit, cutover, Runner shutdown and necessary deletion on 2026-09-24. Privileged host provisioning and first activation follow independent review; the original worktrees contain unrelated changes, especially DoTasks, and must not be committed wholesale. Dedicated GitHub deployment secrets have been configured with deployment disabled; host provisioning follows this review.

## Impact and authority

- DoTasks: `.github/workflows/deploy-cloud.yml`, `deployment/`, deployment tests and deployment documentation. Existing application/config changes are outside this task.
- MarkFix: CI and desktop workflows, `infra/aliyun/` receivers/packaging/bootstrap; the existing deployment script gains a preloaded-image path. The existing desktop publisher is restricted to its authoritative production origin, disallows redirects and bounds Compose subprocesses.
- SSH transport only accepts exact commit references and bounded, checked artifacts. Privileged scripts and Compose are not supplied by application uploads. Uploaded image tags cannot replace another application's tags.
- Application keys can deploy application code and therefore access their application's data. They must be separately generated; neither repository receives Alibaba account credentials.

## Independent review

GPT-5.6 Luna independently read the original implementation, CarryOn's reference, both current changesets and relevant tests, with authority to reject. Initial findings required a trusted desktop download origin, bounded Compose process groups, a fixed DoTasks Compose file and actual Docker identity validation. All were addressed; the reviewer accepted the receiver boundary and recovery semantics. The review also independently ran receiver/publisher/deploy tests.

## Evidence

- DoTasks focused SSH receiver/activation/workflow tests: 11 passed. Full project suite: 428 tests passed with 1 platform-dependent skip (`/tmp/dotasks-runnerless-tests.log`).
- MarkFix receiver/deployment/desktop publisher tests: 25 passed, including rejected environment symlinks, mutable image references and release paths outside the application root.
- Real local Docker: built an isolated Linux image, packaged it, verified and normalized its archive, loaded it, re-exported it and verified its config digest. Containerd reports a manifest ID rather than the classic config ID, so activation uses the verified engine ID.
- Real local Compose: started and removed an isolated, network-disabled container using a bare immutable image ID and `--pull never`; running container identity matched. No business volumes were mounted.
- MarkFix required format/lint/typecheck/test/build checks passed with Node 24.19.0; 351 business tests passed, in addition to the 24 deployment checks. Results are recorded in `/tmp/markfix-runnerless-*.log` for this session.
- Target read-only preflight: Docker 26.1.3, Compose 2.27.0, sshd active, both receivers absent, both old Runner units active. Local Docker is 29.4.1 / Compose 5.1.3, so target deployment remains unverified.
- Isolated DoTasks release baseline ee8f7a5: 385 tests executed; the macOS helper initially lacked web dependencies. After npm ci, all 9 helper tests passed. Unrelated original-worktree changes are excluded.
- Target receiver interpreter is explicitly /usr/bin/python3.11 (3.11.13); the system python3 is older and is not used.
- Live cutover validation is recorded separately after deployment; local evidence alone does not establish deployment success.

## Recovery and release gates

Keep deployment disabled until the reviewed root-owned receivers and separate keys are provisioned. Verify the host key through the authenticated Alibaba channel, configure GitHub secrets/variables, then execute a controlled hosted deployment of each project and the MarkFix desktop publisher. Confirm exact image/source identity and existing application health. Only then, and only with no active/queued jobs, disable the old Runners. Preserve their registration/files during the migration window.

DoTasks backs up stopped data before activation. If new code has run and fails, keep it stopped pending inspection/forward repair; automatic code-only rollback is unsafe if SQLite migrated. MarkFix keeps its existing migration/backup/forward-repair rules and desktop policy restore. Neither path removes a database volume. Pausing deployment is `DEPLOY_ENABLED=false`; returning to an old transport also requires restoring its reviewed workflow and re-enabling the corresponding Runner. Do not infer application-data rollback from transport rollback.

The absence of an upload wall-clock deadline is a low-priority hardening note: a deployment-key holder could hold the transfer lock with a slow upload. Authorized GitHub jobs have bounded timeouts. The migration frees Runner overhead only after cutover and does not establish that this 2 GiB host can run Laya.
