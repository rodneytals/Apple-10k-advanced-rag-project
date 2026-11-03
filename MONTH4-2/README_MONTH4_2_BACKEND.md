# Month 4-2 — RAG API and Agent Pipeline

Month 4-2 is the Python backend/runtime portion of a personal, learning-oriented Retrieval-Augmented Generation (RAG) project. It exposes a FastAPI service around a LangGraph workflow, retrieves document context from MongoDB Atlas, generates and reviews answers, and runs supporting services through Docker Compose.

This README describes the code and deployment shape. It does not claim that every optional integration is enabled in every environment, or that the project is production-ready.

## What it does

- Accepts question requests through a FastAPI endpoint.
- Routes a question through a stateful LangGraph pipeline.
- Retrieves document chunks and source metadata from MongoDB Atlas.
- Classifies questions and checks whether retrieved context appears sufficient.
- Can reformulate a query and retrieve again, or decompose multi-hop questions before generation.
- Generates an answer through the configured language-model integration and includes fallback paths where configured.
- Runs review and output-validation steps before returning a result.
- Tracks a conversation session and supports conversation/profile memory.
- Uses Redis-backed request/rate controls and SQLite-backed LangGraph checkpoints.
- Can store topic/entity memory in Neo4j when that integration is configured and reachable.
- Returns source references when the ingestion/index metadata contains them.
- Includes health/readiness/status endpoints, a reverse-proxy front door, and a watchdog/notification service in the Compose setup.
- Contains an optional visual/chart retrieval route backed by the bundled visual index. Treat this as an experimental path and verify its index contents before public release.

## Architecture

~~~mermaid
flowchart LR
    Client[Flutter client or API consumer] --> Proxy[Optional Caddy front door]
    Proxy --> API[FastAPI service]
    API --> Graph[LangGraph request workflow]
    Graph --> Mongo[(MongoDB Atlas vector/document data)]
    Graph --> LLM[Configured LLM provider]
    Graph --> Redis[(Redis rate and request controls)]
    Graph --> SQLite[(SQLite checkpoints)]
    Graph -. optional topic memory .-> Neo4j[(Neo4j)]
    Graph --> Result[Answer, status, timing, source references]
    API --> Health[healthz / readyz / statusz]
    Watchdog[Watchdog and notifier] --> API
    Watchdog --> Proxy
    Tunnel[Optional tunnel profile] --> Proxy
~~~

The actual availability of MongoDB Atlas, Neo4j, Redis, model providers, and the optional tunnel depends on the environment variables and Compose profile used at runtime.

## API

### Ask a question

~~~http
POST /ask
Content-Type: application/json
~~~

Example request:

~~~json
{
  "question": "What does the document say about the reporting period?",
  "session_id": "optional-session-id"
}
~~~

The API also applies the authentication configured by the backend. Add the header expected by the current API implementation when calling it. Keep credentials on the server; do not publish them in this README, a notebook, a Docker image, or a Flutter application bundle.

The response model includes:

- `request_id` and `session_id`
- `answer`
- optional classification, retrieval source, reviewer verdict, and warning fields
- `sources`, each with a title and an optional page value
- elapsed time in milliseconds

Example response shape:

~~~json
{
  "request_id": "example-request-id",
  "session_id": "example-session-id",
  "answer": "Example answer based on the retrieved context.",
  "classification": "SIMPLE",
  "retrieval_source": "local",
  "reviewer_verdict": "PASS",
  "warning": null,
  "sources": [
    {
      "title": "Example source document",
      "page": "12"
    }
  ],
  "elapsed_ms": 1234
}
~~~

`page` and even `sources` may be absent or empty when the indexed data does not contain source metadata or retrieval returns no source to cite. The backend can return `/ask` results with a missing page value; the ingestion/indexing stage must preserve page metadata for the UI to show it.

### Health endpoints

- `GET /healthz` — process/liveness check.
- `GET /readyz` — checks whether configured dependencies are ready.
- `GET /statusz` — status information for monitoring.

The optional Caddy front door is configured separately from the FastAPI routes. Its current configuration is intended to expose only selected paths (notably question requests and health checks). Check `Caddyfile` before changing proxy behavior.

## Services and storage

- **MongoDB Atlas:** document/vector retrieval and profile-related records. The expected vector index, database, collection, embedding dimensions, and metadata schema must match the ingestion pipeline.
- **Redis:** request/rate controls and related short-lived state.
- **SQLite:** LangGraph checkpoint storage under the local `checkpoints` directory/volume.
- **Neo4j:** optional graph/entity memory.
- **Model and embedding integrations:** configured through environment variables and their provider SDKs. The code includes Groq and local Ollama paths; availability depends on configuration.
- **Caddy:** optional restricted front door for the API.
- **Tunnel:** optional Compose profile. A quick-tunnel hostname can change when the tunnel restarts; do not put an ephemeral/private address in public documentation.
- **Watchdog:** checks service health and can send notifications using the configured notifier.

