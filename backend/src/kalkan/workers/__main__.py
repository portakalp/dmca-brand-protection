"""Start a worker process group: ``python -m kalkan.workers <name>`` (e.g. ``default``)."""

from __future__ import annotations

import sys

from dramatiq.cli import main as dramatiq_main
from dramatiq.cli import make_argument_parser

from kalkan.config import get_settings
from kalkan.logging import configure_logging
from kalkan.workers.specs import WORKERS, dramatiq_args


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in WORKERS:
        print(f"usage: python -m kalkan.workers {{{','.join(WORKERS)}}}", file=sys.stderr)  # noqa: T201
        return 2
    spec = WORKERS[argv[0]]
    # The parent process only supervises; it logs JSON like its children.
    configure_logging(spec.service, get_settings().log_level)
    args = make_argument_parser().parse_args(dramatiq_args(spec))
    return int(dramatiq_main(args) or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
