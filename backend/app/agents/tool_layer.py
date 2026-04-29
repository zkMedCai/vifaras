"""Agent tool layer — async modernized (brief task 6.3.a).

Closes DQ-28: the legacy sync scaffold (intent_service / match_service / ...
all stub-raised) is replaced by an `AsyncToolHandler` that wires each
of the 9 agent tools to the real async service implementations built
across FASE 4 + 5 + 6.1/6.2.

Architecture:

  - **Tool definitions** (`AGENT_TOOLS`) keep the MCP-compatible JSON
    schema (PROJECT_BRIEF §2.7). The `step_up_signature` parameter is
    *not* in the schemas: server-side step-up flow handles it
    transparently — Claude doesn't carry signature material around.
  - **`ToolResult`** is a standardized return object. Four statuses:
      * `ok`             — action executed; `data` holds the tool's
                           result dict.
      * `error`          — generic failure (mandate denied, exec error).
      * `step_up_required` — action paused; `data.step_up_id` lets the
                           caller (orchestrator) re-attempt later.
      * `limit_exceeded` — distinguished from `error` so the prompt
                           can react with "I've hit my cap, stop".
  - **`AsyncToolHandler`** runs each tool through:
      1. `verifier.authorize_async(...)` — sync verifier wrapped via
         `asyncio.to_thread` (DQ-34). Raises `MandateError`,
         `LimitExceeded`, or `StepUpRequired`.
      2. The tool method (e.g. `_create_intent`) — pure async, delegates
         to the appropriate service.
      3. `verifier.record_usage_async(...)` — same wrapper pattern.

  Notice the verifier owns its own sync `Session`; the handler owns the
  `AsyncSession`. They write to the same DB but on different connections.
  Audit-log writes by the verifier commit on the sync side; business
  writes commit on the async side. This is fine for V0 — both writes
  are independent at audit-row granularity.

Step-up flow: when `authorize_async` raises `StepUpRequired`, we look
up the agent's active mandate, persist a `StepUpRequest` row via
`step_up_service.create_pending_request_async` (which also fires the
`STEP_UP_REQUIRED` notification — closing the V0 wire-on-modernization
note from 6.1), and return `ToolResult(status="step_up_required", ...)`.
The orchestrator (6.3.b) will see this and stash the action; the user
signs via the existing `/api/step-up/{id}/sign` endpoint; the next tick
the orchestrator notices `approved_step_ups` in the inbox and re-runs
the original action.

Test pattern: tests inject a `FakeMandateVerifier` so the suite
doesn't need a sync DB session bound to the same transaction as the
async one (which is impossible under savepoint mode). The real verifier
is exercised by its own `test_mandate_verifier.py` (100% coverage).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import Agent, Mandate, User
from app.services import (
    agent_state_service,
    inbox_service,
    intent_service,
    match_service,
    negotiation_service,
    step_up_service,
    user_question_service,
)
from app.services.mandate_verifier import (
    LimitExceeded,
    MandateError,
    StepUpRequired,
)


# ---------------------------------------------------------------------------
# Tool definitions (MCP-compatible JSON schema for Claude tool_use)
# ---------------------------------------------------------------------------


AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_intent",
        "description": (
            "Crea un nuovo intent BUY o SELL nel marketplace. Specifica "
            "reservation_price (limite massimo se buy, minimo se sell) e "
            "ideal_price (target ottimale)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "title": {"type": "string", "maxLength": 200},
                "description": {"type": "string"},
                "category": {"type": "string"},
                "reservation_price_eur": {"type": "number", "minimum": 0},
                "ideal_price_eur": {"type": "number", "minimum": 0},
                "duration_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                },
                "hard_constraints": {"type": "object"},
                "soft_preferences": {"type": "object"},
            },
            "required": [
                "side",
                "title",
                "category",
                "reservation_price_eur",
                "ideal_price_eur",
            ],
        },
    },
    {
        "name": "search_matches",
        "description": (
            "Cerca match potenziali per uno dei tuoi intent attivi. Ritorna "
            "intent della parte opposta con score di compatibilità."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["intent_id"],
        },
    },
    {
        "name": "send_offer",
        "description": (
            "Invia un'offerta a un match. Crea o continua una negoziazione."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "match_id": {"type": "string"},
                "price_cents": {"type": "integer", "minimum": 1},
                "message": {"type": "string", "maxLength": 500},
            },
            "required": ["match_id", "price_cents"],
        },
    },
    {
        "name": "send_counter_offer",
        "description": (
            "Manda contro-offerta in una negoziazione attiva. Il sistema "
            "blocca al 6° round (best-and-final)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "negotiation_id": {"type": "string"},
                "price_cents": {"type": "integer", "minimum": 1},
                "message": {"type": "string", "maxLength": 500},
            },
            "required": ["negotiation_id", "price_cents"],
        },
    },
    {
        "name": "accept_offer",
        "description": (
            "Accetta l'ultima offerta della controparte. Crea un Deal "
            "pending. Sopra la soglia step-up del mandate, l'azione viene "
            "messa in coda per firma utente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "negotiation_id": {"type": "string"},
            },
            "required": ["negotiation_id"],
        },
    },
    {
        "name": "reject_offer",
        "description": "Rifiuta una negoziazione, chiudendola.",
        "input_schema": {
            "type": "object",
            "properties": {
                "negotiation_id": {"type": "string"},
                "reason": {"type": "string", "maxLength": 500},
            },
            "required": ["negotiation_id"],
        },
    },
    {
        "name": "check_state",
        "description": (
            "Ritorna lo stato corrente: mandato, intent attivi, "
            "negoziazioni, deal pending, inbox. USA QUESTO ALL'INIZIO DI "
            "OGNI TURNO per non fidarti della tua memoria."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_inbox",
        "description": (
            "Eventi dall'ultimo tick: nuove offerte, contro-offerte, deal "
            "in attesa di firma, step-up risolti."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "ask_user",
        "description": (
            "USO RARO. Chiedi conferma o nuove istruzioni all'utente. Solo "
            "se davvero non puoi decidere (es. floor irraggiungibile, "
            "scope unclear). Costa attenzione dell'utente, non abusare."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "maxLength": 300},
                "context": {"type": "object"},
            },
            "required": ["question"],
        },
    },
]


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Standardized tool return shape consumed by the orchestrator and
    serialized into Claude's tool_result content blocks.

    Statuses:
      - 'ok':                 action succeeded; `data` holds the result.
      - 'error':              generic failure; `error` describes it.
      - 'step_up_required':   user signature needed; `data.step_up_id`.
      - 'limit_exceeded':     mandate cap hit; tells the model to stop.
    """

    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.data:
            out["data"] = self.data
        if self.error is not None:
            out["error"] = self.error
        if self.error_code is not None:
            out["error_code"] = self.error_code
        return out


