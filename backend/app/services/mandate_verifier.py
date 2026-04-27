"""
Mandate Verifier — il gate di autorizzazione di ogni azione agente.

Questo è il cuore del sistema. Ogni azione di un agente passa di qui.
Se non c'è un mandate attivo che permette esplicitamente l'azione,
l'azione viene rifiutata.

Whitelist-based, never blacklist.
"""
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass
from sqlalchemy.orm import Session

from app.models.schema import Mandate, Agent, AuditLog


# ============================================================================
# Tipi di errore
# ============================================================================

class MandateError(Exception):
    """Base class per tutti gli errori di mandate."""
    code: str = "mandate_error"


class NoActiveMandate(MandateError):
    code = "no_active_mandate"


class MandateExpired(MandateError):
    code = "mandate_expired"


class MandateRevoked(MandateError):
    code = "mandate_revoked"


class ActionNotAllowed(MandateError):
    code = "action_not_allowed"


class LimitExceeded(MandateError):
    code = "limit_exceeded"


class ConstraintViolation(MandateError):
    code = "constraint_violation"


@dataclass
class StepUpRequired:
    """Non è un errore: indica che serve step-up dell'utente."""
    action: str
    params: dict
    reason: str


# ============================================================================
# Verifier principale
# ============================================================================

