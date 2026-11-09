"""Spawn-safe worker for the multiprocess cold-init test.

Lives in its own top-level module: with pytest's importlib import mode, a spawned child cannot
re-import a worker defined inside a test module. conftest.py puts this directory on sys.path.
"""
import multiprocessing

from cmi.project_memory import ProjectMemory


def cold_init_worker(path, barrier, results):
    try:
        barrier.wait(timeout=30)
        memory = ProjectMemory(path)
        memory.publish("p", str(multiprocessing.current_process().pid), "cold marker", source="test")
        results.put(None)
    except Exception as error:
        results.put(repr(error))
