import uuid

from config import (
    USE_PINECONE,
    PINECONE_API_KEY,
    INDEX_NAME,
    PINECONE_CLOUD,
    PINECONE_REGION,
)

_pc = None
_index = None


def _list_index_names(pc):
    try:
        return pc.list_indexes().names()
    except Exception:
        try:
            return [idx["name"] for idx in pc.list_indexes()]
        except Exception:
            return []


def init_pinecone():
    global _pc, _index

    if not USE_PINECONE:
        print("🔹 Pinecone disabled (fallback mode)")
        return

    if not PINECONE_API_KEY:
        raise RuntimeError("PINECONE_API_KEY is not set")

    from pinecone import Pinecone, ServerlessSpec  # type: ignore

    _pc = Pinecone(api_key=PINECONE_API_KEY)

    existing = _list_index_names(_pc)
    if INDEX_NAME not in existing:
        _pc.create_index(
            name=INDEX_NAME,
            dimension=384,
            metric="cosine",
            spec=ServerlessSpec(
                cloud=PINECONE_CLOUD,
                region=PINECONE_REGION,
            ),
        )

    _index = _pc.Index(INDEX_NAME)


def store_resume(user_email, embedding, text):
    if not USE_PINECONE:
        return

    if _index is None:
        init_pinecone()

    vector_id = f"{user_email}-{uuid.uuid4().hex}"
    _index.upsert(
        vectors=[
            {
                "id": vector_id,
                "values": embedding,
                "metadata": {
                    "email": user_email,
                    "text": text,
                },
            }
        ]
    )
