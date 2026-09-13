from pathlib import Path

from quantlab.data.margin_intake import run

if __name__ == "__main__":
    print(run(Path.cwd())["fingerprint"])
