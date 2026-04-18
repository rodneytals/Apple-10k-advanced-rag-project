import os
from dotenv import load_dotenv
from llama_index.llms.huggingface import HuggingFaceLLM
from llama_index.llms.groq import Groq
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core import Settings

load_dotenv()

class Config:
    # LOADING API KEYS
    GROQ_API_KEY = os.getenv('GROQ_API_KEY')
    MONGO_URI = os.getenv('MONGO_URI')
    LLAMA_CLOUD_API_KEY = os.getenv('LLAMA_CLOUD_API_KEY')

    # DATABASE SETTINGS
    DB_NAME = 'month1_RAG_database'
    COLLECTION_NAME = 'rag_collection'
    INDEX_NAME = 'final_index'
    DOCSTORE_NAMESPACE = "month_1_collection"
    

    @staticmethod
    def setup_settings():
        '''Setting up global settings and returning local reasoner'''
        
        if not Config.GROQ_API_KEY:
            raise ValueError('GOQ_API_KEY is missing from .env')
        
        # main powerful llm
        Settings.llm = Groq(
            model = 'llama-3.3-70B-versatile',
            api_key = Config.GROQ_API_KEY
        )

        # Setting embed model
        Settings.embed_model = HuggingFaceEmbedding(model_name= 'BAAI/bge-m3')

        # helper for local logic tasks (step-back and reasoning )
        localReasoner = HuggingFaceLLM(
            model_name= 'TinyLlama/TinyLlama-1.1B-Chat-v1.0',
            tokenizer_name = 'TinyLlama/TinyLlama-1.1B-Chat-v1.0',
            device_map = 'cpu',
            context_window = 2048,
            max_new_tokens = 150,
        )
        return localReasoner
