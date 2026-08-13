"""Session implementations must keep up with the Memory protocol.

Guards mh-incubator #58: a Session missing a Memory member used to crash
deep inside the agent loop on an edge path. Walks the protocol surface
so the submodule's test suite fails the moment a Session drifts.
"""

from minimal_harness.memory import Memory

from mh_gateway.database._session import SimpleSession


def _memory_surface() -> set[str]:
    return set(getattr(Memory, "__protocol_attrs__"))


def test_simple_session_implements_full_memory_surface() -> None:
    missing = sorted(a for a in _memory_surface() if not hasattr(SimpleSession, a))
    assert not missing, f"SimpleSession missing Memory members: {missing}"


def test_simple_session_passes_runtime_contract_check() -> None:
    from minimal_harness.memory import verify_memory_contract

    verify_memory_contract(SimpleSession())  # must not raise
