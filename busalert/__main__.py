from __future__ import annotations

import argparse
import os


def main() -> None:
    ap = argparse.ArgumentParser(prog="busalert", description="TransMilenio live tracking & arrival alerts")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="simulate buses instead of using the live feed")
    ap.add_argument("--demo-speed", type=float, default=None, help="simulation time multiplier (demo)")
    ap.add_argument("--check", action="store_true", help="probe the live feeds, report, and exit")
    args = ap.parse_args()
    if args.check:
        from .check import run_check
        from .config import Settings

        raise SystemExit(0 if run_check(Settings.from_env()) else 1)
    if args.demo:
        os.environ["BUSALERT_DEMO"] = "1"
    if args.demo_speed:
        os.environ["BUSALERT_DEMO_SPEED"] = str(args.demo_speed)

    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
