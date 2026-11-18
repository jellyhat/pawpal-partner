# Model Card: PawPal+ Sitter

> **TODO (you):** Fill in every section marked TODO. This file holds the graded responsible-AI reflection; reflection written only in the README doesn't count.

## System overview

> **TODO (you):** In 2–3 sentences, describe what the AI does in PawPal+ Sitter and who uses it.
>
> *Facts you can use:* Claude (`claude-opus-5-5`, via the Anthropic API) does two jobs:
> 1. It answers sitters' care questions using retrieved owner notes and care guides (RAG).
> 2. It resolves schedule conflicts through a tool-using agent.
>
> Without an API key, both fall back to non-AI behavior: answers quote the retrieved notes directly, and conflicts go to a rule-based resolver.

## Intended use and users

> **TODO (you):** Who is this for, and what should it *not* be used for? For example, is it a substitute for a vet, or for the owner's own judgment?

---

## 1. How did you collaborate with AI on this project?

> **TODO (you):** Describe how you worked with an AI assistant while building this. What did you ask it to do? What did you decide yourself? How did you check its work?

## 2. One AI suggestion you accepted

> **TODO (you):** Describe one suggestion you accepted, why you accepted it, and how you verified it was correct.

## 3. One AI suggestion you rejected or revised

> **TODO (you):** Describe one suggestion you rejected or changed, what was wrong or didn't fit, and what you did instead.

## 4. Limitations of the system

> **TODO (you):** What can the system not do well, and where could it be wrong or cause harm?
>
> *Limitations you've seen in this project, to choose from or add to:*
> - **Keyword retrieval.** It misses paraphrases. For example, "Mochi skipped his dinner, should I worry?" returns the dinner routine instead of the appetite guide. hit@1 is 82% on the evaluation set.
> - **Keyword guardrails.** The emergency and medication checks match phrases, so unusual wording could slip past.
> - **Live Claude paths are unverified.** Claude's answers and agent behavior were only tested with a fake client, not against the real API.
> - **Owner notes are trusted.** If the owner writes something wrong, the app repeats it.
> - **No accounts.** Anyone using the app can see and edit every owner's data, and it only runs on one computer.
> - **Limited care guides.** The six guides in `knowledge/` were written for this project and haven't been reviewed by a vet.

## 5. Risks, misuse, and safeguards

> **TODO (you):** What could go wrong if someone relied on this? What safeguards did you build, and are they enough?
>
> *Safeguards in the code:*
> - Medication-change and human-medicine questions get a fixed reply.
> - Emergency questions put "call the vet now" first.
> - Answers must cite real sources and can't introduce amounts that aren't in the notes.
> - The agent can't move zero-flex tasks or delete anything, and every change is logged and can be undone.

## 6. Testing and evaluation results

> **TODO (you):** Summarize how you tested the AI parts and what the results showed.
>
> *Facts:*
> - `python -m pytest` runs 74 passing tests.
> - `python -m evaluation.evaluate` scores:
>   - planner scenarios 100%
>   - retrieval hit@3 95% (hit@1 82%, MRR 0.88)
>   - guardrails 100%
>   - conflict resolver 100%

## 7. What would you improve next?

> **TODO (you):** Name one or two improvements and why they matter. For example: embedding-based retrieval, user accounts, a vet-reviewed knowledge base, or running the live evaluation with Claude.
