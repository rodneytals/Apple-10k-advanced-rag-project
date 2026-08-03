" real time creation engine"
from pymongo import MongoClient, AsyncMongoClient
from llama_index.core import StorageContext, VectorStoreIndex
from llama_index.vector_stores.mongodb import MongoDBAtlasVectorSearch
from llama_index.storage.docstore.mongodb import MongoDocumentStore
from llama_index.core.node_parser import (
    SentenceWindowNodeParser,
    HierarchicalNodeParser, 
    get_leaf_nodes
)
from llama_parse import LlamaParse
from src.config import Config
import re


class TextSanitizer:
    @staticmethod
    def clean(text: str) -> str:
        text = re.sub(r'Table of Contents', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\|+', '|', text)
        text = re.sub(r'Apple Inc\..*?Form 10-K.*?(\d+)', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()


class IngestionEngine:
    def __init__(self):
        # setting up the mongoDb client    
        self.client = MongoClient(Config.MONGO_URI)
        self.asyncMongoClient = AsyncMongoClient(Config.MONGO_URI)
        
        # loading the document store
        self.docstore = MongoDocumentStore.from_uri(
            uri=Config.MONGO_URI, 
            db_name=Config.DB_NAME,
            namespace=Config.DOCSTORE_NAMESPACE
        )
        # connecting to our vector store
        self.vectorStore = MongoDBAtlasVectorSearch(
            mongodb_client=self.client,
            async_mongodb_client=self.asyncMongoClient,
            db_name=Config.DB_NAME,
            collection_name=Config.COLLECTION_NAME,
            vector_index_name=Config.INDEX_NAME,
            embedding_key='embedding'
        )
        # loading the storage context
        self.storageContext = StorageContext.from_defaults(
            vector_store=self.vectorStore, 
            docstore=self.docstore
        )
        # loading our parser
        self.parser = LlamaParse(
            api_key=Config.LLAMA_CLOUD_API_KEY, 
            result_type='markdown'
        )
        
      

    def is_collection_populated(self):
        # this function checks your vector store in case you run the code again to prevent re-indexing 
        try:
            count = self.client[Config.DB_NAME][Config.COLLECTION_NAME].count_documents({})
            return count > 0
        except Exception:
            return False

    def process_10k_doc(self, file_path):
        # 1. Parse the PDF
        documents = self.parser.load_data(file_path=file_path)

        # 2. Sanitizing optimized for SEC documents
        for doc in documents:
            cleaned = TextSanitizer.clean(doc.text)
            doc = doc.model_copy(update={'text': cleaned})
            doc.metadata.update({
                'document_type': '10-K',
                'company': 'Apple Inc.',
                'sanitized': True
            })
        
        # Keep only essential metadata to stay under Pinecone's 40KB limit
        for doc in documents:
            # Keep only necessary metadata
            essential_metadata = {
                'document_type': doc.metadata.get('document_type'),
                'company': doc.metadata.get('company'),
                'sanitized': doc.metadata.get('sanitized', True),
            }
            doc.metadata = essential_metadata
            
        # 3. Create hierarchical nodes (parents + children) for AutoMerging
        hierarchical_parser = HierarchicalNodeParser.from_defaults(
            chunk_sizes=[1024, 512, 256]
        )
        all_nodes = hierarchical_parser.get_nodes_from_documents(documents)
        leaf_nodes = get_leaf_nodes(all_nodes)

        # IMPORTANT: Store ALL nodes (including parents) in docstore
        self.storageContext.docstore.add_documents(all_nodes)

        # 4. Adding sentence window nodes for better context
        window_parser = SentenceWindowNodeParser.from_defaults(
            window_size=3,
            window_metadata_key='window',
            original_text_metadata_key='original_text'
        )
        window_nodes = window_parser.get_nodes_from_documents(documents)

        # 5. Combining everything
        final_nodes = window_nodes + leaf_nodes

        # 6. Building index
        index = VectorStoreIndex(
            final_nodes,
            storage_context=self.storageContext,
            show_progress=True
        )

        self.storageContext = index.storage_context
        return index