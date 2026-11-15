"""Allow `python3 -m hydra` to start the daemon."""

from .daemon import main

if __name__ == "__main__":
    raise SystemExit(main())
