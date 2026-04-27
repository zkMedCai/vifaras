"""
Agent Orchestrator — il loop che fa girare Claude con i tool del marketplace.

Pattern:
  - Per ogni "tick" del marketplace (ogni 30-60 secondi)
  - Lo scheduler pesca un agente che ha lavoro da fare
  - L'orchestrator carica state minimo, dà a Claude N turni di tool use
  - Persiste tutto, passa al prossimo agente

Single source of truth: il DB. L'agente NON si fida della propria memoria.
All'inizio di ogni invocazione, ricarica state via tool.
"""
import os
import json
from datetime import datetime
from typing import Optional
from anthropic import Anthropic
from sqlalchemy.orm import Session

from app.agents.tool_layer import ToolHandler, AGENT_TOOLS
from app.models.schema import Agent, Mandate, Intent


# Hard cap di turni di tool use per singola invocazione di un agente.
# Previene loops e cost explosions.
MAX_TURNS_PER_TICK = 10

# Modello scelto: Sonnet è il sweet spot prezzo/qualità per negoziazione.
# Per V0 hardcoded. In V1 può essere parametro del mandate (premium tier = Opus).
AGENT_MODEL = "claude-sonnet-4-5"


class AgentOrchestrator:
    """Esegue un singolo tick di un singolo agente."""
    
    def __init__(self, db: Session, anthropic_client: Optional[Anthropic] = None):
        self.db = db
        self.client = anthropic_client or Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )
    
    def run_tick(self, agent_id: str) -> dict:
        """
        Esegue un tick per un agente.
        Ritorna summary dell'attività per logging/monitoring.
        """
        agent = self.db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent or agent.status != "active":
            return {"skipped": True, "reason": "agent not active"}
        
        # Costruisci system prompt dal mandate
        system_prompt = self._build_system_prompt(agent_id)
        
        # Inizializza tool handler
        handler = ToolHandler(self.db, agent_id)
        
        # Conversation history per questa invocazione (ephemeral)
        # IMPORTANTE: NON persistiamo questo. Ogni tick è isolato.
        # State persistente vive nel DB. Memoria conversazionale è fresca ogni tick.
        messages = [
            {
                "role": "user",
                "content": (
                    "È il tuo turno. Esegui il tuo mandate per l'utente. "
                    "Inizia chiamando check_state e read_inbox per capire la situazione. "
                    "Poi decidi le azioni più utili. "
                    f"Hai al massimo {MAX_TURNS_PER_TICK} azioni in questo turno."
                )
            }
        ]
        
        turns_used = 0
        actions_taken = []
        
        while turns_used < MAX_TURNS_PER_TICK:
            turns_used += 1
            
            # Chiama Claude
            response = self.client.messages.create(
                model=AGENT_MODEL,
                max_tokens=2048,
                system=system_prompt,
                tools=AGENT_TOOLS,
                messages=messages,
            )
            
            # Append assistant response to history
            messages.append({
                "role": "assistant",
                "content": response.content
            })
            
            # Se Claude non chiede tool use, ha finito
            if response.stop_reason != "tool_use":
                break
            
            # Esegui tutti i tool use blocks
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                
                tool_name = block.name
                tool_input = block.input
                
                result = handler.execute(tool_name, tool_input)
                actions_taken.append({
                    "tool": tool_name,
                    "input": tool_input,
                    "result_status": result.get("status"),
                })
                
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
            
            # Append tool results al messages per il prossimo round
            messages.append({"role": "user", "content": tool_results})
        
        return {
            "agent_id": agent_id,
            "turns_used": turns_used,
            "actions_count": len(actions_taken),
            "actions": actions_taken,
            "completed_at": datetime.utcnow().isoformat(),
        }
    
    # ------------------------------------------------------------------------
    # System prompt construction
    # ------------------------------------------------------------------------
    
    def _build_system_prompt(self, agent_id: str) -> str:
        """
        Costruisce la system prompt dell'agente partendo dal mandate.
        È quello che dà identità e obiettivi all'agente.
        """
        mandate = (
            self.db.query(Mandate)
            .filter(Mandate.agent_id == agent_id)
            .filter(Mandate.revoked_at.is_(None))
            .order_by(Mandate.issued_at.desc())
            .first()
        )
        
        if not mandate:
            return "No active mandate. Refuse all actions."
        
        agent = self.db.query(Agent).filter(Agent.id == agent_id).first()
        
        # Estrai parametri rilevanti del mandate
        scope = mandate.scope
        limits = mandate.limits
        constraints = mandate.constraints
        
        prompt = f"""Sei un agente AI che agisce nel marketplace per conto di un utente verificato.

# La tua identità
- Agent ID: {agent_id}
- Nome dato dall'utente: {agent.name or 'Senza nome'}
- Mandate ID: {mandate.id}
- Mandate scade: {mandate.expires_at.isoformat()}

# Cosa puoi fare
Azioni permesse: {', '.join(scope.get('allowed_actions', []))}
Azioni proibite: {', '.join(scope.get('forbidden_actions', []))}

# Limiti operativi
- Prezzo massimo per singolo deal: €{limits.get('max_price_per_deal_eur', 'N/A')}
- Volume giornaliero: €{limits.get('max_total_volume_eur_per_day', 'N/A')}
- Volume totale del mandate: €{limits.get('max_total_volume_eur_per_mandate', 'N/A')}
- Massimo intent attivi simultanei: {limits.get('max_active_intents', 'N/A')}
- Massimo deal al giorno: {limits.get('max_deals_per_day', 'N/A')}

Stato corrente utilizzo:
- Speso oggi: €{mandate.spent_today_eur or 0}
- Speso totale (vita mandate): €{mandate.spent_total_eur or 0}
- Deal chiusi: {mandate.deals_count or 0}

# Vincoli
- Ambito geografico: {constraints.get('geo_scope', ['*'])}
- Categorie permesse: {constraints.get('categories_allowed', ['*'])}
- Categorie proibite: {constraints.get('categories_forbidden', [])}

# Principi operativi (CRITICI)

1. **Single source of truth è il DB, non la tua memoria.**
   All'inizio di ogni turno chiama check_state e read_inbox.
   Non assumere mai di "ricordarti" qualcosa dal turno precedente.

2. **Whitelist scope.** Se non vedi un'azione tra le permesse, NON tentarla.
   Il sistema rifiuterà comunque, ma sprechi un turno.

3. **Step-up.** Per azioni sopra soglia, riceverai status='step_up_required'.
   In quel caso fermati e attendi che l'utente confermi via passkey.
   Non riprovare l'azione finché non ricevi la signature.

4. **Negoziazione: persegui ideal price, rispetta floor.**
   - Se sei BUY: ideal_price è il tuo target ottimo, reservation_price è il cap massimo
   - Se sei SELL: ideal_price è il target ottimo, reservation_price è il floor minimo
   - Hard cap: 6 round per negoziazione. Al 5° round vai a "best and final".
   - Se floor irraggiungibile a metà tempo: chiedi all'utente con ask_user.
   
5. **Mini-asta su match multipli.**
   Se hai N>1 match per uno stesso intent, manda offerta a tutti in parallelo.
   Accetta la migliore quando arriva.

6. **Comunicazione pseudonimizzata.**
   Nei messaggi di negoziazione, NON includere mai informazioni personali.
   Mai chiedere/dare nome, telefono, indirizzo. La logistica è post-deal.

7. **Sii efficiente.**
   {MAX_TURNS_PER_TICK} turni per tick. Non sprecare. Concludi quando hai fatto.
"""
        return prompt
