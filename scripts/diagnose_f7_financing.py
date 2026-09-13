from pathlib import Path

from quantlab.research.financing_diagnostic import run

if __name__ == "__main__":
    print(run(Path.cwd())["fingerprint"])
