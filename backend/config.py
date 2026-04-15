from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "./notes.db"
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    chroma_collection: str = "notes"
    embedding_base_url: str = "http://localhost:8080"
    embedding_model: str = "nomic-embed-text"
    embedding_batch_size: int = 32
    chunk_size: int = 512
    chunk_overlap: int = 64
    attachment_dir: str = "./attachments"
    app_base_url: str = "https://localhost:8443"
    summary_base_url: str | None = None
    summary_model: str = "gpt-4o-mini"
    summary_api_key: str | None = None
    chat_llm_base_url: str | None = None  # falls back to summary_base_url
    chat_llm_model: str | None = None     # falls back to summary_model
    chat_n_results: int = 8               # note chunks injected as RAG context
    chat_port: int = 8084                 # port for the RAG chat API

    model_config = {"env_file": ".env"}


settings = Settings()
