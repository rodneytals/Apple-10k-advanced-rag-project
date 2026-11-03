# Apple 10-K Advanced RAG Project

A self-directed AI/ML engineering curriculum, built month by month around one running project: a production-grade Retrieval-Augmented Generation system for Apple's financial filings. The filings were just chosen as an example but the flow of the project can handle all types of data with a few adjustments basing on the pecific field.

---

## The full project summary

| | Focus |
|---|---|
| [**MONTH 1**](./MONTH%201) | The core RAG pipeline: It handles many things including Auto-Merging Retrieval, step back prompting and MongoDB Atlas vector search among others over Apple's 10-K filings. |
| [**MONTH 2**](./MONTH%202) | Turning that pipeline into an agent: Handles tool use, hand-built reasoning, a LangGraph state machine, and four layers of memory. |
| [**Month 3**] | Using rigorous RAGAS evaluation to evaluate the pipeline against the metrics of context precision, answer correctness and faithfulness by testing it against multiple golden datasets to see that it returns answers grounded in the right context.|
| [**Month 4**] | Doing some more RAGAS evaluation after realising some mistakes made earlier. |
| [**Month 4-2**] | Handling multimodal RAG, API integration, DOCKER containers, Watchdog setup, Testing pipeline health and Deployment using a Flutter App frontend written in Dart. Find it in my second Repository |

Each month's folder contains its own README with more detail.

---

*Ongoing curriculum — Month 3 moves into rigorous, RAGAS-based evaluation of everything built so far.*