# ---------------------------------------------------------------------------
# Verifier protocol — narrow interface so tests can inject a fake
# ---------------------------------------------------------------------------


class VerifierProtocol(Protocol):
    """The two methods `AsyncToolHandler` actually awaits.

    Real implementation: `MandateVerifier.{authorize_async,
    record_usage_async, log_failed_async}`. Tests inject a fake that
    implements this same shape — see `tests/test_tool_layer.py`.
    """

    async def authorize_async(
        self, agent_id: str, action: str, params: dict
    ) -> Any:  # returns Mandate-like
        ...

    async def record_usage_async(
        self,
        mandate: Any,
        action: str,
        params: dict,
        success: bool,
        result: dict | None = None,
        error_code: str | None = None,
    ) -> None:
        ...

    async def log_failed_async(
        self, agent_id: str, action: str, error: MandateError
    ) -> None:
        ...


# ---------------------------------------------------------------------------
# AsyncToolHandler
# ---------------------------------------------------------------------------


class AsyncToolHandler:
    """One per agent tick. Owns an `AsyncSession` + a verifier instance.

    The orchestrator (6.3.b) constructs this once per tick, dispatches
    each of Claude's `tool_use` blocks through `handle()`, collects the
    `ToolResult` outputs, feeds them back as `tool_result` content.
    """

    def __init__(
        self,
        db: AsyncSession,
        agent_id: str,
        *,
        verifier: VerifierProtocol,
    ) -> None:
        self.db = db
        self.agent_id = agent_id
        self.verifier = verifier
        self._user_id_cache: str | None = None

    # ------------------------------------------------------------------
    # Public dispatch
    # ------------------------------------------------------------------

    async def handle(
        self, tool_name: str, params: dict[str, Any]
    ) -> ToolResult:
        method = self._resolve_method(tool_name)
        if method is None:
            return ToolResult(
                status="error",
                error=f"unknown_tool:{tool_name}",
                error_code="unknown_tool",
            )

        # 1. Authorize via verifier (sync logic via asyncio.to_thread).
        try:
            mandate = await self.verifier.authorize_async(
                self.agent_id, tool_name, params
            )
        except StepUpRequired as step:
            step_up_id = await self._queue_step_up(step)
            return ToolResult(
                status="step_up_required",
                data={
                    "step_up_id": step_up_id,
                    "reason": step.reason,
                    "action": step.action,
                    "message": (
                        "Step-up dell'utente richiesto. Notifica push inviata. "
                        "Riprova al prossimo tick dopo l'approvazione."
                    ),
                },
            )
        except LimitExceeded as exc:
            await self.verifier.log_failed_async(self.agent_id, tool_name, exc)
            return ToolResult(
                status="limit_exceeded",
                error=str(exc),
                error_code=getattr(exc, "code", "limit_exceeded"),
            )
        except MandateError as exc:
            await self.verifier.log_failed_async(self.agent_id, tool_name, exc)
            return ToolResult(
                status="error",
                error=str(exc),
                error_code=getattr(exc, "code", "mandate_error"),
            )

        # 2. Execute the tool.
        try:
            data = await method(params)
        except Exception as exc:
            await self.verifier.record_usage_async(
                mandate,
                tool_name,
                params,
                success=False,
                result={"error": str(exc)},
                error_code="execution_error",
            )
            return ToolResult(
                status="error",
                error=str(exc),
                error_code="execution_error",
            )

        # 3. Record usage on success.
        await self.verifier.record_usage_async(
            mandate,
            tool_name,
            params,
            success=True,
            result=_truncate_for_audit(data),
        )
        return ToolResult(status="ok", data=data)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _resolve_method(
        self, tool_name: str
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None:
        return {
            "create_intent": self._create_intent,
            "search_matches": self._search_matches,
            "send_offer": self._send_offer,
            "send_counter_offer": self._send_counter_offer,
            "accept_offer": self._accept_offer,
            "reject_offer": self._reject_offer,
            "check_state": self._check_state,
            "read_inbox": self._read_inbox,
            "ask_user": self._ask_user,
        }.get(tool_name)

    # ------------------------------------------------------------------
    # User-id cache (one DB hit per tick)
    # ------------------------------------------------------------------

    async def _get_user_id(self) -> str:
        if self._user_id_cache is None:
            agent = await self.db.get(Agent, self.agent_id)
            if agent is None:
                raise RuntimeError(f"agent {self.agent_id!r} not found")
            self._user_id_cache = agent.user_id
        return self._user_id_cache

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _create_intent(self, params: dict) -> dict:
        user_id = await self._get_user_id()
        input_obj = intent_service.CreateIntentInput(**params)
        intent = await intent_service.create_intent(
            self.db, user_id=user_id, input=input_obj
        )
        return {
            "intent_id": intent.id,
            "status": intent.status,
            "expires_at": intent.expires_at.isoformat() + "Z",
        }

    async def _search_matches(self, params: dict) -> dict:
        intent_id = params["intent_id"]
        limit = int(params.get("limit", match_service.DEFAULT_MATCH_LIMIT))
        matches = await match_service.find_matches_for_intent(
            self.db, intent_id=intent_id, limit=limit
        )
        return {
            "match_count": len(matches),
            "matches": [
                {
                    "match_id": m.id,
                    "buy_intent_id": m.buy_intent_id,
                    "sell_intent_id": m.sell_intent_id,
                    "similarity_score": float(m.similarity_score or 0),
                    "price_proximity_score": float(
                        m.price_proximity_score or 0
                    ),
                    "combined_score": float(m.combined_score or 0),
                    "status": m.status,
                }
                for m in matches
            ],
        }

    async def _send_offer(self, params: dict) -> dict:
        user_id = await self._get_user_id()
        result = await negotiation_service.start_or_continue(
            self.db,
            user_id=user_id,
            agent_id=self.agent_id,
            match_id=params["match_id"],
            price_cents=int(params["price_cents"]),
            message=params.get("message") or "",
        )
        return {
            "negotiation_id": result.negotiation_id,
            "rounds_used": result.rounds_used,
            "max_rounds": result.max_rounds,
            "is_final_round": result.is_final_round,
            "created_new": result.created_new,
        }

    async def _send_counter_offer(self, params: dict) -> dict:
        """`negotiation_service.start_or_continue` takes match_id, not
        negotiation_id — resolve via the negotiation row."""
        user_id = await self._get_user_id()
        from app.models.schema import Negotiation

        nego = await self.db.get(Negotiation, params["negotiation_id"])
        if nego is None:
            raise negotiation_service.NegotiationNotFound(
                f"negotiation {params['negotiation_id']!r} not found"
            )
        result = await negotiation_service.start_or_continue(
            self.db,
            user_id=user_id,
            agent_id=self.agent_id,
            match_id=nego.match_id,
            price_cents=int(params["price_cents"]),
            message=params.get("message") or "",
        )
        return {
            "negotiation_id": result.negotiation_id,
            "rounds_used": result.rounds_used,
            "max_rounds": result.max_rounds,
            "is_final_round": result.is_final_round,
        }

    async def _accept_offer(self, params: dict) -> dict:
        user_id = await self._get_user_id()
        result = await negotiation_service.accept_offer(
            self.db,
            user_id=user_id,
            agent_id=self.agent_id,
            negotiation_id=params["negotiation_id"],
        )
        return {
            "deal_id": result.deal_id,
            "agreed_price_cents": result.agreed_price_cents,
            "next_step": result.next_step,
        }

    async def _reject_offer(self, params: dict) -> dict:
        user_id = await self._get_user_id()
        result = await negotiation_service.reject_offer(
            self.db,
            user_id=user_id,
            agent_id=self.agent_id,
            negotiation_id=params["negotiation_id"],
            reason=params.get("reason"),
        )
        return {
            "negotiation_id": result.negotiation_id,
            "reason": result.reason,
            "status": "rejected",
        }

    async def _check_state(self, params: dict) -> dict:
        state = await agent_state_service.get_full_state(
            self.db, agent_id=self.agent_id
        )
        return state.model_dump(mode="json")

    async def _read_inbox(self, params: dict) -> dict:
        user_id = await self._get_user_id()
        agent = await self.db.get(Agent, self.agent_id)
        since = agent.last_tick_at if agent is not None else None
        inbox = await inbox_service.get_inbox_for_agent(
            self.db,
            agent_id=self.agent_id,
            user_id=user_id,
            since=since,
        )
        return inbox.model_dump(mode="json")

    async def _ask_user(self, params: dict) -> dict:
        user_id = await self._get_user_id()
        question = params["question"]
        context = params.get("context") or {}
        if isinstance(context, str):
            # Tolerant: Claude sometimes passes a string when schema asks
            # for an object. Wrap defensively.
            context = {"text": context}
        row = await user_question_service.create_question(
            self.db,
            agent_id=self.agent_id,
            user_id=user_id,
            question=question,
            context=context,
        )
        if row is None:
            return {
                "status": "queued",
                "message": "Question failed to persist; will not block tick.",
            }
        return {
            "status": "queued",
            "question_id": row.id,
            "message": (
                "Domanda inviata all'utente. Riprova al prossimo tick "
                "per leggere la risposta via read_inbox."
            ),
        }

    # ------------------------------------------------------------------
    # Step-up persistence on StepUpRequired
    # ------------------------------------------------------------------

    async def _queue_step_up(self, step: StepUpRequired) -> str | None:
        """Persist a `StepUpRequest` row + fire STEP_UP_REQUIRED notification.

        Returns the step_up_id (or `None` if mandate/user can't be located,
        which would only happen if the verifier raised inconsistently).
        """
        agent = await self.db.get(Agent, self.agent_id)
        if agent is None:
            return None
        mandate = await self.db.scalar(
            select(Mandate)
            .where(Mandate.agent_id == self.agent_id)
            .where(Mandate.revoked_at.is_(None))
            .order_by(Mandate.issued_at.desc())
        )
        if mandate is None:
            return None
        user = await self.db.get(User, agent.user_id)
        if user is None:
            return None

        return await step_up_service.create_pending_request_async(
            self.db,
            agent_id=self.agent_id,
            mandate_id=mandate.id,
            user_id=user.id,
            nullifier_hash=user.nullifier_hash or "",
            action=step.action,
            action_params=step.params,
            reason=step.reason,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_AUDIT_RESULT_MAX_BYTES = 4096


def _truncate_for_audit(data: dict[str, Any]) -> dict[str, Any]:
    """Audit's `result` JSONB shouldn't store huge state dumps (check_state
    can return ~10 KB). Truncate to 4 KB; keep top-level keys for grep-ability.
    """
    try:
        encoded = json.dumps(data)
    except (TypeError, ValueError):
        return {"_truncated": True, "_keys": sorted(list(data.keys()))}
    if len(encoded) <= _AUDIT_RESULT_MAX_BYTES:
        return data
    return {
        "_truncated": True,
        "_size_bytes": len(encoded),
        "_keys": sorted(list(data.keys())),
    }


# ---------------------------------------------------------------------------
# Legacy sync wrapper preserved for back-compat (will be deleted in 7.x
# alongside DQ-34 cleanup of the verifier)
# ---------------------------------------------------------------------------


class ToolHandler:  # pragma: no cover — legacy stub, no V0 caller
    """Deprecated sync stub.

    The orchestrator (6.3.b) uses `AsyncToolHandler` exclusively. This
    class survives only so legacy imports don't break; calling its
    methods raises immediately to surface the misuse.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "ToolHandler (sync) is deprecated. Use AsyncToolHandler. "
            "See DQ-28 (resolved) for context."
        )