## Repository map

~~~text
MONTH4-2/
├── api_layer.py              # FastAPI routes, request/response models, auth and API behavior
├── serve.py                  # Builds and starts the API application
├── rag_pipeline/
│   ├── pipeline.py           # LangGraph workflow and RAG integration
│   ├── usage_context.py      # Usage/telemetry support
│   └── FREEZE_REPORT.md      # Report from notebook-to-package generation
├── docker-compose.yml        # API, Redis, proxy, watchdog, optional tunnel
├── Dockerfile                # Python container image
├── requirements.docker.txt   # Pinned Python dependencies
├── Caddyfile                 # Front-door routing rules
├── watchdog.py               # Dependency/API monitoring
├── monitoring.py             # Monitoring checks
├── notifiers.py              # Alert delivery
├── freeze_kernel.py          # Notebook-to-Python pipeline generation
└── day*.ipynb                # Development and experiment notebooks
~~~

`pipeline.py` is generated from executed notebook cells by `freeze_kernel.py`. Treat the notebooks and generation process as the source of truth when making pipeline changes; a future freeze can overwrite manual edits to the generated file. Review the freeze report and generated diff before rebuilding.

## Local run with Docker Compose

### Prerequisites

- Docker Desktop with Docker Compose enabled.
- A local `.env` file containing the credentials/configuration required by the services you intend to use.
- Reachable MongoDB Atlas and, if enabled, Neo4j and external model/search services.

Use `rag_pipeline/env.example` as a variable-name reference. The pipeline uses `MONGO_URI`, `NEO4J_URI`, `NEO4J_USERNAME`, and `NEO4J_PASSWORD`; the API uses `RAG_API_KEYS` for server-side request identity. Provider SDKs may also require their own credentials. Set real values only in your local, ignored `.env` file.

### Start

From the Month4-2 directory:

~~~powershell
docker compose up --build -d
docker compose ps
docker compose logs -f rag
~~~

The API container listens on port `8000`. The default Compose host binding is local-only at `127.0.0.1:8080`; the optional proxy uses `127.0.0.1:8088`. Check `docker compose ps` after starting because local environment overrides can change the published ports.

The first container start may take longer while Docker builds the image and required embedding/model assets are downloaded and cached.

### Optional tunnel

The tunnel is not part of the default Compose profile. To opt in, use:

~~~powershell
docker compose --profile tunnel up -d
~~~

Use the tunnel URL shown by the tunnel service only for temporary testing. Quick-tunnel hostnames are not stable deployment addresses. Do not publish the hostname, server key, or notifier topic in GitHub.

### Stop

~~~powershell
docker compose down
~~~

This stops the services. Named volumes are separate; do not remove them unless you intend to delete cached models/checkpoints/data.

## Configuration and GitHub safety

- Never commit `.env`, backup copies of `.env`, API keys, database URIs, passwords, notification topics, private tunnel addresses, or customer documents.
- `RAG_API_KEYS` is a server-side secret. Do not embed a long-lived backend key in Flutter; client application bundles can be inspected.
- Review notebook outputs as well as source files. Notebook cells can contain credentials or private data even when `.env` is ignored.
- Review `day14_visual_index.json`, logs, fixtures, backups, and generated artifacts for content you do not have permission to publish.
- Before pushing, inspect the staged file list and diff. If a credential was ever committed, remove it from history and rotate it; adding `.gitignore` afterward does not invalidate the leaked credential.
- This repository's `.gitignore` excludes several local files/directories, but it is not a substitute for reviewing what Git is tracking.

## Known boundaries

- Retrieval quality and answer latency depend on the model provider, vector index, database availability, model startup/cache state, and question complexity. This README does not promise a fixed response time or perfect accuracy.
- Source-page display depends on ingestion metadata. If a page is missing from MongoDB records, the backend cannot reconstruct it reliably at answer time.
- Ragas evaluation work was developed in the broader Month 3 project. It is an evaluation workflow, not a claim that every live Month4-2 request is evaluated by Ragas.
- The visual retrieval/index path should be treated as experimental unless separately validated against the intended image/chart corpus.
- Local Docker deployment and a temporary tunnel are development/demo setups; they are not a complete production hosting, identity, authorization, backup, or compliance plan.

## Development notes

- Keep service credentials and local endpoints outside committed files.
- If editing the generated pipeline, update the source notebook/generation process and inspect the generated diff.
- Existing Python test files are in the repository. Run the relevant tests and dependency/health checks in your own environment before describing a change as verified.

## License

No license is specified here. Add a license file before inviting reuse or accepting external contributions.
