"""Run the full experimental evaluation (scenarios, fault schedule, privacy,
benchmarks) and write results/ . Equivalent to `agentproto-recovery all`."""

import sys

from agentproto_recovery.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["all", *sys.argv[1:]]))
