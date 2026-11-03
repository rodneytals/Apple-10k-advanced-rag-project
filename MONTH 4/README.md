# Month 4 — Knowledge Graphs and Graph-Aware RAG

Month 4 is a notebook-based exploration of how knowledge graphs can complement a document RAG system. The work focuses on building and evaluating graph-backed reasoning for financial-document questions: resolving entities, distinguishing facts extracted from documents from topics remembered from conversations, checking graph coverage before routing, and comparing graph-assisted answers with a baseline using Ragas.

This folder is the **Month 4 notebook project**. It is separate from the later **MONTH4-2** Docker/FastAPI runtime and the separate Flutter client. The notebooks are development experiments, not a ready-to-run standalone service.

## Current folder contents

In the copy inspected for this README:

- **Week 1** contains the daily notebooks **day1.ipynb** through **day10.ipynb**.
- The local working copy also contains **day9_graph_scored.csv** and **day9_old_scored.csv**, which hold comparison outputs. They are **not part of the code-only GitHub upload** described here.
- **Week 2**, **Week 3**, and **Week 4** are present but currently empty.

The README therefore documents the Week 1 work found here. Add later weeks to the repository and update this overview if they become available.

## Learning and implementation goals

- Model entities and relationships in Neo4j as a complement to MongoDB Atlas document/vector retrieval.
- Resolve different names or references that may point to the same entity, combining embedding similarity, graph neighborhood signals, and an LLM tie-break with an abstain path.
- Write graph information and read it back into a RAG workflow.
- Keep conversational topic memory distinct from facts extracted from source documents.
- Extract document facts and use graph coverage to decide whether a graph-backed answer is supported or the system should fall back to document retrieval.
- Explore multi-hop questions that need facts from more than one passage or relationship.
- Avoid turning one-off mentions into persistent graph topics.
- Explore community detection using Python graph tools rather than depending on a paid Neo4j Graph Data Science tier.
- Compare graph-assisted and baseline RAG runs with Ragas metrics.

## Notebook sequence

| Notebook | Main focus |
|---|---|
| **day1.ipynb** | Introduces graph memory and entity extraction; frames entity merging as an architectural problem and adds Neo4j read/write experiments. |
| **day2.ipynb** | Entity resolution: compares embedding similarity, shared-neighborhood evidence, and an LLM tie-break, with an explicit abstain option. |
| **day3.ipynb** | Connects graph writes and reads to the live LangGraph RAG workflow and adds a verification pass. |
| **day4.ipynb** | Builds a document knowledge graph from extracted facts and distinguishes that graph from user/conversation memory. |
| **day5.ipynb** | Adds coverage-aware routing so the system checks what the graph can support before using it, with a fallback when graph coverage is insufficient. |
| **day6.ipynb** | Runs a small question set to verify graph-backed behavior and identify remaining gaps. |
| **day7.ipynb** | Refines graph memory so recurring topics are distinguished from one-off mentions. |
| **day8.ipynb** | Explores graph community detection with Python graph tooling. |
| **day9.ipynb** | Sets up a graph-assisted versus baseline RAG comparison and records Ragas results in CSV files. |
| **day10.ipynb** | Extends the comparison to a six-question set and reviews what the graph work did and did not resolve. |

The notebooks contain repeated definitions as the implementation evolves. Read them in numeric order and review the setup cells in each notebook rather than assuming the folder is a Python package.

## Evaluation approach

The Ragas work compares generated answers and retrieved context for a small development question set. The code includes **answer correctness**, **context precision**, and **faithfulness** metrics. The scored CSV files are local experiment outputs and are intentionally excluded from the code-only repository. Run the evaluation notebooks to regenerate results in your local working copy, subject to the required dataset, credentials, services, and model configuration.

These are experiment outputs, not a universal accuracy guarantee. Scores depend on the question set, reference answers, indexed documents, model, prompts, and retrieval configuration. This README does not quote numeric scores; regenerate results locally if you need specific values, and include the dataset size and evaluation conditions with them.

## High-level architecture explored

