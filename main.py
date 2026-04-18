from src.config import Config
from src.ingestion import IngestionEngine
from src.retrieval import RetrievalEngine
import os
from llama_index.core import Settings, VectorStoreIndex


def run_pipeline():
    # this ensures we use groq and bge-m3 as defined in the config file.
    local_reasoner = Config.setup_settings()

    print("------- Starting Real-Time Ingestion....connecting to MongoDB Atlas-------")
    
    ingestor = IngestionEngine()

    pdfPath = "data/10-K.Apple.pdf"
    
    if os.path.exists(pdfPath) and not ingestor.is_collection_populated():
        print(f"Found {pdfPath}. Starting ingestion & Sanitization...")
        index = ingestor.process_10k_doc(pdfPath)
        # this triggers: Llamaparse=> Dual-path nodes => mongodb indexing 
    else:
        print(" => Data already exists in MongoDB. Loading existing index...")
        index = VectorStoreIndex.from_vector_store(
            vector_store=ingestor.vectorStore,
            storage_context=ingestor.storageContext,
            embed_model=Settings.embed_model
        )
    # our retrieval engine setup follows here
    print("Initializing expert retrieval (Auto-merging + step-back)...")
    retrievalFactory = RetrievalEngine(index, local_reasoner)

    # this uses the RetrievalEngine class to build our custom logic
    queryEngine = retrievalFactory.get_query_engine()

    # executing the sample query
    userQuery = "Compare the iPhone revenue in 2023 vs 2022."
    print(f"\nQuery: {userQuery}")
    print("Processing...")

    response = queryEngine.query(userQuery)
    
    # output the results
    print("\n" + "="*50)
    print("FINAL RESPONSE:")
    print(response)
    print("="*50)
    
    # optional: printing some of the sourcenodes to havae a look at some of the auto-merging in action 
    print("\nSources used for this answer:")
    for i, node in enumerate(response.source_nodes[:3]):
        score = node.score if node.score is not None else 0.0
        print(f"Source {i+1} (Score: {score:.3f}): {node.text[:150]}...")

if __name__ == "__main__":
    run_pipeline()