"""Retired deployment entry point; use the reviewed restricted SSH workflow."""
import sys


def main():
    print(
        'This deployment path is retired. Use the Test, publish, and deploy '
        'DoTasks Cloud workflow with DEPLOY_ENABLED=true and the reviewed '
        'restricted SSH receiver (deployment/README.md). No cloud command '
        'was executed. Database migration failures require forward repair '
        'or an explicitly reviewed matching code/data restore.',
        file=sys.stderr,
    )
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