~~~mermaid
flowchart LR
    Docs[Financial source documents] --> Mongo[(MongoDB Atlas document/vector index)]
    Question[Question] --> Router[LangGraph routing and retrieval]
    Router --> Mongo
    Router --> Coverage[Graph coverage check]
    Coverage -->|supported| Neo4j[(Neo4j document/entity graph)]
    Coverage -->|not supported| Fallback[Document retrieval / fallback]
    Neo4j --> Answer[Answer generation and review]
    Fallback --> Answer
    Answer --> Eval[Ragas evaluation on sample questions]
    Eval --> Results[Local scored results]
~~~

The exact routing and graph shape vary between notebook revisions. The diagram is a conceptual summary of the experiments, not a claim that every notebook runs the identical final graph.

## Tools and integrations

The notebooks use or explore:

- **Python and Jupyter** for iterative experiments.
- **Neo4j** for graph entities, relationships, and graph queries.
- **MongoDB Atlas** and LlamaIndex components for document/vector retrieval.
- **LangGraph** for conditional routing and multi-step RAG workflows.
- **Groq** for LLM calls in the experiments.
- **Hugging Face sentence embeddings** for retrieval/entity similarity work.
- **Ragas and Hugging Face Datasets** for evaluation data and metrics.
- **NetworkX/community-detection tooling** for graph analysis.
- **SQLite checkpointers and Redis** in parts of the evolving workflow.

The current Month 4 folder does not include a pinned requirements file or a standalone application entry point. Package versions and external-service configuration must be reconstructed and tested in the environment used to run the notebooks.

## Running the notebooks

1. Install Jupyter and the Python packages imported by the notebook you plan to run. Because there is no pinned Month 4 requirements file in this folder, create and verify a dedicated environment before relying on reproducibility.
2. Configure access to the required MongoDB Atlas, Neo4j, and model services through local environment variables or a local secret manager.
3. Do not put real credentials in notebook cells, outputs, screenshots, or committed configuration files.
4. Open **Week 1/day1.ipynb** and work through the setup and cells in order; continue through **day10.ipynb**.
5. Review the generated tables and local CSVs for the intended question set and evaluation configuration. Keep generated CSVs out of the code-only GitHub upload unless you later decide they are safe and appropriate to publish.

Notebook results can depend on prior cell execution, external database contents, model availability, and the exact versions of libraries. Restarting a kernel may require rerunning setup and prerequisite cells.

## Relationship to the rest of the project

- **Month 1** handles the earlier document-ingestion/indexing stage. Its database/index schema must match the assumptions made by the Month 4 retrieval code.
- **Month 4** contains the graph and evaluation experiments documented here.
- **MONTH4-2** is a separate later backend/runtime folder that packages an API and Docker Compose services.
- **rag_chat_app** is the separate Flutter client that calls the backend API.

Do not describe this Month 4 folder by itself as the deployed mobile/desktop application or the Month4-2 API. A combined project README can link these components together.

## Limitations and interpretation

- The graph experiments are tied to the supplied financial-document corpus and sample questions.
- Entity resolution and graph coverage depend on extracted facts and graph contents; an empty or incomplete graph cannot support a complete answer.
- Ragas scores help compare runs on the selected examples; they do not prove production accuracy or eliminate hallucinations.
- Locally generated CSV scores are useful only alongside their question/reference set and run configuration; the CSVs are excluded from this code-only repository.
- A graph-assisted path adds database calls and reasoning steps, so it may increase latency. Measure the full request path before making performance claims.
- This folder does not provide a complete production authentication, deployment, observability, or data-governance setup.

## GitHub safety checklist

- Keep local environment files, API keys, database credentials, Neo4j backups, and private service URLs out of Git.
- Inspect notebook source **and outputs** before publishing. Clear outputs that contain credentials, document text, user information, or private database results.
- Exclude the scored CSV outputs from the code-only repository. If you later choose to publish evaluation results, first inspect them for private data and confirm you have permission to share them.
- Do not upload private source documents just to make the demo reproducible. Use a small synthetic or redistributable sample corpus instead.
- Review the staged files and Git history before making the repository public. If a real credential was committed, rotate it; deleting it in a later commit is not enough.

