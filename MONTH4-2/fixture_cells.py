"""A deliberately messy 'notebook': old versions, test cells, a failing cell, a syntax error, magics."""

def clean_cells():
    return [
# 0 magic
"%pwd",
# 1 imports + config side effects
'''import os, json, re, time, threading, statistics, random
from dataclasses import dataclass
from typing import TypedDict, Optional, Literal
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from fake_llama import Settings
load_dotenv()
os.environ["APP_MODE"] = "prod"
''',
# 2 constants and shared state
'''GROQ_KEY = os.getenv("FAKE_GROQ_KEY", "")
MONGO_URI = os.getenv("FAKE_MONGO_URI", "")
CHECKPOINT_DB = "checkpoints/rag_checkpoints.db"
ROUTER_PROMPT = """You classify {things}.
Respond in EXACTLY this format:
Classification: <X>
"""
PATTERNS = [r"ignore previous", r"system prompt"]
LIMIT = 5
RETRY_LIMIT = 2
Settings.embed_model = "bge-m3"
EVENT_LOG = []
CACHE = {}
CALLS = {"n": 0}
''',
# 3 usage tracker (same shape as the real one)
'''@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_calls: int = 0

    def add(self, usage):
        self.prompt_tokens += usage["p"]
        self.completion_tokens += usage["c"]
        self.total_calls += 1

    def report(self):
        print("tokens", self.prompt_tokens + self.completion_tokens)

usage_tracker = TokenUsage()
''',
# 4 client + tracked call
'''class FakeClient:
    def __init__(self, key):
        self.key = key
    def create(self, messages):
        time.sleep(float(os.getenv("FAKE_LLM_DELAY", "0.01")))
        return {"text": "ok", "usage": {"p": 10, "c": 5}}

client = FakeClient(GROQ_KEY)

def tracked_llm_call(messages, system=""):
    resp = client.create(messages)
    usage_tracker.add(resp["usage"])
    return resp["text"]
''',
# 5 state + decorator helper
'''class RAGState(TypedDict):
    question: str
    answer: Optional[str]
    classification: Optional[str]
    calls: Optional[int]

def logged(fn):
    def wrapper(*a, **k):
        EVENT_LOG.append(fn.__name__)
        return fn(*a, **k)
    wrapper.__wrapped__ = fn
    wrapper.__name__ = fn.__name__
    return wrapper
''',
# 6 nodes v1 (OLD)
'''def classify_node(state):
    CALLS["n"] += 1
    flagged = any(re.search(p, state["question"].lower()) for p in PATTERNS)
    tracked_llm_call([{"role": "user", "content": state["question"]}], system=ROUTER_PROMPT)
    return {"classification": "FLAGGED" if flagged else "SIMPLE"}

def generate_node(state):
    return {"answer": "v1 answer"}

def make_registry():
    return {"version": 1}
''',
# 7 a test cell that must NOT end up in the package
'''print(generate_node({}))
assert generate_node({})["answer"] == "v1 answer"
state_test = generate_node({"question": "x"})
for q in ["a", "b"]:
    print(q, classify_node({"question": q}))
''',
# 8 generate_node v2 (LATEST)
'''def generate_node(state):
    tracked_llm_call([{"role": "user", "content": state["question"]}])
    return {"answer": f"v2 answer 391 retries={RETRY_LIMIT} registry={registry['version']}"}
''',
# 9 decorated node + self-referential wrapping + forward-dependency
'''@logged
def retrieve_node(state):
    return {}

classify_node = logged(classify_node)
registry = make_registry()
''',
# 10 make_registry v2 defined AFTER registry was built (stale case, direct dependency)
'''def make_registry():
    return {"version": 2}
''',
# 11 build the graph
'''def build_graph(db_path=CHECKPOINT_DB):
    g = StateGraph(RAGState)
    g.add_node("classify_node", classify_node)
    g.add_node("retrieve_node", retrieve_node)
    g.add_node("generate_node", generate_node)
    g.set_entry_point("classify_node")
    g.add_edge("classify_node", "retrieve_node")
    g.add_edge("retrieve_node", "generate_node")
    g.add_edge("generate_node", END)
    return g.compile()

multihop_graph = build_graph()
''',
# 12 entry point (same shape as the real one: decorated, global tracker reassigned per call)
'''@logged
def run_with_entity_memory(question, session_id, user_id):
    global usage_tracker
    usage_tracker = TokenUsage()
    EVENT_LOG.append(("run", session_id))
    final = multihop_graph.invoke({"question": question, "answer": None, "classification": None, "calls": None})
    usage_tracker.report()
    final["calls"] = usage_tracker.total_calls
    return final
''',
# 13 helpers the server needs
'''class _Redis:
    def ping(self): return True
class _Mongo:
    def __init__(self, uri): self.uri = uri
    def command(self, c): return {"ok": 1}
class _Neo:
    def verify_connectivity(self): return None
redis_client, mongo_client, neo4j_driver = _Redis(), _Mongo(MONGO_URI), _Neo()

def check_rate_limit(user_id):
    n = CACHE.get(user_id, 0) + 1
    CACHE[user_id] = n
    return n <= LIMIT, n
''',
# 14 test cells, a failing cell and a syntax error
'''state1 = run_with_entity_memory("What was net income?", "s1", "u1")
print(state1["answer"])
assert "391" in state1["answer"]
''',
"x = 1\n1/0\n",
"def broken(:\n    pass\n",
# 17 later cells that deliberately change shared constants
'''PATTERNS.append("new pattern")
LIMIT += 1
''',
]


def stale_graph_cells():
    """The graph was built, then generate_node was improved but the graph was NOT rebuilt."""
    return clean_cells() + ['''def generate_node(state):
    tracked_llm_call([{"role": "user", "content": state["question"]}])
    return {"answer": "v3 answer 391 -- improved but graph never rebuilt"}
''']


def unresolved_cells():
    cells = clean_cells()
    cells[8] = '''exec("hidden_helper = lambda: 7")
def generate_node(state):
    tracked_llm_call([{"role": "user", "content": state["question"]}])
    return {"answer": f"v2 answer 391 helper={hidden_helper()}"}
'''
    return cells


def mismatch_cells():
    return clean_cells() + ['''def _tweak():
    global RETRY_LIMIT
    RETRY_LIMIT = 3
_tweak()
''']


def secret_cells():
    cells = clean_cells()
    cells[2] = cells[2].replace('GROQ_KEY = os.getenv("FAKE_GROQ_KEY", "")', 'GROQ_KEY = "gsk_FAKEFAKEFAKEFAKEFAKE12345678"')
    cells[2] = cells[2].replace('MONGO_URI = os.getenv("FAKE_MONGO_URI", "")', 'MONGO_URI = "mongodb+srv://rodney:Sup3rS3cret@cluster0.example.net/db"')
    return cells
