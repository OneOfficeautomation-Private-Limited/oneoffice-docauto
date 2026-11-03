"""Chooses an engine per request and handles retry, fallback and low-confidence escalation."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from .errors import DocAutoError, PolicyDenied, ProviderUnavailable, UnsupportedCapability
from .policy import PolicyEngine
from .provider import Capability
from .registry import ProviderRegistry, RegisteredProvider

log = logging.getLogger("docauto.router")
T = TypeVar("T")


@dataclass
class Attempt:
    provider_id: str
    fallback_from: Optional[str]


class Router:
    def __init__(self, registry: ProviderRegistry, policy: PolicyEngine, config: dict):
        self.registry = registry
        self.policy = policy
        self.config = config
        self.max_retries = int(config.get("router", {}).get("max_retries", 2))
        self.backoff_s = float(config.get("router", {}).get("backoff_seconds", 0.5))

    # ------------------------------------------------------------ selection
    def candidates(self, kind: str, capabilities: list[Capability], languages: list[str],
                   preferred: Optional[str] = None, default_key: Optional[str] = None,
                   strict: bool = False) -> list[RegisteredProvider]:
        if preferred and preferred != "auto":
            p = self.registry.get(preferred)
            if p is None:
                raise ProviderUnavailable(f"unknown provider '{preferred}'", preferred)
            ok, why = self.policy.check(p.info)
            if not ok:
                raise PolicyDenied(why, preferred)
            if not p.available:
                raise ProviderUnavailable(p.reason or "provider not available", preferred)
            missing = [c.value for c in capabilities if c not in p.info.capabilities]
            if missing:
                raise UnsupportedCapability(f"'{preferred}' lacks {missing}", preferred)
            primary = [p]
            if strict:  # caller wants exactly this engine (e.g. engine comparison): no fallback
                return primary
        else:
            primary = []

        pool = [p for p in self.registry.available(kind)
                if self.policy.allows(p.info)
                and all(c in p.info.capabilities for c in capabilities)
                and p.info.supports_languages(languages)]
        if not pool and not primary:
            raise UnsupportedCapability(
                f"no allowed {kind} engine supports {[c.value for c in capabilities]} for languages {languages}")

        default_id = self.config.get("defaults", {}).get(default_key) if default_key else None
        fallback_ids = self.config.get("fallback", {}).get("on_error", []) or []

        def rank(p: RegisteredProvider):
            return (0 if p.info.id == default_id else 1,
                    p.info.cost(capabilities),
                    p.info.tier)

        ordered = sorted(pool, key=rank)
        # configured fallbacks move to the end in configured order
        # the default engine always stays first, even if it is also listed as a fallback
        fb = [p for fid in fallback_ids for p in ordered if p.info.id == fid and p.info.id != default_id]
        rest = [p for p in ordered if p not in fb]
        out: list[RegisteredProvider] = []
        for p in primary + rest + fb:
            if p not in out:
                out.append(p)
        return out

    # ------------------------------------------------------------ execution
    def run(self, candidates: list[RegisteredProvider], fn: Callable[[RegisteredProvider], T]) -> tuple[T, Attempt]:
        last_err: Optional[Exception] = None
        prev: Optional[str] = None
        for p in candidates:
            for attempt in range(self.max_retries + 1):
                try:
                    return fn(p), Attempt(p.info.id, prev)
                except DocAutoError as e:
                    last_err = e
                    if e.retryable and attempt < self.max_retries:
                        time.sleep(self.backoff_s * (2 ** attempt))
                        continue
                    log.warning("provider %s failed (%s), falling back", p.info.id, e.code)
                    break
                except Exception as e:  # unexpected engine failure -> fallback
                    last_err = e
                    log.exception("provider %s crashed", p.info.id)
                    break
            prev = p.info.id
        if isinstance(last_err, DocAutoError):
            raise last_err
        raise DocAutoError(f"all engines failed: {last_err}")
