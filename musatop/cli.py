"""Console entry point. Hardware is accessed only after argument parsing."""

import argparse
import json
import math
import sys

from . import __version__
from .view import SORT_KEYS, Options, filter_snapshot, render_text


def interval_value(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a number") from exc
    if not math.isfinite(number) or not 0.25 <= number <= 3600:
        raise argparse.ArgumentTypeError("interval must be between 0.25 and 3600 seconds")
    return number


def index_set(value):
    try:
        values = {int(item) for item in value.split(",")}
        if not values or min(values) < 0:
            raise ValueError
        return values
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated nonnegative integers") from exc


def parser():
    result = argparse.ArgumentParser(description="Monitor Moore Threads GPUs and host-visible GPU processes.")
    result.add_argument("--version", action="version", version=f"musatop {__version__}")
    result.add_argument("--once", action="store_true", help="print one text snapshot")
    result.add_argument("--json", action="store_true", help="print one JSON snapshot (schema version 1)")
    result.add_argument("--interval", type=interval_value, default=1.0, metavar="SECONDS", help="sampling interval (default: 1)")
    result.add_argument("--gpu", type=index_set, metavar="IDS", help="GPU indices, e.g. 0,1")
    result.add_argument("--pid", type=index_set, metavar="PIDS", help="host process IDs, e.g. 123,456")
    result.add_argument("--user", help="exact host username")
    result.add_argument("--sort", choices=SORT_KEYS, default="gpu_memory", help="sort processes (default: gpu_memory)")
    result.add_argument("--reverse", action="store_true", help="reverse the default sort direction")
    result.add_argument("--ascii", action="store_true", help="use ASCII bars and trends (TUI only)")
    result.add_argument("--no-color", action="store_true", help="disable colors (TUI only)")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if not sys.platform.startswith("linux"):
        print("musatop currently supports Linux hosts only.", file=sys.stderr)
        return 1
    options = Options(gpu=args.gpu, pid=args.pid, user=args.user, sort=args.sort, reverse=args.reverse,
                      ascii=args.ascii, no_color=args.no_color)
    from .monitor import Monitor
    monitor = Monitor(args.interval, gpu_indices=args.gpu)
    try:
        if args.once or args.json or not (sys.stdin.isatty() and sys.stdout.isatty()):
            snapshot = filter_snapshot(monitor.sample(), options)
            if args.json:
                print(json.dumps(snapshot.to_dict(), ensure_ascii=True, allow_nan=False, indent=2))
            else:
                print(render_text(snapshot))
            return 1 if snapshot.errors or snapshot.devices_stale or snapshot.processes_stale else 0
        from .tui import run_tui
        return run_tui(monitor, options)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"musatop: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
