"""
Tool Layer dell'agente.

Questi sono i tool che Claude può invocare per agire nel marketplace.
Ogni tool passa attraverso MandateVerifier prima di eseguire.

Pattern: tool_handler() funzioni che:
1. Chiamano verifier.authorize() — può sollevare MandateError o StepUpRequired
2. Eseguono l'azione tramite il service appropriato
3. Chiamano verifier.record_usage() per logging e contatori

I tool ritornano sempre dict serializzabili — è quello che Claude legge.
"""
from datetime import datetime
from sqlalchemy.orm import Session
from typing import Any

from app.services.mandate_verifier import (
    MandateVerifier, MandateError, StepUpRequired
)
from app.services import (
    intent_service, match_service, negotiation_service, deal_service
)


# ============================================================================
# Tool definitions per Claude (Anthropic tool use format)
# ============================================================================

AGENT_TOOLS = [
    {
        "name": "create_intent",
        "description": (
            "Crea un nuovo intent BUY o SELL nel marketplace. "
            "Usa questo quando l'utente vuole comprare o vendere qualcosa. "
            "Specifica reservation_price (limite massimo se buy, minimo se sell) "
            "e ideal_price (target ottimale)."
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
                "duration_days": {"type": "integer", "minimum": 1, "maximum": 30},
                "hard_constraints": {"type": "object"},
            },
            "required": ["side", "title", "category", "reservation_price_eur", 
                         "ideal_price_eur", "duration_days"],
        },
    },
    {
        "name": "search_matches",
        "description": (
            "Cerca match potenziali per uno dei tuoi intent attivi. "
            "Ritorna intent della parte opposta (sell se tu hai buy e viceversa) "
            "ordinati per similarity semantica e overlap di prezzo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["intent_id"],
        },
    },
    {
        "name": "send_offer",
        "description": (
            "Invia un'offerta a un match potenziale. "
            "Avvia una negoziazione se non già esistente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "match_id": {"type": "string"},
                "price_cents": {"type": "integer", "minimum": 0},
                "message": {"type": "string", "maxLength": 500},
                "step_up_signature": {
                    "type": "object",
                    "description": "Solo se richiesto da step-up. WebAuthn assertion.",
                },
            },
            "required": ["match_id", "price_cents"],
        },
    },
    {
        "name": "send_counter_offer",
        "description": "Manda contro-offerta in una negoziazione attiva.",
        "input_schema": {
            "type": "object",
            "properties": {
                "negotiation_id": {"type": "string"},
                "price_cents": {"type": "integer", "minimum": 0},
                "message": {"type": "string", "maxLength": 500},
                "step_up_signature": {"type": "object"},
            },
            "required": ["negotiation_id", "price_cents"],
        },
    },
    {
        "name": "accept_offer",
        "description": (
            "Accetta l'ultima offerta ricevuta. Crea un deal pending. "
            "RICHIEDE step-up signature sopra €100 (default mandate)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "negotiation_id": {"type": "string"},
                "step_up_signature": {"type": "object"},
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
                "reason": {"type": "string"},
            },
            "required": ["negotiation_id"],
        },
    },
    {
        "name": "check_state",
        "description": (
            "Ritorna lo stato corrente: budget rimanente, intent attivi, "
            "negoziazioni in corso, deal recenti. "
            "USA QUESTO ALL'INIZIO DI OGNI TURNO per non fidarti della tua memoria."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_inbox",
        "description": (
            "Leggi le offerte ricevute, contro-offerte pending, "
            "deal in attesa di conferma."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "ask_user",
        "description": (
            "USO RARO. Chiedi conferma o nuove istruzioni all'utente "
            "tramite notifica push. Usa solo se davvero non puoi decidere "
            "(es. floor irraggiungibile, scope unclear). "
            "Costa attenzione dell'utente, non abusare."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "maxLength": 300},
                "context": {"type": "string"},
            },
            "required": ["question"],
        },
    },
]


# ============================================================================
# Tool handlers
# ============================================================================

