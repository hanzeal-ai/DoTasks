# Hanzeal host operations

Applies to the existing 2 CPU / 2 GiB host only. User requested resource/log isolation, loopback backend ports and monitoring; explicitly declined business-data and off-site backups. No backup scheduler is installed. MarkFix production email is a separate pending transition; this operation preserves its running application image and mode.

## Resource policy

DoTasks 256 MiB, MarkFix API 384 MiB, PostgreSQL 384 MiB, dashboard 64 MiB. CarryOn retains its existing 256 MiB systemd limit. These are initial containment budgets, not proven peak requirements. CPU shares prioritize the applications/database (1024) over static serving (512), without hard CPU quotas. Container logs retain at most three 10 MiB files per container. DoTasks/MarkFix published backends bind loopback; Nginx retains existing HTTPS domain and IP routes.

`apply.py` is a one-time operator change, not an application deployment. It locks both deployment paths, refuses active DoTasks work, checks configured images match currently running immutable IDs, saves private configuration recovery copies, changes the operator templates and current MarkFix release config, and recreates only existing services with no build/pull/migration. Failure restores original configuration and attempts every service, then checks original image IDs and HTTPS. Existing unrelated source changes are not released. Reapplying an existing resource policy is refused for review.

Install `ops.py` under `/usr/local/libexec/hanzeal-ops/` and the two `hanzeal-metrics` units under `/etc/systemd/system/`. Create `/var/lib/hanzeal-ops` root-owned mode 0700 before enabling the timer. The timer samples every five minutes, retains 14 days of its own private metric files, serializes runs and bounds execution. Low available memory (<256 MiB), free disk (<15%), unhealthy/missing containers or unavailable CarryOn mark the sample failed in systemd/journald. This is local monitoring, not external notification. Read with `journalctl -u hanzeal-metrics.service` and `systemctl status hanzeal-metrics.timer`.

## Recovery

Configuration copies are `/var/lib/hanzeal-ops/config-*/` with a manifest mapping numbered files to their original paths and image IDs. For a later reversal, take both deployment locks, confirm no intervening application release and identical image IDs, restore those configuration files, and recreate only the affected services using the same options as `apply.py`. Never restore an older configuration across an intervening image/schema release without separate review. The script's automatic recovery is limited to this same-image operation.

## Retention

Do not run `docker system prune -a` or delete volumes. Keep every image referenced by current/stopped containers, current configuration, and retained recovery releases. Review image candidates separately before deletion. Keep desktop downloads referenced by current update policy and supported clients; unreferenced artifacts need an explicit cutoff and deletion authorization. Existing historical backups are not deleted by this task. Only newly generated monitoring samples have automatic 14-day retention.
