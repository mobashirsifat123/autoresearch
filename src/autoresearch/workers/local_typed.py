"""Purpose: Executes approved experiment scripts without arbitrary shell execution.

The worker is also the runtime-perception boundary. While an approved script
runs, stdout is read line-by-line; declared probe signals are graded by a
deterministic severity ladder (see ``experiments/perception.py``) and a fatal
verdict terminates the whole process group *before* the run would naturally
finish. Suspect observations are retained as evidence, never used to abort on
their own. Heartbeats and process identity are updated during the run so a
healthy long job is not mistaken for a stale lease.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from autoresearch.domain.models import ArtifactReference, ExperimentSpec, content_checksum, utc_now
from autoresearch.experiments.perception import StreamingPerception, build_monitor_report
from autoresearch.experiments.policy import ActionType, ExecutionAction, resolve_action, specification_checksum
from autoresearch.storage.repository import ResearchRepository


@dataclass(frozen=True)
class WorkerCapabilities:
    worker_id: str = "local"
    actions: tuple[str, ...] = tuple(item.value for item in ActionType)
    isolation: str = "workspace-only"
    network: str = "disabled-by-policy"
    max_concurrency: int = 1
    max_runtime_seconds: int = 3600


@dataclass(frozen=True)
class WorkerSubmission:
    job_id: str
    checksum: str
    state: str


_HEARTBEAT_SECONDS = 5.0
_OUTPUT_LIMIT = 1_000_000
_OUTPUT_TAIL_LIMIT = 200_000
_STREAM_LIMIT = 2_000_000


class LocalTypedWorker:
    """Execute only action vectors produced by ``resolve_action``.

    This is workspace containment and environment filtering, not an OS sandbox.
    """

    def __init__(self, repository: ResearchRepository, workspace_root: Path, *, worker_id: str = "local") -> None:
        self.repository = repository
        self.workspace_root = workspace_root.expanduser().resolve()
        self.worker_id = worker_id
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(worker_id=self.worker_id)

    def _event(self, run_id: str, event_type: str, payload: dict[str, object]) -> None:
        self.repository.append_api_event(run_id, event_type, payload)

    def _workspace(self, spec: ExperimentSpec) -> Path:
        root = self.workspace_root
        repo = Path(spec.repository).expanduser().resolve(strict=True)
        if repo != root and root not in repo.parents and repo not in root.parents:
            raise ValueError("unsafe_execution_request: repository is outside worker workspace")
        return repo

    def _result_store_path(self, spec: ExperimentSpec, job_id: str) -> Path:
        return self._workspace(spec) / "run_outputs" / f"worker_result_{job_id}.json"

    def _monitor_store_path(self, spec: ExperimentSpec, job_id: str) -> Path:
        return self._workspace(spec) / "run_outputs" / f"monitor_report_{job_id}.json"

    def _persist_result(self, spec: ExperimentSpec, job_id: str, payload: dict[str, object]) -> None:
        """Persist the job result with a checksum and a DB-side expected value.

        The workspace file alone is not authoritative: an attacker who can edit
        it could also recompute a plain checksum. The digest is therefore also
        stored in the append-only artifact ledger.
        """
        try:
            body = {key: value for key, value in payload.items() if key != "result_checksum"}
            body["result_integrity_version"] = 2
            digest = content_checksum(body)
            stored = {**body, "result_checksum": digest}
            path = self._result_store_path(spec, job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
            try:
                self.repository.save_artifact(ArtifactReference(
                    uuid4().hex, project_id=spec.project_id, run_id=spec.run_id,
                    artifact_type="worker_result", path=str(path),
                    source_evidence=(job_id,), provider="deterministic", model="typed-worker",
                    prompt_version="worker-result-v2", checksum=digest,
                ))
            except Exception:  # noqa: BLE001 — file is still usable this run
                pass
        except OSError:
            pass  # bookkeeping only — the in-memory result is returned regardless

    def _result_artifact_checksum(self, spec: ExperimentSpec, job_id: str) -> str | None:
        row = self.repository.row(
            "SELECT checksum FROM artifacts WHERE run_id=? AND artifact_type='worker_result' "
            "AND source_evidence_json LIKE ? ORDER BY rowid DESC LIMIT 1",
            (spec.run_id, f"%{job_id}%"),
        )
        return str(row["checksum"]) if row and row.get("checksum") else None

    def _stored_result(self, spec: ExperimentSpec, job_id: str) -> dict[str, object] | None:
        try:
            path = self._result_store_path(spec, job_id)
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or "stdout" not in payload:
            return None
        stored_checksum = str(payload.get("result_checksum") or "")
        if stored_checksum:
            body = {key: value for key, value in payload.items() if key != "result_checksum"}
            if content_checksum(body) != stored_checksum:
                raise RuntimeError(
                    "result_integrity: the stored worker result checksum does not match; "
                    "refusing to ingest a tampered result"
                )
            if payload.get("result_integrity_version") == 2:
                ledger_checksum = self._result_artifact_checksum(spec, job_id)
                if not ledger_checksum:
                    raise RuntimeError(
                        "result_integrity: no ledger checksum exists for this stored result; "
                        "re-approval is required"
                    )
                if ledger_checksum != stored_checksum:
                    raise RuntimeError(
                        "result_integrity: the stored result does not match the artifact ledger; "
                        "refusing to ingest a tampered result"
                    )
        return payload

    # ------------------------------------------------------------------
    # Implementation-content integrity
    # ------------------------------------------------------------------

    def _implementation_artifact(self, spec: ExperimentSpec) -> dict[str, object] | None:
        """Latest STATIC_VERIFIED implementation artifact for THIS spec."""
        for row in self.repository.rows(
            "SELECT artifact_type, source_evidence_json, checksum, path, created_at FROM artifacts "
            "WHERE project_id=? AND artifact_type='experiment_implementation' ORDER BY rowid DESC",
            (spec.project_id,),
        ):
            try:
                evidence = json.loads(row.get("source_evidence_json") or "[]")
            except (TypeError, ValueError):
                continue
            if spec.id in {str(item) for item in evidence} and row.get("checksum"):
                return dict(row)
        return None

    def _verification_for_spec(self, spec: ExperimentSpec) -> dict[str, object] | None:
        """Latest implementation-verification payload for THIS spec."""
        for row in self.repository.rows(
            "SELECT path FROM artifacts WHERE project_id=? AND artifact_type='implementation_verification' "
            "ORDER BY rowid DESC",
            (spec.project_id,),
        ):
            try:
                payload = json.loads(Path(str(row["path"])).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and str(payload.get("spec_id") or "") == spec.id:
                return payload
        return None

    def _approval_binding(self, approved_checksum: str) -> dict[str, object]:
        approval = self.repository.approval_by_checksum(approved_checksum) or {}
        actions = approval.get("approved_actions")
        return dict(actions) if isinstance(actions, dict) else {}

    def _check_implementation_integrity(
        self, spec: ExperimentSpec, argv: tuple[str, ...], approved_checksum: str
    ) -> tuple[str, bool]:
        """Refuse to execute bytes that do not match the verified artifact.

        Three layers:
        1. a generated implementation that failed static verification is
           refused (a script with no verification artifact is allowed only
           under the existing approval and is recorded as unverified);
        2. a new approval records the script digest and it must match;
        3. legacy approvals (no digest binding) fall back to the latest
           verified artifact and reject an artifact created after approval.
        """
        if len(argv) < 2:
            return "", False
        script = Path(argv[1])
        if not script.is_file():
            return "", False
        verification = self._verification_for_spec(spec)
        if verification is not None:
            level = str(verification.get("level") or "")
            if level != "STATIC_VERIFIED":
                raise RuntimeError(
                    "implementation_unverified: the implementation failed static verification "
                    f"({'; '.join(str(item) for item in (verification.get('reasons') or [])[:3])}). "
                    "Repair or regenerate it; do not execute it."
                )
        source = script.read_text(encoding="utf-8")
        digest = content_checksum(source)
        binding = self._approval_binding(approved_checksum)
        bound = str(binding.get("implementation_checksum") or "")
        expected_row = self._implementation_artifact(spec)
        expected = str((expected_row or {}).get("checksum") or "")
        if bound:
            if bound != digest:
                raise RuntimeError(
                    "implementation_integrity: the script changed after approval "
                    f"(approved={bound[:16]}, executed={digest[:16]}). "
                    "Re-verify the implementation and record a new exact approval."
                )
            return digest, True
        if expected and expected != digest:
            raise RuntimeError(
                "implementation_integrity: the script changed after static verification "
                f"(verified={expected[:16]}, executed={digest[:16]}). "
                "Regenerate and re-verify the implementation, then re-approve the action."
            )
        if expected_row is not None:
            approval = self.repository.approval_by_checksum(approved_checksum) or {}
            approved_at = str(approval.get("created_at") or "")
            artifact_at = str(expected_row.get("created_at") or "")
            if approved_at and artifact_at and artifact_at > approved_at:
                raise RuntimeError(
                    "implementation_integrity: a verified implementation artifact was created "
                    "after the approval; legacy approvals cannot be re-baselined. Record a new approval."
                )
        return digest, bool(expected)

    # ------------------------------------------------------------------
    # Job lifecycle
    # ------------------------------------------------------------------

    def submit(self, spec: ExperimentSpec, actions: tuple[ExecutionAction, ...], *, approved_checksum: str, timeout_seconds: int = 3600) -> WorkerSubmission:
        expected = specification_checksum(spec, actions, self.worker_id)
        if approved_checksum != expected:
            raise PermissionError("checksum_mismatch: exact experiment specification approval does not match")
        self._workspace(spec)
        if not actions:
            raise ValueError("unsafe_execution_request: at least one typed action is required")
        for action in actions:
            resolve_action(action, self._workspace(spec), sys.executable)
        existing = self.repository.row("SELECT * FROM worker_jobs WHERE spec_id=? AND checksum=? AND state IN ('QUEUED','LEASED','RUNNING','ANALYZING','COMPLETED') ORDER BY created_at DESC LIMIT 1", (spec.id, expected))
        if existing:
            return WorkerSubmission(existing["id"], expected, existing["state"])
        job_id = uuid4().hex
        now = utc_now()
        # Attempts must persist across jobs. The old code restarted at 0 for
        # every new job, so a repeated timeout never escalated to parameter
        # review.
        prior = self.repository.row(
            "SELECT COUNT(*) AS n FROM worker_jobs WHERE spec_id=? AND checksum=?",
            (spec.id, expected),
        ) or {"n": 0}
        attempt = int(prior.get("n") or 0)
        self.repository.save_worker_job({"id": job_id, "project_id": spec.project_id, "run_id": spec.run_id, "spec_id": spec.id, "worker": self.worker_id, "state": "QUEUED", "attempt": attempt, "checksum": expected, "actions": [asdict(action) for action in actions], "timeout_seconds": min(timeout_seconds, self.capabilities().max_runtime_seconds), "created_at": now, "updated_at": now})
        self.repository.append_worker_transition(spec.run_id, "AWAITING_APPROVAL", "QUEUED", reason="exact approval verified", worker_id=self.worker_id)
        self._event(spec.run_id, "job.queued", {"job_id": job_id, "worker": self.worker_id, "checksum": expected})
        return WorkerSubmission(job_id, expected, "QUEUED")

    @staticmethod
    def _canceled_payload(job_id: str, attempt: int) -> dict[str, object]:
        return {
            "job_id": job_id,
            "state": "CANCELED",
            "exit_code": 130,
            "attempt": attempt,
            "stdout": "",
            "stderr": "execution did not start: job was cancelled",
            "perception": {
                "aborted": True,
                "category": "CANCELED",
                "reason": "job was cancelled before execution",
                "monitor_report": "",
            },
            "monitor_report": "",
        }

    async def _heartbeat_loop(self, job_id: str, stop: asyncio.Event) -> None:
        """Keep a live lease while the process runs. A long job is not stale.

        The update is targeted so a cancellation written by another process is
        never overwritten by a full-row heartbeat write.
        """
        while not stop.is_set():
            self.repository.execute(
                "UPDATE worker_jobs SET heartbeat_at=?, updated_at=? "
                "WHERE id=? AND state IN ('LEASED','RUNNING')",
                (utc_now(), utc_now(), job_id),
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                continue

    async def _terminate(self, process: asyncio.subprocess.Process, *, graceful_seconds: float = 5.0) -> None:
        if process.returncode is not None:
            return
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        try:
            if killpg is not None and getpgid is not None:
                killpg(getpgid(process.pid), signal.SIGTERM)
            else:  # pragma: no cover — Windows/feature-limited hosts
                process.terminate()
        except (ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(process.wait(), timeout=graceful_seconds)
        except asyncio.TimeoutError:
            try:
                if killpg is not None and getpgid is not None:
                    killpg(getpgid(process.pid), signal.SIGKILL)
                else:  # pragma: no cover
                    process.kill()
            except (ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    async def run(self, spec: ExperimentSpec, actions: tuple[ExecutionAction, ...], *, approved_checksum: str, timeout_seconds: int = 3600) -> dict[str, object]:
        submission = self.submit(spec, actions, approved_checksum=approved_checksum, timeout_seconds=timeout_seconds)
        job = self.repository.worker_job(submission.job_id)
        assert job is not None
        if job["state"] == "COMPLETED":
            stored = self._stored_result(spec, submission.job_id)
            if stored is not None:
                # A cached result is only valid for the same code. Refuse to
                # return measurements for a script that has since changed.
                for action in actions:
                    if action.kind is ActionType.RUN_PYTHON_SCRIPT:
                        argv = resolve_action(action, self._workspace(spec), sys.executable)
                        self._check_implementation_integrity(spec, argv, approved_checksum)
                return stored
            self._event(spec.run_id, "worker.result_lost", {
                "job_id": submission.job_id,
                "note": "stored result missing; re-executing the same approved deterministic action",
            })
            # The result payload died with the previous process (results are
            # process-local; worker_jobs keeps bookkeeping only). Re-execute the
            # SAME approved deterministic action rather than returning a
            # stdout-less row the measurement ingester cannot use. The job must
            # return to QUEUED so the lease CAS can claim it.
            self.repository.execute(
                "UPDATE worker_jobs SET state='QUEUED' WHERE id=? AND state='COMPLETED'",
                (submission.job_id,),
            )
        if str(job["state"]) in {"LEASED", "RUNNING", "ANALYZING"}:
            raise RuntimeError(
                "duplicate_execution: a live worker job already owns this approved action"
            )
        lease_id = uuid4().hex
        now = utc_now()
        # Compare-and-swap the lease so a cancellation between submit() and
        # here cannot be overwritten by a blind full-row write.
        self.repository.execute(
            "UPDATE worker_jobs SET lease_id=?, state='LEASED', attempt=attempt+1, "
            "updated_at=?, heartbeat_at=?, lease_expires_at=? "
            "WHERE id=? AND state='QUEUED'",
            (lease_id, now, now, now, submission.job_id),
        )
        leased = self.repository.worker_job(submission.job_id) or job
        if str(leased.get("state")) != "LEASED":
            if str(leased.get("state")) == "CANCELED":
                return {
                    "job_id": submission.job_id,
                    "state": "CANCELED",
                    "exit_code": 130,
                    "attempt": int(leased.get("attempt") or 0),
                    "stdout": "",
                    "stderr": "execution did not start: job was cancelled",
                    "perception": {
                        "aborted": True,
                        "category": "CANCELED",
                        "reason": "job was cancelled before the process started",
                        "monitor_report": "",
                    },
                    "monitor_report": "",
                }
            raise RuntimeError(
                "duplicate_execution: another worker acquired the lease before this process"
            )
        self.repository.append_worker_transition(spec.run_id, "QUEUED", "LEASED", reason="local lease acquired", worker_id=self.worker_id, attempt=int(leased.get("attempt") or 0))
        self._event(spec.run_id, "job.leased", {"job_id": submission.job_id, "worker": self.worker_id, "attempt": int(leased.get("attempt") or 0)})

        output: list[str] = []
        output_tail: list[str] = []
        output_tail_chars = 0
        last_stdout_line = ""
        errors: list[str] = []
        exit_code = 0
        abort_reason = ""
        abort_category = ""
        timed_out = False
        stalled = False
        implementation_digest = ""
        implementation_verified = False
        perception = StreamingPerception(getattr(spec, "monitor_contract", {}) or {})
        started = time.monotonic()
        total_output = 0

        try:
            for raw in job["actions"]:
                action = ExecutionAction(ActionType(raw["kind"]), str(raw["target"]), tuple(str(item) for item in raw.get("arguments", ())))
                argv = resolve_action(action, self._workspace(spec), sys.executable)
                if action.kind is ActionType.RUN_PYTHON_SCRIPT:
                    implementation_digest, implementation_verified = self._check_implementation_integrity(
                        spec, argv, approved_checksum
                    )

                # Last check before spawn: cancellation can arrive between the
                # lease CAS and this line.
                pre_spawn = self.repository.worker_job(submission.job_id) or job
                if str(pre_spawn.get("state")) == "CANCELED":
                    return self._canceled_payload(submission.job_id, int(pre_spawn.get("attempt") or 0))
                if str(pre_spawn.get("state")) != "LEASED":
                    raise RuntimeError("duplicate_execution: job left the LEASED state before spawn")
                self.repository.execute(
                    "UPDATE worker_jobs SET state='RUNNING', updated_at=?, heartbeat_at=? "
                    "WHERE id=? AND state='LEASED'",
                    (utc_now(), utc_now(), submission.job_id),
                )
                self.repository.append_worker_transition(spec.run_id, "LEASED", "RUNNING", reason=action.kind.value, worker_id=self.worker_id, attempt=int(pre_spawn.get("attempt") or 0))
                self._event(spec.run_id, "job.started", {"job_id": submission.job_id, "action": action.kind.value})

                env = {"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0", "PYTHONUNBUFFERED": "1"}
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(self._workspace(spec)),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True, env=env, limit=_STREAM_LIMIT,
                )
                self._processes[submission.job_id] = process
                # Targeted process identity update; a concurrent CANCELED wins.
                self.repository.execute(
                    "UPDATE worker_jobs SET process_id=?, process_group_id=?, updated_at=?, heartbeat_at=? "
                    "WHERE id=? AND state='RUNNING'",
                    (process.pid, process.pid, utc_now(), utc_now(), submission.job_id),
                )
                post_spawn = self.repository.worker_job(submission.job_id) or {}
                if str(post_spawn.get("state")) == "CANCELED":
                    await self._terminate(process)
                    return self._canceled_payload(submission.job_id, int(post_spawn.get("attempt") or 0))

                stop = asyncio.Event()
                last_output = [time.monotonic()]
                fatal_seen = [False]
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(submission.job_id, stop))

                async def read_stream(stream, sink: list[str], *, parse_probes: bool) -> None:
                    nonlocal total_output, output_tail_chars, last_stdout_line, abort_reason, abort_category
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace")
                        last_output[0] = time.monotonic()
                        total_output += len(line)
                        if total_output <= _OUTPUT_LIMIT:
                            sink.append(text)
                        if parse_probes:
                            # Preserve the tail even when the full log exceeds
                            # the cap; the final measurement document lives at
                            # the end of stdout. Keep the last complete line
                            # separately so a large JSON document is never
                            # dropped by whole-line trimming.
                            last_stdout_line = text
                            output_tail.append(text)
                            output_tail_chars += len(text)
                            while output_tail_chars > _OUTPUT_TAIL_LIMIT and output_tail:
                                output_tail_chars -= len(output_tail.pop(0))
                            verdict = perception.observe_line(text)
                            if verdict is not None and verdict.severity == "fatal" and not fatal_seen[0]:
                                fatal_seen[0] = True
                                if not abort_category:
                                    abort_reason = verdict.reason
                                    abort_category = "PERCEPTION_ABORT"
                                await self._terminate(process)
                                break

                stdout_task = asyncio.create_task(read_stream(process.stdout, output, parse_probes=True))
                stderr_task = asyncio.create_task(read_stream(process.stderr, errors, parse_probes=False))
                readers = [stdout_task, stderr_task]

                async def watchdog() -> None:
                    nonlocal abort_reason, abort_category, stalled
                    stall_seconds = float(perception.contract.stall_seconds or 0.0)
                    while not stop.is_set():
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1.0)
                            return
                        except asyncio.TimeoutError:
                            pass
                        if stall_seconds > 0 and (time.monotonic() - last_output[0]) > stall_seconds:
                            if abort_category:
                                return
                            stalled = True
                            abort_reason = (
                                f"no stdout or probe for {stall_seconds:g}s "
                                "(stall watchdog)"
                            )
                            abort_category = "STALL"
                            await self._terminate(process)
                            return

                watchdog_task = asyncio.create_task(watchdog())
                try:
                    remaining = max(1.0, float(min(timeout_seconds, self.capabilities().max_runtime_seconds)))
                    await asyncio.wait_for(asyncio.gather(*readers), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    abort_category = abort_category or "TIMEOUT"
                    abort_reason = abort_reason or f"execution exceeded {timeout_seconds}s"
                    await self._terminate(process)
                    for task in readers:
                        task.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                finally:
                    stop.set()
                    watchdog_task.cancel()
                    heartbeat_task.cancel()
                    await asyncio.gather(watchdog_task, heartbeat_task, return_exceptions=True)
                    if process.returncode is None:
                        await self._terminate(process)
                    await process.wait()
                    self._processes.pop(submission.job_id, None)

                exit_code = int(process.returncode or 0)
                self.repository.execute(
                    "UPDATE worker_jobs SET last_log_offset=?, heartbeat_at=?, updated_at=? "
                    "WHERE id=? AND state IN ('RUNNING','LEASED')",
                    (total_output, utc_now(), utc_now(), submission.job_id),
                )
                current_job = self.repository.worker_job(submission.job_id) or job
                if str(current_job.get("state") or "") == "CANCELED":
                    self._cancelled.add(submission.job_id)
                if submission.job_id in self._cancelled:
                    exit_code = 130
                    break
                if abort_category or exit_code != 0:
                    break

            elapsed = time.monotonic() - started
            monitor_report = build_monitor_report(
                perception, exit_code=exit_code, elapsed_seconds=elapsed,
                abort_reason=abort_reason, abort_category=abort_category,
                timed_out=timed_out, stalled=stalled,
            )
            monitor_path = self._monitor_store_path(spec, submission.job_id)
            monitor_text = json.dumps(monitor_report, indent=2, sort_keys=True)
            monitor_written = False
            monitor_error = ""
            try:
                monitor_path.parent.mkdir(parents=True, exist_ok=True)
                monitor_path.write_text(monitor_text, encoding="utf-8")
                self.repository.save_artifact(ArtifactReference(
                    uuid4().hex, project_id=spec.project_id, run_id=spec.run_id,
                    artifact_type="execution_monitor", path=str(monitor_path),
                    source_evidence=(spec.id, submission.job_id),
                    provider="deterministic", model="runtime-perception",
                    prompt_version="runtime-perception-v1",
                    checksum=content_checksum(monitor_text),
                ))
                monitor_written = True
            except OSError as exc:
                monitor_error = f"{type(exc).__name__}: {exc}"

            # Re-read immediately before finalization; a cancel can arrive
            # while the monitor artifact is being written.
            external = self.repository.worker_job(submission.job_id) or {}
            external_cancel = str(external.get("state") or "") == "CANCELED"
            final_state = "CANCELED" if (submission.job_id in self._cancelled or external_cancel) else (
                "COMPLETED" if (exit_code == 0 and not abort_category) else "FAILED"
            )
            error_payload = None
            if final_state == "FAILED":
                error_payload = {
                    "category": abort_category or ("EXIT_CODE" if exit_code else "UNKNOWN"),
                    "reason": abort_reason or f"process exited with code {exit_code}",
                    "exit_code": exit_code,
                }
            # Conditional finalization: never overwrite a CONCURRENT cancel.
            self.repository.execute(
                "UPDATE worker_jobs SET state=?, updated_at=?, heartbeat_at=?, "
                "process_group_id=NULL, error_json=? WHERE id=? AND state != 'CANCELED'",
                (
                    final_state, utc_now(), utc_now(),
                    json.dumps(error_payload) if error_payload else None,
                    submission.job_id,
                ),
            )
            current = self.repository.worker_job(submission.job_id) or job
            if str(current.get("state")) == "CANCELED" and final_state != "CANCELED":
                final_state = "CANCELED"
                exit_code = 130
                error_payload = None
            self.repository.append_worker_transition(
                spec.run_id, "RUNNING", final_state,
                reason=abort_reason or "process group finished",
                worker_id=self.worker_id, attempt=int(current.get("attempt") or 0),
            )
            self._event(
                spec.run_id,
                "job.completed" if final_state == "COMPLETED" else "job.canceled" if final_state == "CANCELED" else "run.failed",
                {
                    "job_id": submission.job_id, "state": final_state, "exit_code": exit_code,
                    "failure_category": (error_payload or {}).get("category"),
                    "failure_reason": (error_payload or {}).get("reason", "")[:300],
                    "suspect_verdicts": perception.suspect_count,
                    "fatal_verdicts": perception.fatal_count,
                },
            )
            stdout_text = "".join(output)
            if total_output > _OUTPUT_LIMIT:
                tail_text = "".join(output_tail)
                if last_stdout_line and last_stdout_line not in tail_text:
                    tail_text += last_stdout_line
                stdout_text += (
                    "\n...[stdout truncated; tail preserved]...\n"
                    + tail_text
                )
            payload: dict[str, object] = {
                "job_id": submission.job_id, "state": final_state, "exit_code": exit_code,
                "attempt": int(current.get("attempt") or 0),
                "stdout": stdout_text, "stderr": "".join(errors),
                "output_truncated": total_output > _OUTPUT_LIMIT,
                "checksum": approved_checksum,
                "implementation_checksum": implementation_digest,
                "implementation_verified": implementation_verified,
                "monitor_report": str(monitor_path) if monitor_written else "",
                "monitor_error": monitor_error,
                "perception": {
                    "aborted": bool(abort_category),
                    "category": abort_category,
                    "reason": abort_reason,
                    "suspect_verdicts": perception.suspect_count,
                    "fatal_verdicts": perception.fatal_count,
                    "monitor_report": str(monitor_path) if monitor_written else "",
                },
            }
            self._persist_result(spec, submission.job_id, payload)
            if perception.suspect_count or perception.fatal_count:
                self._event(spec.run_id, "experiment.perception", {
                    "job_id": submission.job_id,
                    "suspect_verdicts": perception.suspect_count,
                    "fatal_verdicts": perception.fatal_count,
                    "abort_reason": abort_reason,
                    "monitor_report": str(monitor_path),
                })
            return payload
        except BaseException:
            # Never leave a process group running after an unexpected error.
            active = self._processes.get(submission.job_id)
            if active is not None and active.returncode is None:
                await self._terminate(active)
            self._processes.pop(submission.job_id, None)
            # Job bookkeeping on unexpected failure; re-raise so the caller
            # records the scientific failure rather than masking it.
            try:
                current = self.repository.worker_job(submission.job_id) or job
                if current.get("state") in {"RUNNING", "LEASED"}:
                    self.repository.save_worker_job({
                        **current, "state": "FAILED", "updated_at": utc_now(),
                        "error": {"category": "WORKER_EXCEPTION", "reason": "worker raised before completion"},
                    })
            except Exception:  # noqa: BLE001 — preserve the original exception
                pass
            raise

    async def heartbeat(self, job_id: str) -> dict[str, object] | None:
        job = self.repository.worker_job(job_id)
        if not job:
            return None
        self.repository.execute(
            "UPDATE worker_jobs SET heartbeat_at=?, updated_at=? "
            "WHERE id=? AND state IN ('LEASED','RUNNING','QUEUED')",
            (utc_now(), utc_now(), job_id),
        )
        return self.repository.worker_job(job_id)

    async def cancel(self, job_id: str) -> dict[str, object] | None:
        job = self.repository.worker_job(job_id)
        if not job:
            return None
        self._cancelled.add(job_id)
        process = self._processes.get(job_id)
        if process and process.returncode is None:
            await self._terminate(process)
        previous_state = str(job.get("state"))
        job["state"] = "CANCELED"
        job["updated_at"] = utc_now()
        self.repository.save_worker_job(job)
        self.repository.append_worker_transition(job["run_id"], previous_state, "CANCELED", reason="cancellation requested", worker_id=self.worker_id, attempt=int(job["attempt"]))
        self._event(job["run_id"], "job.canceled", {"job_id": job_id})
        return job

    def recover_stale(self, *, stale_after_seconds: int = 120) -> list[dict[str, object]]:
        cutoff = time.time() - stale_after_seconds
        recovered: list[dict[str, object]] = []
        for job in self.repository.rows("SELECT id FROM worker_jobs WHERE state IN ('LEASED','RUNNING')"):
            current = self.repository.worker_job(job["id"])
            if current and current.get("heartbeat_at"):
                from datetime import datetime
                if datetime.fromisoformat(str(current["heartbeat_at"])).timestamp() < cutoff:
                    current["state"] = "STALE"
                    current["updated_at"] = utc_now()
                    self.repository.save_worker_job(current)
                    self.repository.append_worker_transition(current["run_id"], "RUNNING", "STALE", reason="heartbeat expired", worker_id=self.worker_id, attempt=int(current["attempt"]))
                    recovered.append(current)
        return recovered