class ToolHandler:
    """
    Esegue i tool call di Claude, passando per il MandateVerifier.
    Una istanza per agente per request, NON condivisa.
    """
    
    def __init__(self, db: Session, agent_id: str):
        self.db = db
        self.agent_id = agent_id
        self.verifier = MandateVerifier(db)
    
    def execute(self, tool_name: str, tool_input: dict) -> dict:
        """
        Dispatch del tool call.
        Ritorna sempre un dict — è quello che Claude leggerà.
        """
        # 1. Authorize
        try:
            mandate = self.verifier.authorize(
                self.agent_id, tool_name, tool_input
            )
        except StepUpRequired as step:
            # Mette in coda la richiesta di step-up, ritorna a Claude
            # un risultato che spiega che serve attendere
            step_up_id = self._queue_step_up(step)
            return {
                "status": "step_up_required",
                "step_up_id": step_up_id,
                "message": (
                    "Step-up dell'utente richiesto. "
                    "Notifica push inviata. "
                    "Attendi conferma prima di riprovare."
                ),
                "reason": step.reason,
            }
        except MandateError as e:
            self.verifier.log_failed(self.agent_id, tool_name, e)
            return {
                "status": "error",
                "error_code": e.code,
                "message": str(e),
            }
        
        # 2. Execute
        try:
            handler = self._get_handler(tool_name)
            result = handler(tool_input)
            self.verifier.record_usage(
                mandate, tool_name, tool_input,
                success=True, result=result
            )
            return {"status": "ok", **result}
        except Exception as e:
            self.verifier.record_usage(
                mandate, tool_name, tool_input,
                success=False, error_code="execution_error",
                result={"error": str(e)}
            )
            return {"status": "error", "message": str(e)}
    
    # ------------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------------
    
    def _get_handler(self, tool_name: str):
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
        }[tool_name]
    
    # ------------------------------------------------------------------------
    # Handlers concreti (delegano ai service)
    # ------------------------------------------------------------------------
    
    def _create_intent(self, params: dict) -> dict:
        # 4.1 brief: tool_layer scaffold is sync and uses `Session.query()`;
        # `intent_service.create_intent` is async and takes `user_id +
        # CreateIntentInput`. Wiring the two together cleanly requires
        # modernizing tool_layer to async (FASE 5/6 — orchestrator + agent
        # runtime). For 4.1, intents are exclusively created via the FastAPI
        # endpoints in `api/intents.py`. See DESIGN_QUESTIONS DQ-28.
        raise NotImplementedError(
            "tool_layer._create_intent is deferred to FASE 5/6 "
            "(see DQ-28). Use POST /api/intents directly in V0."
        )
    
    def _search_matches(self, params: dict) -> dict:
        matches = match_service.find_matches(
            self.db, params["intent_id"], limit=params.get("limit", 10)
        )
        return {
            "match_count": len(matches),
            "matches": [
                {
                    "match_id": m.id,
                    "similarity": float(m.similarity_score),
                    "price_overlap": m.price_overlap,
                    "counterparty_intent_summary": match_service.summarize_for_agent(m),
                }
                for m in matches
            ],
        }
    
    def _send_offer(self, params: dict) -> dict:
        nego = negotiation_service.start_or_continue(
            self.db, agent_id=self.agent_id, **params
        )
        return {"negotiation_id": nego.id, "round": nego.rounds_used}
    
    def _send_counter_offer(self, params: dict) -> dict:
        nego = negotiation_service.add_counter_offer(
            self.db, agent_id=self.agent_id, **params
        )
        # EC6: hard cap su round
        if nego.rounds_used >= nego.max_rounds:
            return {
                "negotiation_id": nego.id,
                "status": "capped",
                "message": "Hard cap reached. Best-and-final required."
            }
        return {"negotiation_id": nego.id, "round": nego.rounds_used}
    
    def _accept_offer(self, params: dict) -> dict:
        deal = deal_service.create_pending_deal(
            self.db, agent_id=self.agent_id, **params
        )
        return {
            "deal_id": deal.id,
            "status": deal.status,
            "message": "Deal pending. Awaiting counterparty step-up.",
        }
    
    def _reject_offer(self, params: dict) -> dict:
        nego = negotiation_service.reject(
            self.db, agent_id=self.agent_id, **params
        )
        return {"negotiation_id": nego.id, "status": "rejected"}
    
    def _check_state(self, params: dict) -> dict:
        # Single source of truth: l'agente ricarica state ogni turno
        from app.services import agent_state_service
        return agent_state_service.get_full_state(self.db, self.agent_id)
    
    def _read_inbox(self, params: dict) -> dict:
        from app.services import inbox_service
        return inbox_service.get_inbox(self.db, self.agent_id)
    
    def _ask_user(self, params: dict) -> dict:
        from app.services import notification_service
        notification_service.push_question(
            self.db, agent_id=self.agent_id,
            question=params["question"],
            context=params.get("context", ""),
        )
        return {
            "status": "queued",
            "message": "Question pushed to user. Wait for response in next tick.",
        }
    
    # ------------------------------------------------------------------------
    # Step-up handling
    # ------------------------------------------------------------------------

    def _queue_step_up(self, step: StepUpRequired):
        """Persist a StepUpRequest row + push notification.

        Returns the step_up_id so `execute()` can surface it to Claude.
        Looks up the agent's active mandate (sync, scaffold-style) to
        bind user_id + mandate_id on the new row. Robust to missing
        mandate (returns None) — the verifier wouldn't have raised
        StepUpRequired without an active mandate, but we don't crash if
        the upstream contract is broken.
        """
        from app.models.schema import Mandate, User
        from app.services import notification_service, step_up_service

        mandate = (
            self.db.query(Mandate)
            .filter(Mandate.agent_id == self.agent_id)
            .filter(Mandate.revoked_at.is_(None))
            .order_by(Mandate.issued_at.desc())
            .first()
        )
        if mandate is None:
            return None
        user = self.db.get(User, mandate.user_id)
        if user is None:
            return None

        step_up_id = step_up_service.create_pending_request_sync(
            self.db,
            agent_id=self.agent_id,
            mandate_id=mandate.id,
            user_id=user.id,
            nullifier_hash=user.nullifier_hash or "",
            action=step.action,
            action_params=step.params,
            reason=step.reason,
        )

        notification_service.push_step_up_request(
            self.db,
            agent_id=self.agent_id,
            action=step.action,
            params=step.params,
            reason=step.reason,
            step_up_id=step_up_id,
        )
        return step_up_id
