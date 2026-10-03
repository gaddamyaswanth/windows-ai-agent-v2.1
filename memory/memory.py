"""Backward-compatible memory interface."""
from .store import MemoryStore

class Memory:
    def __init__(self, path="memory/memory.db"):
        self.store=MemoryStore(path)
    def add_entry(self, goal, outcome):
        self.store.save("task", f"Goal: {goal}\nOutcome: {outcome}", importance=4, source="task_history")
    def recent(self, n=10):
        return self.store.recent(n)
