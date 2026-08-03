# => this will handle the expert logic

from llama_index.core import QueryBundle
from llama_index.core.postprocessor import MetadataReplacementPostProcessor
from llama_index.postprocessor.flashrank_rerank import FlashRankRerank
from llama_index.core.schema import NodeWithScore
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.retrievers import BaseRetriever, AutoMergingRetriever
from typing import List

#-----EXPERT RETRIEVER CLASS---------
class ExpertRetriever(BaseRetriever):
    def __init__(self, retrieval_instance):
        self.retrieval_instance = retrieval_instance
        super().__init__()

    def _retrieve(self, query_bundle: QueryBundle) -> List[NodeWithScore]:
        '''handshake that calls the custom logic when the query engine wants data'''
        nodes, _ = self.retrieval_instance.expert_retrieve(query_bundle.query_str)
        return nodes

#-----------THE RETRIEVAL ENGINE--------- this handles all the retrieval logic
class RetrievalEngine:
    def __init__(self, index, local_reasoner):
        self.index = index
        self.local_llm = local_reasoner
        self.reranker = FlashRankRerank(model='ms-marco-TinyBERT-L-2-v2', top_n=5)

    def step_back_query(self, query: str) -> str:
        # abstraction or step-back prompting
        # this is done to generate a broader search concept.
        prompt = f"""Rephrase this question to be broader and more general:

        Question: {query}

        Broader question:"""

        # this works reliably for both groq and huggingfaceLLM
        response = self.local_llm.complete(prompt)
        return str(response.text if hasattr(response, 'text') else response).strip()

    def expert_retrieve(self, query: str):
        stepbackConcept = self.step_back_query(query=query)
        print(f"Step-back concept: {stepbackConcept}")

        # we bundle the original query with the step-back concept for better hits
        queryBundle = QueryBundle(
            query_str=query,
            custom_embedding_strs=[stepbackConcept]
        )
        
        initialNodes = None

        try:
            # Try AutoMerging first
            base_retriever = self.index.as_retriever(similarity_top_k=20)
            automerge_retriever = AutoMergingRetriever(
                base_retriever,
                self.index.storage_context,
                simple_ratio_thresh=0.35,
                verbose=True
            )

            initialNodes = automerge_retriever.retrieve(queryBundle)

        except ValueError as e:
            if "not found" in str(e):
                print(f"***Some parent nodes missing in docstore. Falling back to basic retrieval.")
                # Fallback: use normal retriever without merging
                base_retriever = self.index.as_retriever(similarity_top_k=20)
                initialNodes = base_retriever.retrieve(queryBundle)
            else:
                raise  # re-raise if it's a different error
       

        # doing a light context reconstruction 
        metadataProcessor = MetadataReplacementPostProcessor(target_metadata_key="window")
        nodesWithContext = metadataProcessor.postprocess_nodes(initialNodes)

        final_nodes = self.reranker.postprocess_nodes(nodesWithContext, query_bundle=queryBundle)

        valid_nodes = [n for n in final_nodes if n.score is None or n.score > 0.05]

        print(f"DEBUG: After reranking → {len(valid_nodes)} nodes kept")

        return valid_nodes, stepbackConcept

    def get_query_engine(self):
        customRetriever = ExpertRetriever(self)
        return RetrieverQueryEngine.from_args(retriever=customRetriever)