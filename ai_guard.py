"""ZORA AI availability guard: unavailable AI blocks NEW entries only."""
from __future__ import annotations
import threading, time
from collections import deque
from config import CFG
_lock=threading.Lock(); _calls=deque(); _blocked_until=0.0; _reason=""
def configured(): return any((CFG.groq_api_key, CFG.openrouter_api_key, CFG.gemini_api_key, CFG.anthropic_api_key, CFG.openai_api_key, CFG.mistral_api_key, CFG.huggingface_api_key))
def _prune(now):
    while _calls and _calls[0] < now-86400: _calls.popleft()
def record_call():
    with _lock: _calls.append(time.time()); _prune(time.time())
def record_unavailable(reason):
    global _blocked_until,_reason
    with _lock: _blocked_until=max(_blocked_until,time.time()+max(1,CFG.ai_provider_cooldown_seconds)); _reason=reason[:240]
def record_available():
    global _blocked_until,_reason
    with _lock: _blocked_until=0.0; _reason=""
def status():
    now=time.time()
    with _lock:
        _prune(now); remaining=max(0,int(_blocked_until-now)); lim=CFG.ai_daily_call_limit; over=lim>0 and len(_calls)>=lim
        return {"configured":configured(),"entry_blocked":bool(CFG.ai_entry_block_on_unavailable and (remaining>0 or over)),"blocked_seconds":remaining,"reason":_reason or ("daily AI call limit reached" if over else ""),"calls_24h":len(_calls),"daily_limit":lim}
def before_cycle():
    if not configured(): return True,"AI not configured; technical-only mode"
    st=status()
    return (False, st["reason"] or f"AI unavailable ({st['blocked_seconds']}s cooldown)") if st["entry_blocked"] else (True,"AI capacity available")
