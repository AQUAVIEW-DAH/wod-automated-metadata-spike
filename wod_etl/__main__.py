"""`python -m wod_etl run <accession>` -- the same command as `wod-etl`."""
import sys

from wod_etl.orchestration.cli import main

sys.exit(main())
