# Month 2. Building an Agentic RAG System


This one took me twenty-five days, four weeks, one running project: turning a single Apple 10-K document into a system that can use tools, reason step by step, catch its own mistakes, run as a checkpointed graph, and remember who you are across separate conversations.

Every day has a real Jupyter notebook (`dayN.ipynb`) and a matching write-up (`dayN.md`) explaining exactly what the code does, why it exists, and what actually happened when it ran — including the real bugs that came up along the way.

---

## The four weeks

| Week | Days | Focus |
|---|---|---|
| [**Week 1**](./Week%201) | 1–7 | Learning to build tools. Hand-written vs. auto-generated schemas, a first multi-tool agent, forcing real math instead of AI guesswork, routing between SQL and vector search, running tools in parallel, and defending against tool hallucination. |
| [**Week 2**](./Week%202) | 8–13 | Building an agent's reasoning *by hand*. A ReAct loop from scratch, structured logging of every thought, Plan-and-Execute, Corrective RAG with a self-checking Reviewer, a cost-aware router, and a real 20-question audit. |
| [**Week 3**](./Week%203) | 14–20 | Rebuilding everything as a LangGraph state machine. Cycles, checkpointing that survives a crash, a human-approval pause before risky actions, two AI agents collaborating, and automatic fallback when the main AI service goes down. |
| [**Week 4**](./Week%204) | 21–25 | Giving the system memory. A short-term conversation window, a long-term profile, a compressed running summary, tracked analysis topics, and a final recall test proving all four memory layers work together. |

---

## What this project actually proves, end to end

By Day 25, a single system built entirely across these 25 days could:

- Decide on its own whether a question needed a document search, a live web search, or both
- Refuse to do arithmetic itself, routing every calculation through real, deterministic code
- Catch its own hallucinated tool calls and recover instead of crashing
- Score its own answers with a second AI acting as a reviewer, and rewrite a bad answer using that feedback
- Save its progress mid-task and resume later, even after a simulated crash
- Pause and wait for a human's explicit approval before an irreversible action
- Split work between two specialist AI roles; a researcher and a writer that hand tasks back and forth
- Fall back to a locally-run AI model if the main cloud service went down
- Remember a stated preference, a compressed summary, and a named recurring topic across four entirely separate, later conversations and pass an independent test proving it

## A few honest highlights

Nothing here was scripted to look clean. A few real things worth knowing before you dig in:

- **Day 13's audit initially scored 0 out of 20** not because the system didn't work, but because of a one-letter capitalization mismatch between two files. Finding it is exactly why the audit was built.
- **Day 22's fact-memory once saved the literal word "YES"** as a "fact" about the user, due to a regex quietly matching inside its own label. It's explained in full in `Week 4/day22.md`.
- **Day 25 ends with a real, independently-judged 3-out-of-3 pass** on a recall test that gave the system zero hints — proof the four memory layers built across the week genuinely work together.

---

*Part of a self-directed AI/ML engineering curriculum. [Month 1](../MONTH%201) covered building the underlying retrieval pipeline; Month 3 moves into rigorous, RAGAS-based evaluation.*
