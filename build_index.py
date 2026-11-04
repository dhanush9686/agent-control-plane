import os
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document

CORPUS_PATH = 'data/corpus.txt'
INDEX_PATH = 'data/faiss_index'

def load_and_chunk(path):
    with open(path, 'r', encoding = 'utf-8') as f:
        raw = f.read()
        
        sections = raw.split("=== ")[1:]

        documents = []

        for section in sections:
            header, _, body = section.partition("===")
            source = header.strip()
            paragraphs = [p.strip() for p in body.strip().split("\n\n") if p.strip()]

            for para in paragraphs:
                documents.append(Document(page_content=para, metadata={"source": source}))

    return documents

def main():
    print("Parsing and chunking corpus...")
    docs = load_and_chunk(CORPUS_PATH)
    print(f"Created {len(docs)} chunks")
    if not docs:
        raise ValueError("No chunks created — check corpus.txt formatting against the === markers")
    for d in docs[:3]:
        print(f"  [{d.metadata['source']}] {d.page_content[:60]}...")

    print("\nLoading local embedding model (first run downloads ~80MB, then it's cached)...")
    embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

    print("Embedding chunks and building FAISS index...")
    vectorstore = FAISS.from_documents(docs, embeddings)

    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    vectorstore.save_local(INDEX_PATH)
    print(f"Index saved to {INDEX_PATH}")


if __name__ == "__main__":
    main()