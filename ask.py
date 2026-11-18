"""Ask the PawPal+ care assistant a question from the command line.

    python ask.py "How much does Mochi eat for breakfast?"
    python ask.py "Can I give Luna an extra pill tonight?"

Uses the sample owner (data/sample_routine.json). Runs with Claude when
ANTHROPIC_API_KEY is set, otherwise offline (answers quote the sources).
"""

import argparse

from llm import get_client
from pawpal_logging import setup_logging
from rag import CareAssistant
from sitter_planner import Routine


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask a pet-care question about the sample pets.")
    parser.add_argument("question", nargs="+", help="the question to ask")
    args = parser.parse_args()
    setup_logging(console=False)

    client = get_client()
    assistant = CareAssistant(Routine.load("data/sample_routine.json"), llm_client=client)
    question = " ".join(args.question)
    answer = assistant.answer(question)

    print(f"Q: {question}")
    print(f"Mode: {'Claude' if answer.used_llm else 'offline (quoting sources)' if client is None else 'Claude unavailable, quoted sources'}")
    print(f"Guardrail: {answer.guardrail}{'  [URGENT]' if answer.urgent else ''}")
    print("A:")
    for line in answer.text.splitlines():
        print(f"  {line}")
    if answer.sources:
        print("Sources:")
        for i, doc in enumerate(answer.sources, start=1):
            print(f"  [{i}] {doc.title}")


if __name__ == "__main__":
    main()
