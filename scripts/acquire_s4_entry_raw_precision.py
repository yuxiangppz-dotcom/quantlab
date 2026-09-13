from pathlib import Path

from quantlab.data.entry_raw_precision import run

if __name__ == "__main__":
    print(run(Path.cwd())["fingerprint"])
