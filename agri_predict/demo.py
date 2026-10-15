"""One-command demo (no GPU, no API key): builds the synthetic register if needed, runs the agent loop with
the scripted policy over one collective, then shows the officer's queue.   python -m agri_predict.demo
For the real open-weights agent:  python -m agri_predict.agent --model ollama --group GV-BOR"""
import sys

from . import agent, review, synth_households
from .config import HOUSEHOLD_DB


def main():
    if not HOUSEHOLD_DB.exists():
        synth_households.main()
    agent.main(["--model", "scripted", "--group", "GV-BOR"])
    print("\n--- officer queue ---")
    review.main(["list"])
    print("\nNext: python -m agri_predict.review show ADV-0001   |   approve ADV-0001 --officer \"Your Name\"")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
