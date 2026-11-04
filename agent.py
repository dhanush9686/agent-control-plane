from typing import TypedDict, List
from langchain_core.documents import Document
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field

load_dotenv()

INDEX_PATH = "data/faiss_index"


class AgentState(TypedDict):
    question: str
    retrieved_docs: List[Document]
    search_query: str
    search_attempts: int
    sufficient_context: bool
    judge_reasoning: str           # why the judge decided what it did
    answer: str


# Loaded once when the file is imported, not on every call.
_embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
_vectorstore = FAISS.load_local(INDEX_PATH, _embeddings, allow_dangerous_deserialization=True)

llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0)


def retrieve_node(state: AgentState) -> dict:
    query = state.get("search_query") or state["question"]

    results = _vectorstore.similarity_search(query, k=3)

    existing_content = {doc.page_content for doc in state.get("retrieved_docs", [])}
    new_docs = [doc for doc in results if doc.page_content not in existing_content]

    return {
        "retrieved_docs": state.get("retrieved_docs", []) + new_docs,
        "search_attempts": state.get("search_attempts", 0) + 1,
    }


class JudgeVerdict(BaseModel):
    sufficient: bool = Field(description="True only if the context explicitly contains the information needed to answer the question")
    reasoning: str = Field(description="One or two sentences explaining the decision")


_judge_llm = llm.with_structured_output(JudgeVerdict)


def format_docs(docs) -> str:
    # Include each chunk's source label so the LLM sees facts like "unreleased"
    return "\n\n".join(f"[Source: {d.metadata['source']}]\n{d.page_content}" for d in docs)


def judge_node(state: AgentState) -> dict:
    context = format_docs(state["retrieved_docs"])
    prompt = f"""You are checking whether retrieved context is enough to answer a question.


Question: {state["question"]}

Retrieved context:
{context}

Rules:
- Answer sufficient=true ONLY if the context explicitly states the information needed.
- If the question asks for something the context never mentions, answer sufficient=false,
  even if the context contains related or similarly-shaped facts about other topics.
- Do not use outside knowledge. Judge only what is written above."""

    verdict = _judge_llm.invoke(prompt)
    return {"sufficient_context": verdict.sufficient, "judge_reasoning": verdict.reasoning}

def refine_query_node(state: AgentState) -> dict:
    prompt = f"""A search over a small film-knowledge corpus did not return enough information.

Original question: {state["question"]}
Last search query: {state.get("search_query") or state["question"]}
Why it was not enough: {state["judge_reasoning"]}

Write ONE new search query (under 15 words) that targets the specific missing information.
Use different keywords than the last query. Output only the query, nothing else."""

    new_query = llm.invoke(prompt).content.strip()
    return {"search_query": new_query}


if __name__ == "__main__":
    test_questions = [
        "Who kills Garuda, and how does Rocky's story end?",
        "Who plays Deva in Salaar?",
    ]
    MAX_ATTEMPTS = 2
    for q in test_questions:
        state = {"question": q, "retrieved_docs": [], "search_attempts": 0, "search_query": ""}
        print(f"\nQ: {q}")
        while True:
            state.update(retrieve_node(state))
            state.update(judge_node(state))
            print(f"  attempt {state['search_attempts']}: query='{state.get('search_query') or q}' "
                  f"| chunks={len(state['retrieved_docs'])} | sufficient={state['sufficient_context']}")
            if state["sufficient_context"] or state["search_attempts"] >= MAX_ATTEMPTS:
                break
            state.update(refine_query_node(state))