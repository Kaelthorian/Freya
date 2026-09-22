"""Lifecycle glue for orchestration-level integration and final response."""
from __future__ import annotations

from uuid import uuid4

from .integration import (INTEGRATION_VERSION, IntegrationPreconditionError,
                          build_integration_input, global_problem_fingerprint)


class IntegrationOrchestrationMixin:
    def _integration_model_calls_used(self, oid: str) -> int:
        records = self.store.list_integrations(oid)
        revisions = [item for item in self.store.list_plan_revisions(oid)
                     if item.get("revision_source_type") == "integration"]

        def calls(item):
            value = (item.get("metrics") or {}).get("model_calls", 0)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        return sum(calls(item) for item in [*records, *revisions])

    def _fail_integrating(self, oid: str, message: str, *, event_started: bool = True) -> None:
        with self.lock:
            failed = self.store.transition_orchestration(
                oid, ("Integrating",), "Failed", error=message,
            )
            if failed is None:
                return
            if event_started:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.integration.failed", "status": "Failed",
                    "message": message,
                })
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.failed", "status": "Failed", "message": message,
            })
            self._archive_dynamic_agents(oid)

    def _timeout_integration(self, oid: str) -> None:
        self._fail_integrating(
            oid, "Orchestration time limit reached during global verification.",
        )

    def _prepare_integration(self, oid: str) -> dict:
        run = self.store.get_orchestration(oid)
        graph = self.store.get_execution_graph(oid)
        evaluations = {}
        for node in graph["nodes"]:
            evaluation_id = node.get("evaluation_id")
            if evaluation_id:
                evaluations[evaluation_id] = self.store.get_evaluation(
                    evaluation_id, include_snapshot=True,
                )
        return build_integration_input(
            # The immutable plan goal is the Task Analyst operational prompt.
            # The human source prompt remains on the orchestration row for audit only.
            original_user_prompt=run["plan"]["goal"], original_plan=run["plan"],
            effective_plan=run.get("effective_plan") or run["plan"],
            plan_revision=int(run.get("current_plan_revision") or 0),
            graph_nodes=graph["nodes"], evaluations=evaluations,
            revision_history=self.store.list_plan_revisions(oid),
        )

    def _complete_or_integrate(self, oid: str, graph, deadline: float) -> None:
        summary = graph.summary()
        unsuccessful = (summary["failed"] + summary["blocked"] + summary["cancelled"]
                        + summary["skipped"])
        if unsuccessful:
            self._finish_graph(oid, graph)
            return
        with self.lock:
            integrating = self.store.transition_orchestration(
                oid, ("Running",), "Integrating",
            )
            if integrating is None:
                return
            round_number = len(self.store.list_integrations(oid)) + 1
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.graph.completed", "status": "Integrating",
                "message": "The execution graph completed; global integration is required.",
                "summary": summary,
            })
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.integration.started", "status": "Integrating",
                "round": round_number,
                "plan_revision": integrating.get("current_plan_revision", 0),
                "criteria_count": len((integrating.get("plan") or {}).get("success_criteria", [])),
                "message": "Freya started orchestration-level integration verification.",
            })
        try:
            resume = self._run_global_integration(oid, deadline)
        except Exception as exc:
            self._fail_integrating(oid, "Global integration failed safely: " + str(exc))
            return
        if resume:
            self._run_graph(oid, self.store.get_orchestration(oid), deadline)

    def _run_global_integration(self, oid: str, deadline: float) -> bool:
        if self.clock() >= deadline:
            self._timeout_integration(oid)
            return False
        try:
            prepared = self._prepare_integration(oid)
        except IntegrationPreconditionError as exc:
            self._fail_integrating(oid, "Global integration precondition failed: " + str(exc))
            return False
        run = self.store.get_orchestration(oid)
        if run["status"] != "Integrating":
            return False
        records_before = self.store.list_integrations(oid)
        round_number = len(records_before) + 1
        used_calls = self._integration_model_calls_used(oid)
        remaining_calls = int(self.config["max_integration_model_calls"]) - used_calls
        if remaining_calls < 1 and not self.global_verifier.offline:
            self._fail_integrating(oid, "The integration model-call budget is exhausted.")
            return False
        technical_error = None
        try:
            with self.integration_lock:
                outcome = self.global_verifier.verify(
                    prepared, max_model_calls=max(0, min(2, remaining_calls)),
                )
        except Exception as exc:
            technical_error = str(exc)
            outcome = {
                "status": "error",
                "summary": "Global verifier failed: " + technical_error[:1000],
                "criteria": [{"criterion": item, "status": "unknown",
                              "reason": "The global verifier did not complete.", "evidence": []}
                             for item in prepared["context"]["global_success_criteria"]],
                "cross_task_issues": [],
                "missing_evidence": list(prepared["context"]["global_success_criteria"]),
                "responsible_task_ids": [], "recommended_action": "fail",
                "metrics": dict(getattr(self.global_verifier, "metrics", {}) or {}),
                "deterministic": False,
                "context_truncated": bool(prepared["context_truncated"]),
            }
        if self.clock() >= deadline:
            self._timeout_integration(oid)
            return False
        result = {key: outcome[key] for key in (
            "status", "summary", "criteria", "cross_task_issues", "missing_evidence",
            "responsible_task_ids", "recommended_action",
        )}
        problem_fingerprint = global_problem_fingerprint(result)
        integration_id = str(uuid4())
        committed = self.store.commit_integration(
            integration_id, oid, round_number=round_number,
            plan_revision=int(run.get("current_plan_revision") or 0),
            integration_version=INTEGRATION_VERSION, result=result,
            metrics=outcome.get("metrics") or {}, snapshot=prepared["snapshot"],
            context_truncated=bool(outcome.get("context_truncated")),
            deterministic=bool(outcome.get("deterministic")),
            problem_fingerprint=problem_fingerprint,
        )
        if committed is None:
            if self.store.get_orchestration(oid)["status"] == "Integrating":
                self._fail_integrating(
                    oid, "A stale global verification result was discarded.",
                )
            return False
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Integrating":
                return False
            event_type = ("freya.integration.failed" if technical_error
                          else "freya.integration.completed")
            self.store.add_orchestration_event(oid, {
                "event_type": event_type,
                "status": "Success" if result["status"] == "accepted" else "Failed",
                "integration_id": integration_id, "round": round_number,
                "plan_revision": run.get("current_plan_revision", 0),
                "integration_status": result["status"],
                "criteria_count": len(result["criteria"]), "message": result["summary"],
            })
        if result["status"] == "accepted":
            return self._finalize_integration(oid, integration_id, prepared, result, deadline)
        if result["status"] == "error":
            self._fail_integrating(
                oid, "All active tasks were accepted, but global verification failed technically: "
                + result["summary"], event_started=False,
            )
            return False
        return self._recover_integration(
            oid, integration_id, prepared, result, problem_fingerprint,
            records_before, deadline,
        )

    def _finalize_integration(self, oid: str, integration_id: str, prepared: dict,
                              result: dict, deadline: float) -> bool:
        remaining = (int(self.config["max_integration_model_calls"])
                     - self._integration_model_calls_used(oid))
        with self.integration_lock:
            response, metrics, fallback = self.result_integrator.compose(
                prepared, result, max_model_calls=max(0, min(2, remaining)),
            )
        if self.clock() >= deadline:
            self._timeout_integration(oid)
            return False
        completed = self.store.finalize_accepted_integration(oid, integration_id, response)
        if completed is None:
            if self.store.get_orchestration(oid)["status"] == "Integrating":
                self._fail_integrating(
                    oid, "The final response snapshot became stale before commit.",
                )
            return False
        with self.lock:
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.final_response.created", "status": "Success",
                "integration_id": integration_id, "fallback": fallback,
                "model_calls": metrics.get("model_calls", 0),
                "metrics": metrics,
                "message": "Freya created a grounded final response.",
            })
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.completed", "status": "Success", "message": response,
            })
            self._archive_dynamic_agents(oid)
        return False

    def _recover_integration(self, oid: str, integration_id: str, prepared: dict,
                             result: dict, problem_fingerprint: str,
                             prior_integrations: list[dict], deadline: float) -> bool:
        prior_recovery_rounds = sum(
            item.get("status") in {"needs_work", "blocked"} for item in prior_integrations
        )
        if prior_recovery_rounds >= int(self.config["max_integration_rounds"]):
            self._fail_integrating(
                oid, "All planned tasks were individually accepted, but the global objective "
                "remained unsatisfied after the integration recovery budget.",
            )
            return False
        if any(item.get("problem_fingerprint") == problem_fingerprint
               for item in prior_integrations):
            self._fail_integrating(
                oid, "The same global integration gap repeated; additional work was stopped.",
            )
            return False
        run = self.store.get_orchestration(oid)
        revisions = self.store.list_plan_revisions(oid)
        if len(revisions) >= int(self.config["max_plan_revisions"]):
            self._fail_integrating(oid, "The shared plan-revision budget is exhausted.")
            return False
        remaining = (int(self.config["max_integration_model_calls"])
                     - self._integration_model_calls_used(oid))
        if remaining < 1 or self.integration_replanner.model is None:
            self._fail_integrating(
                oid, "Global verification requires additional work, but no safe integration "
                "replanner call is available.",
            )
            return False
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Integrating":
                return False
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.integration.recovery_started", "status": "Integrating",
                "integration_id": integration_id,
                "round": len(prior_integrations) + 1,
                "plan_revision": run.get("current_plan_revision", 0),
                "criteria_count": len(result["criteria"]),
                "message": "Freya started bounded append-only integration recovery.",
            })
        graph_nodes = self.store.get_execution_graph(oid)["nodes"]
        accepted = {node["plan_task_id"] for node in graph_nodes
                    if node["state"] == "success" and node.get("evaluation_status") == "accepted"}
        historical = {node["plan_task_id"] for node in graph_nodes}
        try:
            with self.integration_lock:
                revision = self.integration_replanner.create_revision(
                    current_plan=run.get("effective_plan") or run["plan"],
                    integration=result, accepted_task_ids=accepted,
                    historical_task_ids=historical,
                    max_tasks=int(self.config["max_delegated_tasks"]),
                    max_model_calls=min(2, remaining),
                )
        except Exception as exc:
            self._fail_integrating(oid, "Integration replanning failed: " + str(exc))
            return False
        if self.clock() >= deadline:
            self._timeout_integration(oid)
            return False
        revision_id = str(uuid4())
        saved = self.store.commit_integration_plan_revision(
            revision_id, oid, integration_id, summary=revision["summary"],
            plan=revision["plan"], new_task_ids=revision["new_task_ids"],
            metrics=revision.get("metrics") or {},
        )
        if saved is None:
            if self.store.get_orchestration(oid)["status"] == "Integrating":
                self._fail_integrating(
                    oid, "The integration replan snapshot became stale before commit.",
                )
            return False
        with self.lock:
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.integration.replan_created", "status": "Running",
                "integration_id": integration_id, "plan_revision_id": revision_id,
                "revision": saved["revision"], "new_task_ids": revision["new_task_ids"],
                "message": "Freya committed an append-only integration revision.",
            })
        return True
