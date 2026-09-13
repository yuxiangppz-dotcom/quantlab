"""Run the once-only fixed S4 first-entry intent projection."""

from pathlib import Path

from quantlab.research.s4_entry_plan import run

if __name__ == "__main__":
    print(run(Path.cwd())["fingerprint"])
