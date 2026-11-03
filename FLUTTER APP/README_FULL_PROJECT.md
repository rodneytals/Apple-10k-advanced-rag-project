# Cross-Platform RAG Assistant — Full Project

This project combines a document-ingestion/evaluation journey, a Python RAG backend, and a Flutter chat client for Android and Windows. It is a personal development project that demonstrates the end-to-end flow from indexed documents to an interactive question-answering app.

The components may live in separate folders or repositories. This README describes how they fit together and highlights which stages need to be included/configured for a reproducible clone.

## Project goals

- Ingest document content and metadata into a vector-searchable knowledge base.
- Answer questions using retrieved context rather than relying only on model memory.
- Use an agentic/stateful workflow to classify, retrieve, reformulate, decompose, generate, review, and validate answers.
- Return source references when the index provides source metadata.
- Present answers in a responsive phone/desktop chat interface.
- Evaluate the broader RAG workflow with Ragas experiments from Month 3.

## High-level architecture

~~~mermaid
flowchart LR
    Docs[Source documents] --> Ingest[Month 1 ingestion]
    Ingest --> Atlas[(MongoDB Atlas vector/document index)]
    User[User] --> Flutter[Flutter app: Android / Windows]
    Flutter -->|POST /ask, question + session_id| API[Month4-2 FastAPI]
    API --> Graph[LangGraph RAG workflow]
    Graph --> Atlas
    Graph --> Model[Configured LLM and embedding services]
    Graph --> Redis[(Redis request controls)]
    Graph --> Checkpoint[(SQLite checkpoints)]
    Graph -. optional entity memory .-> Neo4j[(Neo4j)]
    Graph -->|answer, timing, sources/page if available| API
    API --> Flutter
    Eval[Month 3 Ragas evaluation work] -. offline evaluation of sample questions .-> Graph
~~~

### End-to-end flow

1. **Ingestion:** The earlier ingestion stage prepares document chunks and metadata and stores them in MongoDB Atlas. The runtime backend expects its configured vector index/database/collection and metadata to agree with this ingestion output.
2. **Question request:** The Flutter app sends a question and optional session identifier to the FastAPI `/ask` endpoint.
3. **Routing and retrieval:** The LangGraph pipeline classifies the question, retrieves context, checks sufficiency, and can reformulate or decompose questions before generation.
4. **Answer checks and memory:** The graph can review or validate generated text and update configured conversation/profile/entity memory.
5. **Response and UI:** FastAPI returns the answer, timing and optional source references. Flutter renders Markdown and stores recent conversation history locally.
6. **Evaluation:** Month 3 contains Ragas evaluation work for sample questions. Treat those evaluations as a separate development/evaluation stage unless you explicitly wire them into a deployment workflow.

## Components

| Component | Purpose | Main technologies |
|---|---|---|
| Document ingestion (earlier project stage) | Prepare and index document chunks with metadata | Python, MongoDB Atlas vector search |
| Month4-2 backend | API and request-time RAG/agent pipeline | Python 3.11, FastAPI, LangGraph |
| Retrieval and memory | Vector retrieval, checkpointing and optional entity memory | MongoDB Atlas, SQLite, Redis, optional Neo4j |
| Model integrations | Embedding and answer generation | Hugging Face embeddings, configured Groq/Ollama paths |
| Evaluation (Month 3) | Evaluate sample RAG question/answer runs | Ragas |
| Client app | Responsive chat for Android and Windows | Flutter, Dart, HTTP, SharedPreferences, Markdown |
| Local operations | Start services and report failures | Docker Compose, proxy, watchdog/notifier, optional tunnel |

## Repository layout

If combined into one repository, a clear layout would be:

~~~text
rag-assistant/
├── README.md                         # This full-project overview
├── MONTH4-2/
│   ├── README.md                     # Use README_MONTH4_2_BACKEND.md
│   ├── serve.py
│   ├── api_layer.py
│   ├── rag_pipeline/
│   ├── docker-compose.yml
│   └── ...
└── rag_chat_app/
    ├── README.md                     # Use README_FLUTTER_APP.md
    ├── lib/
    ├── android/
    ├── windows/
    ├── pubspec.yaml
    └── ...
~~~

