"""Performance benchmark only. Equivalent to `agentproto-recovery benchmark`."""

import sys

from agentproto_recovery.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["benchmark", *sys.argv[1:]]))
