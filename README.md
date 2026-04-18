# 🚀 Apple 10-K Advanced RAG: Month 1 Capstone

A production-grade Retrieval-Augmented Generation (RAG) pipeline optimized for complex, table-heavy financial documents like Apple's 10-K filings.

This project successfully integrates **hierarchical chunking**, **sentence windowing**, **Auto-Merging Retrieval**, **step-back prompting**, and **FlashRank reranking** into a single cohesive system. These were some of the Advanced retrieval techniques I've learned over the past 28 days and combined these ones specifically due to the nature of data of the apple 10-k filings. Other methods will be seen in the coming months of my work.


## 🛣️ Project Vision 🛣️

The goal was to build a robust, persistent RAG system capable of handling dense financial filings with high accuracy. After weeks of iteration, this pipeline now demonstrates:

- Precise text sanitization tailored for SEC documents
- Dual-path node creation for both broad context and fine-grained precision
- Proper parent-child relationship storage for reliable Auto-Merging
- Expert multi-stage retrieval logic


## 🤖 Technical Architecture 🤖

### 1. Sanitization Layer
Custom *TextSanitizer* cleans raw LlamaParse output by:
- Removing repetitive SEC headers and footers
- Normalizing table artifacts and whitespace
- Preserving important financial table structures

This step significantly improves embedding quality.

### 2. Multi-Strategy Ingestion - THE DUAL-PATH
- **HierarchicalNodeParser**: Creates structured nodes: parent(1024) → mid(512) → child(256) to enable Auto-Merging
- **SentenceWindowNodeParser**: Adds surrounding 3-sentence context for better precision

All nodes (including parent nodes) are properly persisted in MongoDocumentStore.

### 3. Persistent Cloud Storage
- **MongoDB Atlas Vector Search** using `BAAI/bge-m3` embeddings which have **1024** dimensions with a search done using **cosine similarity**
- **MongoDocumentStore** to maintain parent-child relationships across sessions

### 4. Expert Retrieval Engine
- **Step-back prompting** was done using a small local LLM **TinyLlama/TinyLlama-1.1B-Chat-v1.0** for better query understanding
- **AutoMergingRetriever** with dynamic parent node merging
- **MetadataReplacementPostProcessor** to be used for the sentence window context
- **FlashRank reranker** as the final quality gate as it uses cross-encoding to produce final scores to chunks before they are sent to the LLM


## 📂 Project Structure 🗼
advanced-RAG-month1/
├── src/
│   ├── config.py          # LLM, embedding model & MongoDB Atlas configuration
│   ├── ingestion.py       # contains text sanitization, parsing & hierarchical node creation
│   └── retrieval.py       # contains the Auto-merging, step-back prompting & reranking logic
├── data/
│   └── 10-K.Apple.pdf     # Source Apple 10-K document
├── main.py                # Main orchestrator and entry point
├── run.bat                # for easy launching on Windows as recommended
├── .env                   # contains environment variables (API keys)
└── requirements.txt       # contains the project dependencies
⚡Quick Start⚡

1. **Prerequisites**
Python 3.10+

MongoDB Atlas Cluster (Vector Search Index enabled) # sign up to MongoDB Atlas if you don't yet have an account.

API Keys: Groq (LLM), LlamaCloud (Parsing)

2. **Installation**
Bash
git clone https://github.com/yourusername/apple-10k-rag.git
cd apple-10k-rag
pip install -r requirements.txt
3. Configuration
Add your keys to a .env file:
*Code snippet*
GROQ_API_KEY=your_key
MONGO_URI=your_mongodb_atlas_uri
LLAMA_CLOUD_API_KEY=your_parse_key
4. Execution
Doubleclick *run.bat*


## 🎓 Lessons Learned 👨‍🎓

- Sanitization is 80% of success in financial RAG.
- AutoMerging will only work reliably when parent nodes are correctly stored in the docstore.
- Embedding model consistency between ingestion and retrieval is critical.
- Persistent storage with MongoDB Atlas adds complexity but enables true production readiness.
- Connection stability (SSL issues) can be a major bottleneck when using cloud databases.

## Future Work (Month 2+) 🚀🛩️

- Hybrid search (vector + keyword)    *month 2*
- uery routing and agentic RAG        *month 2*
- Evaluation metrics (RAGAS / ARES)   *month 3*
- Local-first fallback using ChromaDB *month 2*
- Multi-document support              *month 4*
- The frontier (GraphRAG and production) *month 4* 

☎️Contact
 Third-Year Engineering Student | AI Developer LinkedIn | Portfolio | Twitter