The Month 1 ingestion and Month 3 evaluation source files are separate project stages. Include those directories/notebooks or link to their repositories if you want a new contributor to reproduce the entire research and data preparation process. Do not claim the full workflow is reproducible from only the backend and Flutter directories.

## Local setup

### 1. Start the backend

Prerequisites: Docker Desktop/Compose, the required local environment variables, and access to the external database/model services used by the chosen configuration.

Create a local `.env` file in the Month4-2 project root using the example/configuration notes in that repository. Common required backend settings include `MONGO_URI`, `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, and `RAG_API_KEYS`; provider integrations can require additional credentials. Set real values locally and keep them out of Git.

~~~powershell
cd MONTH4-2
docker compose up --build -d
docker compose ps
docker compose logs -f rag
~~~

The Compose API port defaults to `127.0.0.1:8080` on the development computer. A local proxy is also published on `127.0.0.1:8088` by default. Use `docker compose ps` to confirm the actual bindings on your machine.

### 2. Configure and run Flutter

Install a compatible Flutter SDK, then:

~~~powershell
cd rag_chat_app
flutter pub get
flutter devices
flutter run -d DEVICE_ID
~~~

For Windows desktop, install Visual Studio with the **Desktop development with C++** workload and run:

~~~powershell
flutter run -d windows
~~~

Set the endpoint in `lib/chat_service.dart` to the backend's `/ask` route. The source currently uses a hardcoded endpoint value; replace it with your own reachable address before running or publishing. Desktop local testing can use the local API port. A physical phone cannot reach the computer through `127.0.0.1`; configure a reachable development route and protect it appropriately.

### 3. Verify the end-to-end path

- Confirm the backend is healthy and ready.
- Confirm the Flutter endpoint targets the current backend, not an expired quick-tunnel address.
- Send one question using a document known to be indexed.
- Check the API response for `answer`, `session_id`, `sources`, and optional `page`.
- Check the backend logs if the app reports a timeout or connectivity error.
- Verify that the ingested record includes title/page metadata if the UI is expected to show source details.

This README does not represent a test run; execute your local integration checks after configuring your own services.

## GitHub and credential safety

Before making either repository public:

1. Keep `.env` and its backup variants untracked. Confirm Git is not tracking them.
2. Inspect staged files, notebook outputs, fixtures, JSON indexes, logs, screenshots, archives, and history for API keys, database URIs, private tunnel URLs, notification topics, and real user/document data.
3. Do not embed a long-lived backend key in the Flutter app. A key packaged in a client application can be extracted.
4. Replace the current hardcoded client endpoint with local/build-time configuration and do not include a private or temporary tunnel URL in the README.
5. If a credential has ever entered Git history, rotate it; deleting the line in a later commit is not enough.
6. Add a LICENSE only if you are ready to grant the permissions described by that license.

## Current boundaries and known limitations

- The app's conversation history is stored locally per device. It is not synchronized between Android and Windows and is not tied to a user account.
- Source references and page numbers depend on ingestion and retrieval metadata. They may be absent if metadata was not indexed or returned.
- Ragas evaluation from Month 3 measures sample runs; it cannot guarantee perfect answers or replace production monitoring.
- Latency varies with retrieval, database readiness, model/provider response time, cold starts, and query complexity. Do not advertise a fixed latency unless you have measured it under stated conditions.
- The local Docker setup and optional quick tunnel are for development/demo use. Public deployment needs a stable host, TLS, secure authentication, secret management, backups, access controls, monitoring, and a reviewed data-retention policy.
- The current backend and frontend are personal-project components. Review reliability, privacy, security, and data handling before using real customer documents.

## Suggested next steps

- Move the Flutter endpoint into a build/runtime configuration mechanism.
- Confirm that document IDs, titles, and page metadata survive the ingestion-to-retrieval path.
- Add an automated integration check that runs one known question against a small, non-sensitive test corpus.
- Record Ragas evaluation data and report actual metrics, dataset size, and evaluation conditions rather than making broad accuracy claims.
- Document the exact MongoDB index definition and required collection schema without including credentials.
- Add a top-level license and contribution guidance only when you are ready for external use.

## License

No license is specified. Until a license is added, do not assume others have permission to reuse or redistribute the code.
