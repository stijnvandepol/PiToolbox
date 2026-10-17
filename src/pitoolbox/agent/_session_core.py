"""Kern van `AgentSession`: alle toestand, meldingen voor het model en beurtregels in de audit.

`AgentSession` (`session`) is één object, per verantwoordelijkheid over modules verdeeld:
`_SessionCore` (hier) -> `_HumanDecisions` (`_decisions`, de uitkomsten van bevestigingen door
een mens en de noodstop) -> `AgentSession` (`session`, de loop en de geschiedenis). Elk deel
gebruikt alleen wat eronder ligt. De ontwerpkeuzes staan in de module-docstring van `session`
en gelden voor alle delen.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pitoolbox.agent._confirmations import OpenConfirmations
from pitoolbox.agent._tool_calls import ToolCallExecutor, model_tool_defs
from pitoolbox.agent.prompt import NOTE_PREFIX, SYSTEM_PROMPT
from pitoolbox.agent.types import AgentLimits, PendingConfirmation

if TYPE_CHECKING:
    from pitoolbox.core.tools.runner import ToolRunner
    from pitoolbox.llm.providers import LLMProvider
    from pitoolbox.llm.types import Message, ToolDef

# Dezelfde loggernaam als vóór het opsplitsen: de beurtregels komen nog van `session`.
log = logging.getLogger("pitoolbox.agent.session")


def note(text: str) -> str:
    return f"{NOTE_PREFIX} {text}"


class _SessionCore:
    """Toestand en basis van `AgentSession`; zie de module-docstring."""

    def __init__(
        self,
        provider: LLMProvider,
        runner: ToolRunner,
        *,
        frontend: str,
        limits: AgentLimits | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.runner = runner
        self.frontend = frontend
        self.limits = limits or AgentLimits()
        self.system_prompt = system_prompt
        self._clock = clock
        # Least privilege: tools buiten PITOOLBOX_TOOL_ALLOWLIST ziet het model niet eens (de
        # runner zou ze toch weigeren).
        self._tools = model_tool_defs(runner.allowed_specs())
        self._history: list[Message] = []
        self._notes: list[str] = []
        self._session_id = secrets.token_hex(4)
        self._turns = 0
        self._confirmations = OpenConfirmations()
        self._calls = ToolCallExecutor(runner, frontend, self.limits, clock, self._confirmations)
        self._unsubscribe: Callable[[], None] | None = runner.subscribe_resolutions(
            self._confirmations.on_resolved
        )

    def close(self) -> None:
        """Meldt de luisteraar voor uitkomsten af (bij het weggooien van de sessie)."""
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    @property
    def history(self) -> tuple[Message, ...]:
        return tuple(self._history)

    @property
    def tools(self) -> tuple[ToolDef, ...]:
        return self._tools

    @property
    def open_confirmations(self) -> tuple[PendingConfirmation, ...]:
        return self._confirmations.pending

    # --- audit ----------------------------------------------------------------------------

    def _audit_event(self, event: str, **fields: Any) -> None:
        """Beurtregel in de audit-log; nooit berichtinhoud of sleutels."""
        try:
            self.runner.audit.write(
                event=event,
                frontend=self.frontend,
                session=self._session_id,
                turn=self._turns,
                **fields,
            )
        except OSError:
            log.exception("audit-regel %s kon niet worden geschreven", event)
        log.info("%s turn=%d %s", event, self._turns, fields)
