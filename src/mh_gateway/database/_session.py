from __future__ import annotations

from typing import Protocol, TypedDict
from uuid import uuid4

from minimal_harness.memory import ConversationMemory, Memory

from mh_gateway.database._ids import generate_bigint_id


class SessionSummary(TypedDict):
    session_id: str
    agent_name: str
    user_id: str
    scenario_id: str | None
    title: str | None
    created_at: str
    message_count: int
    status: str  # "running" | "idle", filled by the TUI layer
    display_name_locale: (
        str | None
    )  # JSON-encoded i18n dict, e.g. {"zh":"通用助手","en":"General Assistant"}


class Session(Memory, Protocol):
    """An identity-enriched Memory.

    Inherits the full :class:`Memory` protocol surface: any member
    minimal-harness adds to ``Memory`` is automatically required of
    every ``Session`` implementation. pyright and the contract tests in
    ``tests/test_session_contract.py`` catch drift at build time instead
    of an AttributeError deep inside the agent loop (mh-incubator #58).
    """

    @property
    def session_id(self) -> str: ...
    @property
    def display_name_locale(self) -> str | None: ...
    @property
    def user_id(self) -> str: ...
    @property
    def scenario_id(self) -> str | None: ...
    @property
    def memory(self) -> Memory: ...


class SimpleSession(ConversationMemory):
    """A basic Session implementation: a ConversationMemory plus session identity.

    Subclassing :class:`ConversationMemory` instead of hand-delegating
    means the entire Memory surface is inherited — a new Memory protocol
    member can never silently go missing from this class again. The only
    members overridden here are the identity properties.
    """

    def __init__(
        self,
        session_id: str = "",
        agent_name: str = "",
        user_id: str = "",
        scenario_id: str | None = None,
        display_name_locale: str | None = None,
    ) -> None:
        super().__init__()
        self.db_id: int = generate_bigint_id()
        self.session_id = session_id or f"sess_{uuid4().hex[:12]}"
        self._agent_name = agent_name
        self.user_id = user_id
        self.scenario_id = scenario_id
        self._title: str | None = None
        self.display_name_locale = display_name_locale
        self._created_at = ""

    @property
    def memory(self) -> Memory:
        return self

    @property
    def memory_id(self) -> str:
        return self.session_id

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def title(self) -> str | None:
        return self._title

    @title.setter
    def title(self, value: str | None) -> None:
        self._title = value

    @property
    def created_at(self) -> str:
        return self._created_at

    @created_at.setter
    def created_at(self, value: str) -> None:
        self._created_at = value
