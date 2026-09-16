"""Freya orchestration service: bounded planning, delegation and integration."""
from __future__ import annotations
import json, threading, time
from typing import Any, Callable

class Orchestrator:
    def __init__(self, store, runtime, decide: Callable | None = None, config: dict | None = None):
        self.store, self.runtime = store, runtime
        self.decide = decide
        self.config = {"max_rounds": 6, "max_delegated_tasks": 8, "max_model_calls": 12, "max_wallclock_seconds": 900}
        self.config.update(config or {})

    def submit(self, prompt: str) -> dict:
        run = self.store.create_orchestration(prompt, self.config)
        threading.Thread(target=self._run, args=(run["id"],), daemon=True, name="freya-orchestrator").start()
        return run

    def _decision(self, prompt, agents, results):
        if self.decide: return self.decide(prompt, agents, results)
        enabled=[a for a in agents if a.get("enabled") and a.get("status") != "Offline"]
        if not enabled: return {"action":"respond","message":"No enabled agent is available for this request."}
        return {"action":"delegate","tasks":[{"agent_id":enabled[0]["id"],"objective":prompt}]}

    def _run(self, oid):
        run=self.store.get_orchestration(oid); started=time.monotonic(); results=[]; delegated=0
        self.store.update_orchestration(oid,status="Running"); self.store.add_orchestration_event(oid,{"event_type":"freya.analyzing","status":"Running","message":"Freya is analyzing the request."})
        try:
            for _round in range(int(self.config["max_rounds"])):
                if time.monotonic()-started > self.config["max_wallclock_seconds"]: raise RuntimeError("Orchestration time limit reached.")
                decision=self._decision(run["prompt"], self.store.list_agents(), results)
                action=decision.get("action") if isinstance(decision,dict) else None
                if action == "respond":
                    msg=str(decision.get("message", "")); self.store.update_orchestration(oid,status="Success",response=msg); self.store.add_orchestration_event(oid,{"event_type":"freya.completed","status":"Success","message":msg}); return
                if action not in {"delegate","continue"}: raise ValueError("Freya returned an invalid action.")
                tasks=decision.get("tasks",[])
                if not isinstance(tasks,list) or delegated+len(tasks)>self.config["max_delegated_tasks"]: raise ValueError("Delegation limit reached.")
                for item in tasks:
                    aid=item.get("agent_id"); objective=str(item.get("objective","")).strip()
                    agent=self.store.get_agent(aid)
                    if not agent.get("enabled") or agent.get("status")=="Offline": raise ValueError("Selected agent is disabled or unavailable.")
                    task=self.runtime.submit(aid, objective); did=self.store.add_delegation(oid,aid,objective,task["id"]); delegated+=1
                    self.store.add_orchestration_event(oid,{"event_type":"freya.delegated","status":"Queued","agent_id":aid,"task_id":task["id"],"message":"Delegated objective to agent."})
                    results.append({"delegation_id":did,"task_id":task["id"],"agent_id":aid})
                # bounded wait for submitted tasks; scheduler remains independent
                deadline=time.monotonic()+self.config["max_wallclock_seconds"]
                while any(self.store.get_task(r["task_id"])["status"] in {"Queued","Running","Paused"} for r in results) and time.monotonic()<deadline: time.sleep(.2)
                for r in results:
                    task=self.store.get_task(r["task_id"]); r["status"]=task["status"]; r["result"]=task.get("result")
                if action == "delegate" and results:
                    msg="Freya completed the delegated work.\n\n"+"\n\n".join(str(r.get("result") or r.get("status")) for r in results)
                    self.store.update_orchestration(oid,status="Success",response=msg); self.store.add_orchestration_event(oid,{"event_type":"freya.completed","status":"Success","message":"Freya integrated agent results."}); return
            raise RuntimeError("Maximum orchestration rounds reached.")
        except Exception as exc:
            self.store.update_orchestration(oid,status="Failed",error=str(exc)); self.store.add_orchestration_event(oid,{"event_type":"freya.failed","status":"Failed","message":str(exc)})