class MandateVerifier:
    """
    Singolo punto di verifica per ogni azione agente.
    
    Use:
        verifier = MandateVerifier(db)
        try:
            mandate = verifier.authorize(agent_id, "send_offer", {"price_cents": 5000})
            # ... esegui azione
            verifier.record_usage(mandate, "send_offer", {"price_cents": 5000}, success=True)
        except MandateError as e:
            verifier.log_failed(agent_id, "send_offer", e)
            raise
        except StepUpRequired as step:
            # Push notification all'utente per conferma biometrica
            return queue_step_up(step)
    """
    
    def __init__(self, db: Session):
        self.db = db
    
    # ------------------------------------------------------------------------
    # Authorization (chiamata prima dell'azione)
    # ------------------------------------------------------------------------
    
    def authorize(self, agent_id: str, action: str, params: dict) -> Mandate:
        """
        Solleva eccezione se l'azione non è autorizzata.
        Solleva StepUpRequired se serve conferma biometrica utente.
        Ritorna il mandate attivo se tutto ok.
        """
        # 1. Carica mandate attivo
        mandate = self._get_active_mandate(agent_id)
        
        # 2. Reset contatori giornalieri se necessario
        self._reset_daily_counters_if_needed(mandate)
        
        # 3. Verifica scope (whitelist)
        self._check_scope(mandate, action)
        
        # 4. Verifica constraints (geo, categoria, ecc.)
        self._check_constraints(mandate, action, params)
        
        # 5. Verifica limiti
        self._check_limits(mandate, action, params)
        
        # 6. Step-up check (può sollevare StepUpRequired)
        self._check_step_up(mandate, action, params)
        
        return mandate
    
    # ------------------------------------------------------------------------
    # Verifiche individuali
    # ------------------------------------------------------------------------
    
    def _get_active_mandate(self, agent_id: str) -> Mandate:
        mandate = (
            self.db.query(Mandate)
            .filter(Mandate.agent_id == agent_id)
            .filter(Mandate.revoked_at.is_(None))
            .order_by(Mandate.issued_at.desc())
            .first()
        )
        if not mandate:
            raise NoActiveMandate(f"No active mandate for agent {agent_id}")
        
        if mandate.expires_at < datetime.utcnow():
            raise MandateExpired(f"Mandate {mandate.id} expired at {mandate.expires_at}")
        
        if mandate.revoked_at is not None:
            raise MandateRevoked(f"Mandate {mandate.id} revoked: {mandate.revocation_reason}")
        
        return mandate
    
    def _check_scope(self, mandate: Mandate, action: str):
        allowed = mandate.scope.get("allowed_actions", [])
        forbidden = mandate.scope.get("forbidden_actions", [])
        
        if action in forbidden:
            raise ActionNotAllowed(f"Action {action} explicitly forbidden")
        
        if action not in allowed:
            # Whitelist: se non esplicitamente permesso, è negato
            raise ActionNotAllowed(
                f"Action {action} not in allowed_actions list. "
                f"Allowed: {allowed}"
            )
    
    def _check_constraints(self, mandate: Mandate, action: str, params: dict):
        constraints = mandate.constraints
        
        # Geo
        geo_scope = constraints.get("geo_scope", [])
        if geo_scope and "location" in params:
            location_country = self._extract_country(params["location"])
            if location_country and location_country not in geo_scope:
                raise ConstraintViolation(
                    f"Location {location_country} not in geo_scope {geo_scope}"
                )
        
        # Categorie
        forbidden_cats = constraints.get("categories_forbidden", [])
        if "category" in params and params["category"] in forbidden_cats:
            raise ConstraintViolation(f"Category {params['category']} is forbidden")
        
        allowed_cats = constraints.get("categories_allowed", ["*"])
        if "*" not in allowed_cats and "category" in params:
            if params["category"] not in allowed_cats:
                raise ConstraintViolation(
                    f"Category {params['category']} not in allowed list"
                )
    
    def _check_limits(self, mandate: Mandate, action: str, params: dict):
        limits = mandate.limits
        price_eur = self._extract_price_eur(params)
        
        # Per-deal cap
        if price_eur and "max_price_per_deal_eur" in limits:
            cap = Decimal(str(limits["max_price_per_deal_eur"]))
            if price_eur > cap:
                raise LimitExceeded(
                    f"Price €{price_eur} exceeds per-deal cap €{cap}"
                )
        
        # Daily volume
        if price_eur and action in ("accept_offer", "create_deal"):
            daily_cap = Decimal(str(limits.get("max_total_volume_eur_per_day", 0)))
            if daily_cap and (mandate.spent_today_eur + price_eur) > daily_cap:
                raise LimitExceeded(
                    f"Daily volume cap €{daily_cap} would be exceeded"
                )
        
        # Mandate-lifetime volume
        if price_eur and action in ("accept_offer", "create_deal"):
            total_cap = Decimal(str(limits.get("max_total_volume_eur_per_mandate", 0)))
            if total_cap and (mandate.spent_total_eur + price_eur) > total_cap:
                raise LimitExceeded(
                    f"Mandate total volume cap €{total_cap} would be exceeded"
                )
        
        # Deal count daily
        if action in ("accept_offer", "create_deal"):
            deals_cap = limits.get("max_deals_per_day", 0)
            if deals_cap and mandate.deals_count >= deals_cap:
                raise LimitExceeded(
                    f"Daily deals cap ({deals_cap}) reached"
                )
    
    def _check_step_up(self, mandate: Mandate, action: str, params: dict):
        rules = mandate.step_up_required_for or []
        price_eur = self._extract_price_eur(params)
        
        for rule in rules:
            if rule.get("action") != action:
                continue
            
            # Step-up sempre richiesto?
            if rule.get("always", False):
                if not params.get("step_up_signature"):
                    raise StepUpRequired(
                        action=action,
                        params=params,
                        reason=f"Action {action} always requires step-up"
                    )
            
            # Step-up oltre soglia €?
            threshold = rule.get("above_eur")
            if threshold and price_eur and price_eur > Decimal(str(threshold)):
                if not params.get("step_up_signature"):
                    raise StepUpRequired(
                        action=action,
                        params=params,
                        reason=f"Price €{price_eur} above threshold €{threshold}"
                    )
    
    # ------------------------------------------------------------------------
    # Usage recording (chiamata dopo l'azione)
    # ------------------------------------------------------------------------
    
    def record_usage(
        self,
        mandate: Mandate,
        action: str,
        params: dict,
        success: bool,
        result: Optional[dict] = None,
        error_code: Optional[str] = None,
    ):
        """Aggiorna contatori e scrive audit log."""
        if success and action in ("accept_offer", "create_deal"):
            price_eur = self._extract_price_eur(params)
            if price_eur:
                mandate.spent_today_eur = (mandate.spent_today_eur or Decimal(0)) + price_eur
                mandate.spent_total_eur = (mandate.spent_total_eur or Decimal(0)) + price_eur
                mandate.deals_count = (mandate.deals_count or 0) + 1
        
        log_entry = AuditLog(
            user_id=mandate.user_id,
            agent_id=mandate.agent_id,
            mandate_id=mandate.id,
            action=action,
            params=params,
            result=result,
            success=success,
            error_code=error_code,
            timestamp=datetime.utcnow(),
        )
        self.db.add(log_entry)
        self.db.commit()
    
    def log_failed(self, agent_id: str, action: str, error: MandateError):
        """Logga tentativi falliti anche se non c'è un mandate."""
        # Per casi NoActiveMandate non abbiamo mandate_id, log degraded
        log_entry = AuditLog(
            user_id=None,
            agent_id=agent_id,
            mandate_id=None,
            action=action,
            success=False,
            error_code=error.code,
            params={"error": str(error)},
            timestamp=datetime.utcnow(),
        )
        self.db.add(log_entry)
        self.db.commit()
    
    # ------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------
    
    def _reset_daily_counters_if_needed(self, mandate: Mandate):
        today = date.today()
        last_reset = (mandate.last_reset_date or datetime.utcnow()).date()
        if last_reset < today:
            mandate.spent_today_eur = Decimal(0)
            mandate.deals_count = 0
            mandate.last_reset_date = datetime.utcnow()
            self.db.commit()
    
    @staticmethod
    def _extract_price_eur(params: dict) -> Optional[Decimal]:
        if "price_cents" in params:
            return Decimal(params["price_cents"]) / Decimal(100)
        if "price_eur" in params:
            return Decimal(str(params["price_eur"]))
        return None
    
    @staticmethod
    def _extract_country(location: str) -> Optional[str]:
        # TODO: parsing location più serio (geocoding o tag-based)
        # V0: assumiamo che l'utente metta tag tipo "Roma, IT"
        if "," in location:
            parts = [p.strip() for p in location.split(",")]
            if len(parts[-1]) == 2:
                return parts[-1].upper()
        return None
