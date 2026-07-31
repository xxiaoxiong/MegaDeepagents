"""ParallelTeamScheduler â€” åŸºäº asyncio + TaskBoard + AgentRegistry çš„çœŸå®å¹¶è¡Œè°ƒåº¦ã€‚

ç”Ÿäº§å¹¶è¡Œè°ƒåº¦è¯­ä¹‰ï¼š
- ä¸å†ç”¨é¡ºåº for å¾ªç¯éå† ready_tasksï¼›æ”¹ç”¨ asyncio åç¨‹æ± å¹¶è¡Œæ‰§è¡Œ
- TaskBoard æä¾›åŸå­è®¤é¢†ï¼Œå¤š Agent å¯åŒæ—¶æŠ¢ä»»åŠ¡
- AgentRegistry æä¾› Agent ç”Ÿå‘½å‘¨æœŸ + å¿ƒè·³ï¼Œè°ƒåº¦å™¨ä»ç©ºé—²æ± å­é‡ŒæŒ‘ worker
- å¤±è´¥çš„ task é€šè¿‡ board.fail() è‡ªåŠ¨é‡è¯•åˆ° max_attempts
- æŒç»­å·¥ä½œç›´åˆ° all_succeeded æˆ– max_rounds åˆ°è¾¾

è®¾è®¡åŸåˆ™ï¼š
- ä¸ç°æœ‰ _run_sync_fallback å¹¶å­˜ï¼š
  - TASK_TEAM é»˜è®¤èµ° ParallelTeamSchedulerï¼ˆasyncï¼‰
  - ä¸åŒ…å«æ—è·¯åŒæ­¥è°ƒåº¦å™¨
- ä¼˜å…ˆä¿è¯ LLM å·¥å…·åœºæ™¯çš„ååï¼šæ— ç›¸äº’ä¾èµ–çš„ task å¹¶è¡Œæ‰§è¡Œ
- å• task å¤±è´¥ä¸é˜»å¡å…¶ä»– task
- è°ƒåº¦å™¨å’Œ AgentRegistry é€šè¿‡å¿ƒè·³äº’é”ï¼šè¶…æ—¶çš„ Agent è¢«å›æ”¶ï¼Œå…¶ä»»åŠ¡ç”± timeout
  å¤„ç†ç¨‹åº release å› PENDING ç»™å…¶ä»– Agent
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger
from app.multiagent.agent_registry import AgentRegistry, get_agent_registry
from app.multiagent.task_board import (
    BoardTask,
    BoardTaskStatus,
    ClaimResult,
    TaskBoard,
    get_task_board,
)
from app.multiagent.task_graph import capability_timeout
from app.runtime.reliability import RetryDecision, RetryPolicy


class ScheduleStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"


@dataclass
class ParallelRunResult:
    """å¹¶è¡Œè°ƒåº¦çš„æ•´ä½“ç»“æœã€‚"""
    status: str  # ScheduleStatus value; kept as str for API compatibility
    rounds: int
    total_tasks: int
    succeeded: int
    failed: int
    error: str | None = None
    summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "total_tasks": self.total_tasks,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "error": self.error,
            "summary": self.summary,
        }


class ParallelTeamScheduler:
    """çœŸæ­£çš„å¹¶è¡Œå›¢é˜Ÿè°ƒåº¦å™¨ã€‚

    æµç¨‹ï¼ˆæ¯ä¸ª roundï¼‰ï¼š
        1. é€šè¿‡ board.list_pending() æ‹¿å¯è®¤é¢†ä»»åŠ¡ï¼ˆä¾èµ–å·²æ»¡è¶³ + capability åŒ¹é…ï¼‰
        2. ç»™æ¯ä¸ª task åœ¨ asyncio.gather ä¸­å¹¶è¡Œè°ƒåº¦ï¼š
            - ä» AgentRegistry å–ç©ºé—² Agent
            - atomic claim
            - è®¾ RUNNING
            - äº¤ç»™ executor æ‰§è¡Œ
            - complete / fail
        3. round ç»“æŸååˆ¤æ–­ all_succeeded / max_rounds
    """

    def __init__(
        self,
        run_id: str,
        max_rounds: int = 30,
        max_concurrency: int = 4,
        heartbeat_interval_seconds: float = 3.0,
        lease_timeout_seconds: int = 120,
        task_graph: Any | None = None,
        cancel_event: Any | None = None,
        verifier: Any | None = None,
        worktree_manager: Any | None = None,
        integration_manager: Any | None = None,
        control_plane: Any | None = None,
        permission_broker: Any | None = None,
        task_execution_timeout_seconds: float | None = None,
        retry_policy: RetryPolicy | None = None,
        audit_heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self.run_id = run_id
        self.max_rounds = max_rounds
        self.max_concurrency = max_concurrency
        self.heartbeat_interval = heartbeat_interval_seconds
        self.lease_timeout = lease_timeout_seconds
        self.task_execution_timeout = (
            settings.task_execution_timeout_seconds
            if task_execution_timeout_seconds is None
            else max(0.0, float(task_execution_timeout_seconds))
        )
        self.retry_policy = retry_policy or RetryPolicy(
            base_delay_seconds=settings.retry_base_delay_seconds,
            max_delay_seconds=settings.retry_max_delay_seconds,
            rate_limit_base_delay_seconds=settings.retry_rate_limit_base_delay_seconds,
            rate_limit_max_delay_seconds=settings.retry_rate_limit_max_delay_seconds,
        )
        self.audit_heartbeat_interval = (
            settings.audit_heartbeat_interval_seconds
            if audit_heartbeat_interval_seconds is None
            else max(1.0, float(audit_heartbeat_interval_seconds))
        )
        self.task_graph = task_graph
        self.cancel_event = cancel_event or asyncio.Event()
        self.verifier = verifier
        self.worktree_manager = worktree_manager
        self.integration_manager = integration_manager

        self.board = get_task_board()
        self.registry = get_agent_registry()
        from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
        self.runtime_manager = get_agent_runtime_manager()
        if control_plane is None:
            from app.multiagent.control_plane import TeamControlPlaneService
            control_plane = TeamControlPlaneService()
        self.control_plane = control_plane
        if permission_broker is None:
            from app.multiagent.permission import get_permission_broker
            permission_broker = get_permission_broker()
        self.permission_broker = permission_broker
        self._dispatch_agent_hints: dict[str, str] = {}

    # ===== ä¸»å¾ªç¯ =====

    def _deps_satisfied(self, task: BoardTask) -> bool:
        """Return True only when every dependency of ``task`` is SUCCEEDED.

        ``TaskBoard.list_pending`` returns every PENDING task regardless of
        dependency state.  Dispatching a task whose dependencies are not
        SUCCEEDED is wasteful and dangerous: ``board.claim`` rejects it with
        ``dependency_not_succeeded``, the reserved agent is released
        instantly, and the next round repeats the same dance.  With
        ``max_rounds`` budget this busy-loop burns the entire round budget
        in milliseconds (observed: 80 rounds in 38 ms) and aborts the run
        with ``max_rounds`` before any retry backoff expires.
        """
        for dep_id in task.dependencies:
            dep = self.board.get(dep_id, run_id=self.run_id)
            if dep is None or dep.status != BoardTaskStatus.SUCCEEDED:
                return False
        return True

    async def _run_wide_heartbeat(self, stop_event: asyncio.Event) -> None:
        """Heartbeat every live agent in the run, including IDLE ones.

        Without this loop, only the currently-executing task heartbeats its
        own agent.  IDLE teammates sit with whatever ``last_heartbeat_at``
        they had at registration, so once ``lease_timeout`` elapses
        ``cleanup_expired`` marks them FAILED â€” killing the whole team
        during any long-running task.  This loop keeps IDLE teammates
        alive while the run is active, and still lets ``cleanup_expired``
        reap truly crashed agents (their per-task heartbeat stops, but the
        scheduler-level heartbeat below is intentionally best-effort and
        skips agents whose status is already terminal).
        """
        from app.multiagent.agent_instance import AgentStatus
        while not stop_event.is_set():
            for agent in self.registry.list_by_run(self.run_id):
                status = getattr(agent.status, "value", agent.status)
                if status in {"stopped", "failed"}:
                    continue
                # Only heartbeat IDLE agents here.  RUNNING agents are
                # heartbeated by their per-task ``_heartbeat_loop``; echoing
                # them from the scheduler would mask a stuck executor thread
                # and defeat the lease-based crash detection.
                if status == AgentStatus.IDLE.value:
                    self.registry.heartbeat(agent.agent_id)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.heartbeat_interval
                )
            except TimeoutError:
                pass

    async def run(self, executor: Any) -> ParallelRunResult:
        """æ‰§è¡Œå¹¶è¡Œè°ƒåº¦ã€‚executor å¿…é¡»å®ç° execute_task(dag, task_id, task_input)ã€‚"""
        round_n = 0
        self._event("SchedulerStarted", payload={
            "max_rounds": self.max_rounds,
            "max_concurrency": self.max_concurrency,
            "task_execution_timeout_seconds": self.task_execution_timeout,
        })

        # Run-wide heartbeat keeps IDLE teammates alive across long tasks.
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._run_wide_heartbeat(heartbeat_stop))

        try:
            return await self._run_loop(executor, round_n)
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _run_loop(self, executor: Any, round_n: int) -> ParallelRunResult:
        """Event-driven dispatch loop.

        The previous implementation dispatched a batch of tasks then
        ``await asyncio.gather(*coros)`` â€” waiting for **every** task in the
        batch to finish before re-evaluating the pending queue.  A single
        long-running task (e.g. a 15-minute planning task that eventually
        timed out) blocked re-dispatch of siblings that had already failed
        and were due for retry.  Observed in run_207f813863a04c39: T2
        failed with a 429 at t+6m, its retry became due 2s later, but the
        scheduler could not re-claim it until T1 timed out at t+15m â€” by
        which point the researcher agent had been reaped by lease expiry.

        This version dispatches new tasks as soon as a concurrency slot
        frees up, using ``asyncio.wait(return_when=FIRST_COMPLETED)`` so a
        slow task never blocks re-dispatch of ready retries.
        """
        from app.infrastructure.database.run_store import get_agent_run_history

        # One semaphore for the whole run so concurrency is honoured across
        # batches, not just within a single gather.
        semaphore = asyncio.Semaphore(self.max_concurrency)
        # future â†’ task_id for the tasks currently dispatched.
        running: dict[asyncio.Future, str] = {}

        while round_n < self.max_rounds:
            self._refresh_task_graph()
            run_record = get_agent_run_history().get_team_run(self.run_id)
            if run_record and run_record.get("status") == "paused":
                await self._cancel_running(running)
                return self._finalize(round_n, status=ScheduleStatus.PAUSED.value,
                                      error="paused")

            if self.cancel_event.is_set():
                await self._cancel_running(running)
                self.board.cancel_run(self.run_id)
                return self._finalize(round_n, status=ScheduleStatus.CANCELLED.value,
                                      error="cancelled")

            # Reap truly crashed agents (stale heartbeat while
            # RUNNING/CLAIMING).  IDLE agents are kept alive by
            # ``_run_wide_heartbeat`` and will not be reaped.
            self.registry.cleanup_expired()

            # Discover newly-dispatchable tasks (deps satisfied + capability
            # match + not already running).
            new_dispatch = self._discover_dispatchable(running)

            if new_dispatch:
                round_n += 1
                self._event("SchedulerRoundStarted", payload={
                    "round": round_n,
                    "pending_task_ids": [t.task_id for t in new_dispatch],
                    "task_count": len(new_dispatch),
                })
                for task in new_dispatch:
                    fut = asyncio.ensure_future(
                        self._run_one_guarded(task, executor, semaphore)
                    )
                    running[fut] = task.task_id

            # Nothing running and nothing to dispatch â†’ resolve the wait.
            if not running:
                resolved = await self._resolve_idle(round_n)
                if resolved is not None:
                    return resolved
                # _resolve_idle either returned a final result or slept for a
                # deferred retry; loop back to re-evaluate.
                continue

            # Wait for at least one in-flight task to finish, then loop to
            # dispatch newly-ready tasks immediately.  This is the key
            # difference from the old gather(): a slow task no longer blocks
            # re-dispatch of ready retries.
            #
            # BUT: a bare ``FIRST_COMPLETED`` wait with no timeout also blocks
            # re-dispatch of *retry-ready* tasks.  If task A (15-min planning)
            # is running and task B failed with a 15-s backoff, B becomes due
            # while A is still running â€” but the scheduler won't notice until
            # A finishes.  In run_e8587ea68ac64ff5, T2's retry sat for 10 min
            # behind T1's 900-s run.  Cap the wait at the next deferred retry's
            # due time so the loop wakes up and re-evaluates ``list_pending``.
            retry_wait = self._next_retry_delay()
            # Always wake periodically to observe run cancellation/pause.
            # ``None`` used to block until a non-cooperative model call
            # finished (potentially many minutes), even after the operator
            # had cancelled the run. One lightweight control-plane read per
            # second keeps cancellation bounded without redispatching work.
            wait_timeout = min(retry_wait, 1.0) if retry_wait is not None else 1.0
            done, _ = await asyncio.wait(
                running.keys(),
                return_when=asyncio.FIRST_COMPLETED,
                timeout=wait_timeout,
            )
            if not done:
                # The wait timed out without any task finishing â€” a deferred
                # retry is likely due now.  Loop back to re-dispatch it
                # without consuming a round.
                continue
            for fut in done:
                running.pop(fut, None)
                exc = fut.exception()
                if exc is not None:
                    logger.error(
                        f"[ParallelSched] run={self.run_id} task raised: {exc}"
                    )

            if self.cancel_event.is_set():
                await self._cancel_running(running)
                self.board.cancel_run(self.run_id)
                return self._finalize(round_n, status=ScheduleStatus.CANCELLED.value,
                                      error="cancelled")

            run_record = get_agent_run_history().get_team_run(self.run_id)
        Û]4îÚ$z{-®éÜj×ç'Våö–BÒ6VÆbç'Våö–C ¢&—6R'VçF–ÖTW'&÷"†b&'F–f7E÷w&öæu÷'Vã§¶'F–f7Eö–GÒ"¢7FGW2ÒvWFGG"†'F–f7Bç7FGW2Â'fÇVR"Â'F–f7Bç7FGW2¢–b&WV—&U÷fW&–f–VBæB7FGW2Ò'fW&–f–VB# ¢&—6R'VçF–ÖTW'&÷"†b&'F–f7Eöæ÷E÷fW&–f–VC§¶'F–f7Eö–GÒ"¢–bæ÷B7F÷&RçfW&–g•ö–çFVw&—G’†'F–f7Eö–B“ ¢&—6R'VçF–ÖTW'&÷"†b&'F–f7Eö–çFVw&—G•öf–ÆVC§¶'F–f7Eö–GÒ"¢6VVâæFB†'F–f7Eö–B¢–G2æVæB†'F–f7Eö–B¢&Vg2æVæB‡°¢&'F–f7Eö–B#¢'F–f7Bæ–BÀ¢'F6µö–B#¢'F–f7BçF6µö–BÀ¢'&öGV6–æuövVçEö–B#¢'F–f7Bç&öGV6VEö'’À¢'G—R#¢'F–f7BçG—RçfÇVRÀ¢'F‚#¢'F–f7BçF‚À¢&6öçFVçEö†6‚#¢'F–f7Bæ6öçFVçEö†6‚À¢'fW'6–öâ#¢'F–f7BçfW'6–öâÀ¢&6öÖÖ—E÷6†#¢'F–f7Bæ6öÖÖ—E÷6†÷"'F–f7BæÖWFFFævWB‚&6öÖÖ—E÷6†"’À¢'fW&–f–6F–öå÷7FFR#¢7FGW2À¢'W'÷6R#¢W'÷6RÀ¢&7&VFVEöB#¢'F–f7Bæ7&VFVEöBæ—6öf÷&ÖB‚’À¢'7VÖÖ'’#¢'F–f7BæÖWFFFævWB‚'7VÖÖ'’"Â""’À¢Ò ¢f÷"FWVæFVæ7•ö–B–âF6²æFWVæFVæ6–W3 ¢FWVæFVæ7’Ò6VÆbæ&ö&BævWB†FWVæFVæ7•ö–BÂ'Våö–C×6VÆbç'Våö–B¢–bFWVæFVæ7’—2æöæR÷"FWVæFVæ7’ç7FGW2Ò&ö&EF6µ7FGW2å5T44TTDTC ¢&—6R'VçF–ÖTW'&÷"†b&FWVæFVæ7•öæ÷E÷fW&–f–VC§¶FWVæFVæ7•ö–GÒ"¢f÷"'F–f7Eö–B–âFWVæFVæ7’ç&öGV6VEö'F–f7Eö–G3 ¢VæEö'F–f7B€¢'F–f7Eö–BÀ¢W'÷6SÒ&FWVæFVæ7’"À¢&WV—&U÷fW&–f–VCÕG'VRÀ¢¢f÷"'F–f7Eö–B–âF6²æÖWFFFævWB‚'6÷W&6Uö'F–f7Eö–G2"ÂµÒ“ ¢VæEö'F–f7B€¢7G"†'F–f7Eö–B’À¢W'÷6SÒ'&W—%÷6÷W&6R"À¢&WV—&U÷fW&–f–VCÔfÇ6RÀ¢¢&WGW&â–G2Â&Vg0 ¢2ÓÓÓÓÒ[z^X[rÓÓÓÓĞ ¢FVböWfVçB€¢6VÆbÀ¢WfVçE÷G—S¢7G"À¢¢À¢–ÆöC¢F–7E·7G"Âç•ÒÂæöæRÒæöæRÀ¢vVçEö–C¢7G"ÂæöæRÒæöæRÀ¢F6µö–C¢7G"ÂæöæRÒæöæRÀ¢’ÓâæöæS ¢g&öÒæ–æg&7G'V7GW&RæFF&6Rç'Vå÷7F÷&R–×÷'B€¢vWEövVçE÷'Våö†—7F÷'’À¢Ö¶U÷'VåöWfVçEö–BÀ¢ ¢vWEövVçE÷'Våö†—7F÷'’‚’ç&V6÷&EöWfVçB€¢WfVçEö–CÖÖ¶U÷'VåöWfVçEö–B‚’À¢'Våö–C×6VÆbç'Våö–BÀ¢WfVçE÷G—SÖWfVçE÷G—RÀ¢vVçEö–CÖvVçEö–BÀ¢F6µö–C×F6µö–BÀ¢F–ÖW7F×ÖFFWF–ÖRææ÷r…UD2’À¢–ÆöC×–ÆöB÷"·ÒÀ¢ ¢FVböf–æÆ—¦R€¢6VÆbÂ&÷VæG3¢–çBÂ7FGW3¢7G"ÂW'&÷#¢7G"ÂæöæRÒæöæRÀ¢’Óâ&ÆÆVÅ'Vå&W7VÇC ¢7VÖÖ&—¦RÒ6VÆbæ&ö&Bç7VÖÖ'’‡6VÆbç'Våö–B¢&W7VÇBÒ&ÆÆVÅ'Vå&W7VÇB€¢7FGW3×7FGW2À¢&÷VæG3×&÷VæG2À¢F÷FÅ÷F6·3×7VÖÖ&—¦RævWB‚'F÷FÂ"Â’À¢266†VGVÆW"6ö×ÆWF–öâ6÷VçG2&öGV6VBF6·3²f–æÂfW&–f–V@¢26ö×ÆWF–öâ&VÖ–ç2f—6–&ÆR6W&FVÇ’–â7VÖÖ'–à¢7V66VVFVC×7VÖÖ&—¦RævWB„&ö&EF6µ7FGW2å5T44TTDTBçfÇVRÂ’À¢f–ÆVC×7VÖÖ&—¦RævWB„&ö&EF6µ7FGW2äd”ÄTBçfÇVRÂ’À¢W'&÷#ÖW'&÷"À¢7VÖÖ'“×7VÖÖ&—¦RÀ¢¢6VÆbåöWfVçB‚%66†VGVÆW%7F÷VB"Â–ÆöC×&W7VÇBçFõöF–7B‚’¢&WGW&â&W7VÇ@ ¢FVböf–æÆ—¦U÷fW&–f–VE÷'Vâ‡6VÆbÂ&÷VæG3¢–çB’Óâ&ÆÆVÅ'Vå&W7VÇC ¢""$Ç’'VâÖÆWfVÂvFW2gFW"WfW'’F6²—2fW&–f–W"Ö÷væVB5T44TTDTBâ"" ¢VæF–æu÷W&Ö—76–öç2Ò6VÆbçW&Ö—76–öåö'&ö¶W"æÆ—7E÷VæF–ær‡6VÆbç'Våö–B¢–bVæF–æu÷W&Ö—76–öç3 ¢&WGW&â6VÆbåöf–æÆ—¦R‡&÷VæG2Â66†VGVÆU7FGW2åt•D”äuô…TÔâçfÇVRÀ¢'VæF–æuö†–v…÷&—6µ÷W&Ö—76–öç2"¢–bç’‡F6²æÖWFFFævWB‚&ÖW&vUö6öæfÆ–7G2"¢f÷"F6²–â6VÆbæ&ö&BæÆ—7Eö'•÷'Vâ‡6VÆbç'Våö–B’“ ¢&WGW&â6VÆbåöf–æÆ—¦R‡&÷VæG2Â66†VGVÆU7FGW2äd”ÄTBçfÇVRÀ¢'Vç&W6öÇfVEöÖW&vUö6öæfÆ–7G2"¢–b6VÆbæ–çFVw&F–öåöÖævW"—2æ÷BæöæS ¢&ö÷BÒ6VÆbçF6µöw&‚ææöFW2ævWB‡6VÆbçF6µöw&‚ç&ö÷E÷F6µö–B’–b6VÆbçF6µöw&‚VÇ6RæöæP¢ÆâÒ6VÆbåö–çFVw&F–öå÷fW&–f–6F–öå÷Æâ‡&ö÷B¢f÷"6†V6²–âÆâæ6†V6·3 ¢–ÆöBÒ6†V6²æö'6W'f&ÆU÷–ÆöB‚¢6VÆbåöWfVçB‚$–çFVw&F–öåfW&–f–6F–öå7F'FVB"Â–ÆöC×–ÆöB¢G'“ ¢&W7VÇBÒ6VÆbæ–çFVw&F–öåöÖævW"çfW&–g•ö6†V6²†6†V6²¢W†6WBW†6WF–öâ2W†3 ¢6VÆbåöWfVçB‚$–çFVw&F–öåfW&–f–6F–öåVæf–Æ&ÆR"Â–ÆöC×°¢¢§–ÆöBÀ¢&W'&÷"#¢7G"†W†2’À¢Ò¢&WGW&â6VÆbåöf–æÆ—¦R€¢&÷VæG2À¢66†VGVÆU7FGW2åt•D”äuô…TÔâçfÇVRÀ¢&–çFVw&F–öå÷fW&–f–6F–öå÷Væf–Æ&ÆR"À¢¢6VÆbåöWfVçB‚$–çFVw&F–öåfW&–f–6F–öä6ö×ÆWFVB"Â–ÆöC×°¢¢§–ÆöBÀ¢'&WGW&æ6öFR#¢&W7VÇBç&WGW&æ6öFRÀ¢&6æ6VÆÆVB#¢&W7VÇBæ6æ6VÆÆVBÀ¢'F–ÖVEö÷WB#¢&W7VÇBçF–ÖVEö÷WBÀ¢&GW&F–öå÷6V6öæG2#¢&W7VÇBæGW&F–öå÷6V6öæG2À¢'7FF÷WE÷&Wf–Wr#¢&W7VÇBç7FF÷WE²Ó%ó¥ÒÀ¢'7FFW'%÷&Wf–Wr#¢&W7VÇBç7FFW'%²Ó%ó¥ÒÀ¢Ò¢–b&W7VÇBç&WGW&æ6öFRÒ÷"&W7VÇBæ6æ6VÆÆVB÷"&W7VÇBçF–ÖVEö÷WC ¢&WGW&â6VÆbåöf–æÆ—¦R€¢&÷VæG2À¢66†VGVÆU7FGW2äd”ÄTBçfÇVRÀ¢b&–çFVw&F–öå÷fW&–f–6F–öåöf–ÆVC§¶6†V6²æÆ&VÇÒ"À¢¢–bÆâæÖ—76–æu÷&WV—&VÖVçG3 ¢6VÆbåöWfVçB‚$–çFVw&F–öåfW&–f–6F–öåVæf–Æ&ÆR"Â–ÆöC×°¢&Ö—76–æu÷&WV—&VÖVçG2#¢Æ—7B‡ÆâæÖ—76–æu÷&WV—&VÖVçG2’À¢'6÷W&6R#¢Æâç6÷W&6RÀ¢Ò¢&WGW&â6VÆbåöf–æÆ—¦R€¢&÷VæG2À¢66†VGVÆU7FGW2åt•D”äuô…TÔâçfÇVRÀ¢&–çFVw&F–öå÷fW&–f–6F–öå÷&WV—&VÖVçG5öÖ—76–ær"À¢¢&WGW&â6VÆbåöf–æÆ—¦R‡&÷VæG2Â66†VGVÆU7FGW2ä4ôÕÄUDTBçfÇVR ¢FVbö–çFVw&F–öå÷fW&–f–6F–öå÷Æâ‡6VÆbÂ&ö÷C¢ç’’Óâç“ ¢g&öÒæ×VÇF–vVçBæv—E÷v÷&·76R–×÷'B€¢–çFVw&F–öåfW&–f–6F–öä6†V6²À¢–çFVw&F–öåfW&–f–6F–öåÆâÀ¢ ¢ÖWFFFÒ&ö÷BæÖWFFF–b&ö÷B—2æ÷BæöæRVÇ6R·Ğ¢6öæf–wW&VBÒÖWFFFævWB‚&–çFVw&F–öå÷FW7Eö6öÖÖæG2"¢–b6öæf–wW&VB—2æöæRæBÖWFFFævWB‚&–çFVw&F–öå÷FW7Eö&wb"“ ¢6öæf–wW&VBÒ¶ÖWFFF²&–çFVw&F–öå÷FW7Eö&wb%ÕĞ¢–b6öæf–wW&VB—2æöæS ¢&WGW&â6VÆbæ–çFVw&F–öåöÖævW"æF—66÷fW%÷fW&–f–6F–öå÷Æâ‚¢–bæ÷B—6–ç7Fæ6R†6öæf–wW&VBÂÆ—7B“ ¢&WGW&â–çFVw&F–öåfW&–f–6F–öåÆâ€¢Ö—76–æu÷&WV—&VÖVçG3Ò‚&–çfÆ–Eö–çFVw&F–öå÷FW7Eö6öÖÖæG2"Â’À¢6÷W&6SÒ&6öæf–wW&VB"À¢¢6†V6·3¢Æ—7E´–çFVw&F–öåfW&–f–6F–öä6†V6µÒÒµĞ¢Ö—76–æs¢Æ—7E·7G%ÒÒµĞ¢f÷"–æFW‚Â—FVÒ–âVçVÖW&FR†6öæf–wW&VB“ ¢–b—6–ç7Fæ6R†—FVÒÂÆ—7B“ ¢&wbÒ—FVĞ¢Æ&VÂÒb$6öæf–wW&VB6†V6²¶–æFW‚²Ò ¢7vBÒ"â ¢F–ÖV÷WBÒ3ã ¢VÆ–b—6–ç7Fæ6R†—FVÒÂF–7B“ ¢&wbÒ—FVÒævWB‚&&wb"¢Æ&VÂÒ7G"†—FVÒævWB‚&Æ&VÂ"’÷"b$6öæf–wW&VB6†V6²¶–æFW‚²Ò"¢7vBÒ7G"†—FVÒævWB‚&7vB"’÷""â"¢G'“ ¢F–ÖV÷WBÒfÆöB†—FVÒævWB‚'F–ÖV÷WE÷6V6öæG2"’÷"3ã¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢Ö—76–æræVæB€¢b&6öæf–wW&VEö6†V6µ÷¶–æFW‚²Ó¦–çfÆ–E÷F–ÖV÷WB ¢¢6öçF–çVP¢VÇ6S ¢Ö—76–æræVæB†b&6öæf–wW&VEö6†V6µ÷¶–æFW‚²Ó¦–çfÆ–E÷6†R"¢6öçF–çVP¢&VÆF—fUö7vBÒF‚†7vBç&WÆ6R‚%ÅÂ"Â"ò"’¢–b€¢ÆVâ†7vB’âS ¢÷"&VÆF—fUö7vBæ—5ö'6öÇWFR‚¢÷""ââ"–â&VÆF—fUö7vBç'G0¢÷"€¢&VÆF—fUö7vBç'G0¢æB#¢"–â&VÆF—fUö7vBç'G5³Ğ¢¢“ ¢Ö—76–æræVæB†b&6öæf–wW&VEö6†V6µ÷¶–æFW‚²Ó§Vç6fUö7vB"¢6öçF–çVP¢–bæ÷BÖF‚æ—6f–æ—FR‡F–ÖV÷WB“ ¢Ö—76–æræVæB†b&6öæf–wW&VEö6†V6µ÷¶–æFW‚²Ó¦–çfÆ–E÷F–ÖV÷WB"¢6öçF–çVP¢–b€¢æ÷B—6–ç7Fæ6R†&wbÂÆ—7B¢÷"æ÷B&w`¢÷"æ÷BÆÂ†—6–ç7Fæ6R‡'BÂ7G"’æB'Bf÷"'B–â&wb¢“ ¢Ö—76–æræVæB†b&6öæf–wW&VEö6†V6µ÷¶–æFW‚²Ó¦–çfÆ–Eö&wb"¢6öçF–çVP¢–bæ÷B6VÆbå÷6fUö–çFVw&F–öåö&wb†&wb“ ¢Ö—76–æræVæB†b&6öæf–wW&VEö6†V6µ÷¶–æFW‚²Ó§Vç6fUö&wb"¢6öçF–çVP¢FWVæFVæ7•÷6÷W&6RÒæöæP¢W†V7WF&ÆRÒ6VÆbåö–çFVw&F–öåöW†V7WF&ÆR†&we³Ò¢FWVæFVæ7•÷&W6öÇfW"ÒvWFGG"€¢6VÆbæ–çFVw&F–öåöÖævW"À¢&æöFUöFWVæFVæ7•÷6÷W&6R"À¢æöæRÀ¢¢–b€¢W†V7WF&ÆR–â²&çÒ"Â'çÒ"Â'–&â'Ğ¢æB6ÆÆ&ÆR†FWVæFVæ7•÷&W6öÇfW"¢“ ¢FWVæFVæ7•÷6÷W&6RÒFWVæFVæ7•÷&W6öÇfW"€¢&VÆF—fUö7vBæ5÷÷6—‚‚¢¢–bFWVæFVæ7•÷6÷W&6R—2æöæS ¢Ö—76–æræVæB€¢b&6öæf–wW&VEö6†V6µ÷¶–æFW‚²Ó¢ ¢&æöFUöFWVæFVæ6–W5öÖ—76–ær ¢¢6öçF–çVP¢6†V6·2æVæB„–çFVw&F–öåfW&–f–6F–öä6†V6²€¢Æ&VÃÖÆ&VÂÀ¢&wc×GWÆR†&wb’À¢7vE÷&VÆF—fS×&VÆF—fUö7vBæ5÷÷6—‚‚’À¢F–ÖV÷WE÷6V6öæG3ÖÖ‚ƒãÂÖ–â‡F–ÖV÷WBÂ5ócã’’À¢FWVæFVæ7•÷6÷W&6SÒ€¢7G"†FWVæFVæ7•÷6÷W&6R¢–bFWVæFVæ7•÷6÷W&6R—2æ÷BæöæP¢VÇ6RæöæP¢’À¢’¢–bæ÷B6†V6·2æBæ÷BÖ—76–æs ¢Ö—76–æræVæB‚&6öæf–wW&VEö–çFVw&F–öåö6†V6·5öV×G’"¢&WGW&â–çFVw&F–öåfW&–f–6F–öåÆâ€¢6†V6·3×GWÆR†6†V6·2’À¢Ö—76–æu÷&WV—&VÖVçG3×GWÆR†Ö—76–ær’À¢6÷W&6SÒ&6öæf–wW&VB"À¢ ¢7FF–6ÖWF†ö@¢FVbö–çFVw&F–öåöW†V7WF&ÆR‡fÇVS¢7G"’Óâ7G# ¢W†V7WF&ÆRÒfÇVRç&WÆ6R‚%ÅÂ"Â"ò"’ç'7Æ—B‚"ò"Â•²ÓÒæÆ÷vW"‚¢f÷"7Vff—‚–â‚"æW†R"Â"æ6ÖB"Â"æ&B"“ ¢W†V7WF&ÆRÒW†V7WF&ÆRç&VÖ÷fW7Vff—‚‡7Vff—‚¢&WGW&âW†V7WF&ÆP ¢6Æ76ÖWF†ö@¢FVb÷6fUö–çFVw&F–öåö&wb†6Ç2Â&wc¢Æ—7E·7G%Ò’Óâ&ööÃ ¢W†V7WF&ÆRÒ6Ç2åö–çFVw&F–öåöW†V7WF&ÆR†&we³Ò¢&w2Ò·'BæÆ÷vW"‚’f÷"'B–â&we³¥ÕĞ¢–bW†V7WF&ÆR–â²'—FW7B'Ó ¢&WGW&âG'VP¢–bW†V7WF&ÆR–â²'—F†öâ"Â'—F†öã2'Ó ¢&WGW&âÆVâ†&w2’ãÒ"æB&w5³ÒÓÒ"ÖÒ"æB&w5³Ò–â°¢'—FW7B"À¢'Væ—GFW7B"À¢&6ö×–ÆVÆÂ"À¢Ğ¢–bW†V7WF&ÆR–â²&çÒ"Â'çÒ"Â'–&â'Ó ¢67&—G2Ò²'FW7B"Â&'V–ÆB"Â&Æ–çB"Â'G—V6†V6²"Â&6†V6²'Ğ¢&WGW&â&ööÂ†&w2’æB€¢&w5³Ò–â67&—G0¢÷"ÆVâ†&w2’ãÒ"æB&w5³ÒÓÒ''Vâ"æB&w5³Ò–â67&—G0¢¢–bW†V7WF&ÆRÓÒ&6&vò# ¢&WGW&â&ööÂ†&w2’æB&w5³Ò–â²'FW7B"Â&6†V6²"Â&6Æ—’'Ğ¢–bW†V7WF&ÆRÓÒ&vò# ¢&WGW&â&ööÂ†&w2’æB&w5³ÒÓÒ'FW7B ¢–bW†V7WF&ÆR–â²&×fâ"Â&w&FÆR"Â&w&FÆWr'Ó ¢&WGW&âç’†&r–â²'FW7B"Â&6†V6²"Â'fW&–g’'Òf÷"&r–â&w2¢–bW†V7WF&ÆRÓÒ&Ö¶R# ¢&WGW&â&ööÂ†&w2’æB&w5³Ò–â²'FW7B"Â&6†V6²"Â'fW&–g’"Â&Æ–çB'Ğ¢&WGW&âfÇ6P ¢2ÓÓÓÓÒK»¾XªiÛşKˆâDrYÎjÚRÓÓÓÓĞ ¢6Æ76ÖWF†ö@¢FVb7–æ5ög&öÕ÷F6µöw&‚€¢6Ç2ÂFs¢ç’Â&ö&C¢F6´&ö&BÂ'Våö–C¢7G"À¢’ÓâæöæS ¢"".h¨¢F6´w&‚y¨Nˆ¨.x+YÎjÚ^X‹F6´&ö&NûÈK¸^YÎjÚRTäD”ärˆ¨.x+ûÈ8  ¢YÊ[›nŠÎ‹>[ªn[ÈZx¾X˜Ş‹>yJûÈÎŠê’&ö&EF6²KˆâF6´æöFR£Zû[©N8 ¢"" ¢f÷"æöFUö–BÂæöFR–âFrææöFW2æ—FV×2‚“ ¢W†—7F–ærÒ&ö&BævWB†æöFUö–BÂ'Våö–C×'Våö–B¢–bW†—7F–ær—2æ÷BæöæS ¢6öçF–çVP¢&ö&Bæ7&VFU÷F6²€¢F6µö–CÖæöFUö–BÀ¢'Våö–C×'Våö–BÀ¢F—FÆSÖæöFRçF—FÆR÷"æöFUö–BÀ¢ö&¦V7F—fSÖæöFRæö&¦V7F—fRÀ¢FWVæFVæ6–W3ÖÆ—7B†æöFRæFWVæFVæ6–W2’À¢&WV—&VEö6&–Æ—F–W3ÖÆ—7B†æöFRç&WV—&VEö6&–Æ—F–W2’À¢&–÷&—G“ÖvWFGG"†æöFRÂ'&–÷&—G’"Â’À¢Ö…öGFV×G3ÖvWFGG"†æöFRÂ&Ö…öGFV×G2"Â2’À¢ ¢7FF–6ÖWF†ö@¢FVb7–æ5ö&6µ÷FõöFr†Fs¢ç’Â&ö&C¢F6´&ö&BÂ'Våö–C¢7G"’ÓâæöæS ¢"".h¨¢&ö&EF6²y¨NiÈ{¸x«nhY¹îXiX‹F6´w&8  ¢‹[Yk9^‹ÚÎhÚ.™;îûÉ¥TäD”är(i"$TE’(i"%Tää”är(i"5T44TTDTBôd”ÄTN8 ¢"" ¢g&öÒæ×VÇF–vVçBçF6µöw&‚–×÷'BF6´æöFU7FGW0¢f÷"B–â&ö&BæÆ—7Eö'•÷'Vâ‡'Våö–B“ ¢æöFRÒFrææöFW2ævWB‡BçF6µö–B¢–bæöFR—2æöæS ¢6öçF–çVP¢F&vWBÒæöæP¢–bBç7FGW2ÓÒ&ö&EF6µ7FGW2å5T44TTDTC ¢F&vWBÒF6´æöFU7FGW2å5T44TTDT@¢VÆ–bBç7FGW2ÓÒ&ö&EF6µ7FGW2äd”ÄTC ¢F&vWBÒF6´æöFU7FGW2äd”ÄT@¢VÆ–bBç7FGW2–â„&ö&EF6µ7FGW2å%Tää”ärÂ&ö&EF6µ7FGW2ä4Ä”ÔTB“ ¢F&vWBÒF6´æöFU7FGW2å%Tää”äp¢VÇ6S ¢6öçF–çVP¢2yJ‚÷7FW÷Fòhê‹ù¾X‹F&vW@¢÷7FW÷Fò†FrÂBçF6µö–BÂF&vWB¢f÷"'B–âBç&öGV6VEö'F–f7Eö–G3 ¢æöFRÒFrææöFW2ævWB‡BçF6µö–B¢–b'Bæ÷B–âæöFRæ÷WGWEö'F–f7Eö–G3 ¢Fræ66WEö'F–f7B‡BçF6µö–BÂ'B  ¦FVb÷7FW÷Fò†Fs¢ç’ÂæöFUö–C¢7G"ÂF&vWC¢ç’’ÓâæöæS ¢"".hÈYk9^‹ÚÎhÚ.™;îhê‹ù¾ˆ¨.x+x«nhX‹F&vWN8  ¢™;îûÉ¥TäD”är(i"$TE’(i"%Tää”är(i"5T44TTDTBôd”ÄT@¢"" ¢g&öÒæ×VÇF–vVçBçF6µöw&‚–×÷'BF6´æöFU7FGW0¢æöFRÒFrææöFW2ævWB†æöFUö–B¢–bæöFR—2æöæR÷"æöFRç7FGW2ÓÒF&vWC ¢&WGW&à¢2TäD”är(i"$TE¢–bæöFRç7FGW2ÓÒF6´æöFU7FGW2åTäD”äs ¢FrçWFFU÷7FGW2†æöFUö–BÂF6´æöFU7FGW2å$TE’¢2$TE’(i"%Tää”äp¢æöFRÒFrææöFW2ævWB†æöFUö–B¢–bæöFRç7FGW2ÓÒF6´æöFU7FGW2å$TE’æBF&vWBÒF6´æöFU7FGW2å$TE“ ¢FrçWFFU÷7FGW2†æöFUö–BÂF6´æöFU7FGW2å%Tää”är¢æöFRÒFrææöFW2ævWB†æöFUö–B¢2%Tää”är(i"F&vWB…5T44TTDTBôd”ÄTB¢–bæöFRç7FGW2ÓÒF6´æöFU7FGW2å%Tää”äræBF&vWB–â€¢F6´æöFU7FGW2å5T44TTDTBÂF6´æöFU7FGW2äd”ÄT@¢“ ¢FrçWFFU÷7FGW2†æöFUö–BÂF&vWB