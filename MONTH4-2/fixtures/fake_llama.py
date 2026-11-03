# Stand-in for a third-party library that keeps global settings (like LlamaIndex's Settings).
class _Settings:
    embed_model = None
    llm = None
Settings = _Settings()
