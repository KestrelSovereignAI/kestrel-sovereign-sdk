"""Focused tests for durable operator run-plane contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime

import pytest

from kestrel_sdk.operator import (
    ARTIFACT_READ_ACTION,
    ArtifactAuthorizationAction,
    ArtifactRecord,
    ExecutionTargetReference,
    ExternalEngineJobLink,
    OperatorAuthorizationError,
    OperatorContext,
    RUN_ATTACH_ACTION,
    RUN_CANCEL_ACTION,
    RUN_LAUNCH_ACTION,
    RUN_PAUSE_ACTION,
    RUN_READ_ACTION,
    RUN_RESUME_ACTION,
    RUN_RETRY_ACTION,
    RunAttempt,
    RunConflictError,
    RunControl,
    RunControlAction,
    RunLaunch,
    RunNotFoundError,
    RunPage,
    RunQuery,
    RunRecord,
    RunService,
    RunSource,
    RunStage,
    RunState,
)


NOW = datetime(2026, 8, 10, 12, 5, tzinfo=UTC)


def _context(**overrides: object) -> OperatorContext:
    values: dict[str, object] = {
        "principal_id": "principal-1",
        "tenant_id": "tenant-1",
        "granted_actions": {
            RUN_LAUNCH_ACTION,
            RUN_READ_ACTION,
            RUN_ATTACH_ACTION,
            RUN_PAUSE_ACTION,
            ARTIFACT_READ_ACTION,
        },
        "granted_capabilities": {"build.execute"},
        "permitted_boundary_ids": {"workspace-1"},
        "correlation_id": "request-1",
        "issued_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        "expires_at": datetime(2026, 8, 10, 12, 15, tzinfo=UTC),
    }
    values.update(overrides)
    return OperatorContext(**values)  # type: ignore[arg-type]


def _target(**overrides: object) -> ExecutionTargetReference:
    values = {
        "target_id": "worker-1",
        "boundary_id": "workspace-1",
        "capability": "build.execute",
    }
    values.update(overrides)
    return ExecutionTargetReference(**values)


def _launch(**overrides: object) -> RunLaunch:
    values: dict[str, object] = {
        "run_id": "run-1",
        "kind": "build",
        "source": RunSource.MANUAL,
        "initiated_by": "principal-1",
        "tenant_id": "tenant-1",
        "target": _target(),
        "orchestrator": None,
        "idempotency_key": "launch-request-1",
    }
    values.update(overrides)
    return RunLaunch(**values)  # type: ignore[arg-type]


def _artifact(**overrides: object) -> ArtifactRecord:
    values: dict[str, object] = {
        "artifact_id": "artifact-1",
        "run_id": "run-1",
        "type": "build.report",
        "label": "Build report",
        "media_type": "application/json",
        "href": "/authorized/artifacts/artifact-1",
        "metadata": {"summary": {"passed": True}, "steps": ["lint", "test"]},
    }
    values.update(overrides)
    return ArtifactRecord(**values)  # type: ignore[arg-type]


def test_launch_contains_only_semantic_input_and_record_owns_runtime_clock() -> None:
    launch = _launch()
    assert {field.name for field in fields(launch)}.isdisjoint({"state", "created_at"})
    assert launch.target == _target()

    record = RunRecord(launch, RunState.QUEUED, NOW)
    assert record.accepted_at is NOW
    assert record.state_changed_at is NOW
    assert record.sequence == 0
    with pytest.raises(ValueError, match="timezone-aware"):
        RunRecord(launch, RunState.QUEUED, datetime(2026, 8, 10, 12, 5))


def test_launch_authorization_binds_all_trusted_authority() -> None:
    _launch().authorize(_context(), at=NOW)
    _launch(
        source=RunSource.AGENT,
        initiated_by="agent-7",
        orchestrator="workflow-agent",
    ).authorize(_context(acting_agent_id="agent-7"), at=NOW)

    failures = [
        (_launch(), _context(granted_actions={RUN_READ_ACTION})),
        (_launch(tenant_id="tenant-2"), _context()),
        (_launch(target=_target(boundary_id="workspace-2")), _context()),
        (_launch(target=_target(capability="deploy.execute")), _context()),
        (_launch(initiated_by="principal-2"), _context()),
        (
            _launch(
                source=RunSource.AGENT,
                initiated_by="agent-7",
                orchestrator="workflow-agent",
            ),
            _context(acting_agent_id="agent-8"),
        ),
    ]
    for launch, context in failures:
        with pytest.raises(OperatorAuthorizationError):
            launch.authorize(context, at=NOW)
    with pytest.raises(OperatorAuthorizationError, match="agent-mediated"):
        _launch().authorize(_context(acting_agent_id="agent-7"), at=NOW)


def test_manual_and_agent_launch_shape_invariants() -> None:
    assert _launch().orchestrator is None
    with pytest.raises(ValueError, match="manual"):
        _launch(orchestrator="workflow-agent")
    with pytest.raises(ValueError, match="agent"):
        _launch(source=RunSource.AGENT)


def test_run_models_are_frozen_and_correlate_attempt_and_external_job() -> None:
    launch = _launch()
    record = RunRecord(launch, RunState.RUNNING, NOW)
    stage = RunStage(launch.run_id, "compile", "command", RunState.RUNNING)
    attempt = RunAttempt(launch.run_id, stage.stage_id, "attempt-2", 2)
    link = ExternalEngineJobLink(
        run_id=attempt.run_id,
        stage_id=attempt.stage_id,
        attempt_id=attempt.attempt_id,
        engine="generic-runner",
        external_job_id="queue/job/8394",
    )

    assert record.run_id == "run-1"
    assert link.external_job_id != record.run_id
    with pytest.raises(FrozenInstanceError):
        record.state = RunState.SUCCEEDED  # type: ignore[misc]
    with pytest.raises(ValueError, match="at least 1"):
        RunAttempt("run-1", "compile", "attempt-0", 0)
    with pytest.raises(ValueError, match="precede"):
        RunRecord(
            launch,
            RunState.RUNNING,
            NOW,
            datetime(2026, 8, 10, 12, 4, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="non-negative"):
        RunRecord(launch, RunState.RUNNING, NOW, sequence=-1)


def test_resolved_run_authorization_rechecks_durable_launch_scope() -> None:
    record = RunRecord(_launch(), RunState.RUNNING, NOW)

    record.authorize(_context(), RUN_READ_ACTION, at=NOW)
    failures = (
        _context(granted_actions={RUN_ATTACH_ACTION}),
        _context(permitted_boundary_ids={"workspace-2"}),
        _context(granted_capabilities={"deploy.execute"}),
    )
    for context in failures:
        with pytest.raises(OperatorAuthorizationError):
            record.authorize(context, RUN_READ_ACTION, at=NOW)

    cross_tenant_with_other_denials = _context(
        tenant_id="tenant-2",
        granted_actions=frozenset(),
        granted_capabilities=frozenset(),
        permitted_boundary_ids=frozenset(),
        expires_at=NOW,
    )
    with pytest.raises(RunNotFoundError, match="run not found"):
        record.authorize(cross_tenant_with_other_denials, RUN_READ_ACTION, at=NOW)


def test_resolved_run_tenant_mismatch_matches_tenant_scoped_absence() -> None:
    record = RunRecord(_launch(), RunState.RUNNING, NOW)

    with pytest.raises(RunNotFoundError) as direct_error:
        record.authorize(
            _context(tenant_id="tenant-2"), RUN_READ_ACTION, at=NOW
        )

    absence_error = RunNotFoundError("run not found")
    assert type(direct_error.value) is type(absence_error)
    assert str(direct_error.value) == str(absence_error)


def test_terminal_state_and_separate_control_and_artifact_actions() -> None:
    assert {state for state in RunState if state.is_terminal()} == {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.CANCELLED,
    }
    assert set(RunControlAction) == {
        RunControlAction.PAUSE,
        RunControlAction.RESUME,
        RunControlAction.CANCEL,
        RunControlAction.RETRY,
    }
    assert set(ArtifactAuthorizationAction) == {ArtifactAuthorizationAction.READ}
    assert ArtifactAuthorizationAction.READ.value == ARTIFACT_READ_ACTION
    assert RUN_ATTACH_ACTION == "run.attach"
    assert RUN_CANCEL_ACTION == "run.cancel"
    assert RUN_LAUNCH_ACTION == "run.launch"
    assert RUN_PAUSE_ACTION == "run.pause"
    assert RUN_READ_ACTION == "run.read"
    assert RUN_RESUME_ACTION == "run.resume"
    assert RUN_RETRY_ACTION == "run.retry"
    assert {
        action: action.required_action for action in RunControlAction
    } == {
        RunControlAction.PAUSE: RUN_PAUSE_ACTION,
        RunControlAction.RESUME: RUN_RESUME_ACTION,
        RunControlAction.CANCEL: RUN_CANCEL_ACTION,
        RunControlAction.RETRY: RUN_RETRY_ACTION,
    }
    control = RunControl("run-1", RunControlAction.PAUSE, "pause-request-1")
    assert control.idempotency_key == "pause-request-1"
    assert control.idempotency_scope("tenant-1") == (
        "tenant-1",
        "run-1",
        RUN_PAUSE_ACTION,
        None,
        "pause-request-1",
    )
    assert _launch().idempotency_scope("tenant-1") == (
        "tenant-1",
        RUN_LAUNCH_ACTION,
        "launch-request-1",
    )
    with pytest.raises(ValueError):
        RunControl("run-1", RunControlAction.PAUSE, "")


def test_control_expected_sequence_is_validated_and_outside_key_scope() -> None:
    control = RunControl(
        "run-1",
        RunControlAction.PAUSE,
        "pause-request-1",
        expected_sequence=7,
    )

    assert control.expected_sequence == 7
    assert control.idempotency_scope("tenant-1") == (
        "tenant-1",
        "run-1",
        RUN_PAUSE_ACTION,
        None,
        "pause-request-1",
    )
    with pytest.raises(TypeError, match="integer or None"):
        RunControl(
            "run-1",
            RunControlAction.PAUSE,
            "pause-request-2",
            expected_sequence=True,
        )
    with pytest.raises(ValueError, match="non-negative"):
        RunControl(
            "run-1",
            RunControlAction.PAUSE,
            "pause-request-3",
            expected_sequence=-1,
        )


def test_retry_requires_stage_and_control_authorization_is_exact() -> None:
    retry = RunControl(
        "run-1", RunControlAction.RETRY, "retry-request-1", stage_id="compile"
    )
    assert retry.idempotency_scope("tenant-1") == (
        "tenant-1",
        "run-1",
        RUN_RETRY_ACTION,
        "compile",
        "retry-request-1",
    )
    retry.authorize(_context(granted_actions={RUN_RETRY_ACTION}), at=NOW)
    with pytest.raises(OperatorAuthorizationError):
        retry.authorize(_context(granted_actions={RUN_PAUSE_ACTION}), at=NOW)
    with pytest.raises(OperatorAuthorizationError, match="fresh"):
        retry.authorize(
            _context(granted_actions={RUN_RETRY_ACTION}),
            at=datetime(2026, 8, 10, 12, 15, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="required"):
        RunControl("run-1", RunControlAction.RETRY, "retry-request-2")
    with pytest.raises(ValueError, match="only for retry"):
        RunControl(
            "run-1", RunControlAction.PAUSE, "pause-request-2", stage_id="compile"
        )


def test_control_authorize_is_only_the_pre_resolution_gate() -> None:
    control = RunControl("run-1", RunControlAction.PAUSE, "pause-request-1")
    wrong_boundary = _context(
        granted_actions={RUN_PAUSE_ACTION},
        permitted_boundary_ids={"workspace-2"},
    )

    control.authorize(wrong_boundary, at=NOW)
    with pytest.raises(OperatorAuthorizationError, match="boundary"):
        RunRecord(_launch(), RunState.RUNNING, NOW).authorize(
            wrong_boundary, control.action.required_action, at=NOW
        )


def test_run_query_and_page_are_bounded_and_cursor_based() -> None:
    record = RunRecord(_launch(), RunState.RUNNING, NOW)
    query = RunQuery(
        limit=25, cursor="opaque_cursor-1", states={RunState.RUNNING}, kinds={"build"}
    )
    page = RunPage([record], next_cursor="opaque_cursor-2")  # type: ignore[arg-type]

    assert query.states == frozenset({RunState.RUNNING})
    assert query.kinds == frozenset({"build"})
    assert page.records == (record,)
    with pytest.raises(ValueError, match="duplicate run_id"):
        RunPage((record, record))
    divergent = RunRecord(_launch(), RunState.SUCCEEDED, NOW, sequence=1)
    with pytest.raises(ValueError, match="duplicate run_id"):
        RunPage((record, divergent))
    with pytest.raises(ValueError, match="between"):
        RunQuery(limit=101)
    with pytest.raises(ValueError, match="cursor"):
        RunQuery(cursor="not/a/cursor")
    with pytest.raises(ValueError, match="at most"):
        RunPage(tuple(record for _ in range(101)))
    with pytest.raises(TypeError, match="RunRecord"):
        RunPage((["unhashable"],))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="next_cursor"):
        RunPage((), next_cursor=1)  # type: ignore[arg-type]


def test_artifact_accepts_only_canonical_or_signed_https_hrefs() -> None:
    assert _artifact().href == "/authorized/artifacts/artifact-1"
    signed = _artifact(
        href=(
            "https://objects.example.test/reports/artifact-1.json"
            "?X-Amz-Expires=300&X-Amz-Credential=scope%2Fkey&X-Amz-Signature=abc123"
        )
    )
    assert signed.href.startswith("https://")
    assert _artifact(href="HTTPS://objects.example.test/report?signature=abc").href


@pytest.mark.parametrize(
    "href",
    [
        "//operator.example.test/authorized/artifacts/artifact-1",
        "/authorized/artifacts/artifact-1/extra",
        "/authorized/artifacts/artifact-1?token=opaque",
        "/authorized/artifacts/artifact-2",
        "/artifacts/artifact-1",
        "artifacts/artifact-1",
        "file:///private/tmp/report.json",
        "/private/tmp/report.json",
        "C:\\reports\\report.json",
        "http://operator.example.test/report?signature=abc",
        "https://operator.example.test/report",
        "https://operator.example.test/reports/../secrets?signature=abc",
        "https://operator.example.test/reports/%2e%2e/secrets?signature=abc",
        "https://operator.example.test/reports/%252e%252e/secrets?signature=abc",
        "https://operator.example.test/reports/%2Fsecrets?signature=abc",
        "https://user:password@operator.example.test/report?signature=abc",
        "https://operator.example.test/report?signature=abc#fragment",
        "https://operator.example.test/report?bad=%ZZ",
        "https://bad_host.example.test/report?signature=abc",
        "https://bad%00host.example.test/report?signature=abc",
        "https://operator.example.test/re\u2028port?signature=abc",
        "https://operator.example.test/re\u200bport?signature=abc",
        "https://operator.example.test/re port?signature=abc",
    ],
)
def test_artifact_rejects_hostile_or_non_authorized_hrefs(href: str) -> None:
    with pytest.raises(ValueError):
        _artifact(href=href)


def test_artifact_copies_and_deeply_freezes_json_metadata() -> None:
    metadata = {"summary": {"passed": True}, "steps": ["lint"]}
    artifact = _artifact(metadata=metadata)
    metadata["summary"]["passed"] = False  # type: ignore[index]
    metadata["steps"].append("deploy")  # type: ignore[union-attr]

    assert artifact.metadata["summary"]["passed"] is True  # type: ignore[index]
    assert artifact.metadata["steps"] == ("lint",)
    with pytest.raises(TypeError):
        artifact.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError, match="JSON-like"):
        _artifact(metadata={"bad": object()})


def test_artifact_with_nested_metadata_is_hashable_by_value() -> None:
    artifact = _artifact(
        metadata={"nested": {"items": [1, {"passed": True}]}, "label": "ok"}
    )
    equal = _artifact(
        metadata={"label": "ok", "nested": {"items": [1, {"passed": True}]}}
    )

    assert artifact == equal
    assert hash(artifact) == hash(equal)
    assert {artifact, equal} == {artifact}


@pytest.mark.parametrize(
    "metadata",
    [
        {"bad\u202e": "text"},
        {"bad": "zero\u200bwidth"},
        {"bad": "line\u2028separator"},
        {"bad": 9_007_199_254_740_992},
        {"bad": 1.234567890123456},
        {"bad": "x" * 4097},
    ],
)
def test_artifact_metadata_rejects_unsafe_text_and_numbers(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _artifact(metadata=metadata)


def test_artifact_metadata_allows_joiners_and_unassigned_codepoints() -> None:
    artifact = _artifact(metadata={"joined": "می\u200cروم 👩\u200d💻", "new": "\u0378"})

    assert artifact.metadata["joined"] == "می\u200cروم 👩\u200d💻"
    assert artifact.metadata["new"] == "\u0378"


def test_artifact_metadata_has_total_node_key_and_depth_budgets() -> None:
    with pytest.raises(ValueError, match="keys"):
        _artifact(metadata={f"key-{index}": index for index in range(257)})
    with pytest.raises(ValueError, match="nodes"):
        _artifact(metadata={"items": [None] * 1024})
    nested: object = "leaf"
    for _ in range(17):
        nested = {"next": nested}
    with pytest.raises(ValueError, match="levels"):
        _artifact(metadata={"root": nested})


class _FixtureRunService:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], RunRecord] = {}
        self.keys: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.stages: dict[tuple[str, str], tuple[RunStage, ...]] = {}
        self.attempts: dict[tuple[str, str, str], tuple[RunAttempt, ...]] = {}
        self.artifacts: dict[tuple[str, str], ArtifactRecord] = {}

    def _get_visible_run(self, run_id: str, context: OperatorContext) -> RunRecord:
        try:
            return self.runs[(context.tenant_id, run_id)]
        except KeyError as error:
            raise RunNotFoundError("run not found") from error

    def _get_authorized_run(
        self, run_id: str, context: OperatorContext, action: str
    ) -> RunRecord:
        record = self._get_visible_run(run_id, context)
        record.authorize(context, action, at=NOW)
        return record

    async def launch_run(
        self, launch: RunLaunch, context: OperatorContext
    ) -> RunRecord:
        launch.authorize(context, at=NOW)
        scope = launch.idempotency_scope(context.tenant_id)
        existing_scope = self.keys.get(scope)
        if existing_scope is not None:
            existing = self.runs[existing_scope]
            if existing.launch.replay_identity != launch.replay_identity:
                raise RunConflictError("conflicting idempotency key")
            return existing
        run_scope = (context.tenant_id, launch.run_id)
        if run_scope in self.runs:
            raise RunConflictError("run already exists under another key")
        record = RunRecord(launch, RunState.QUEUED, NOW)
        self.keys[scope] = run_scope
        self.runs[run_scope] = record
        return record

    async def get_run(self, run_id: str, context: OperatorContext) -> RunRecord:
        return self._get_authorized_run(run_id, context, RUN_READ_ACTION)

    async def list_runs(
        self, query: RunQuery, context: OperatorContext
    ) -> RunPage:
        context.require_fresh(NOW)
        context.require_action(RUN_READ_ACTION)
        visible_records: list[RunRecord] = []
        for (tenant_id, _), record in self.runs.items():
            if tenant_id != context.tenant_id:
                continue
            try:
                record.authorize(context, RUN_READ_ACTION, at=NOW)
            except OperatorAuthorizationError:
                continue
            visible_records.append(record)
        visible = tuple(visible_records[: query.limit])
        return RunPage(visible)

    async def list_stages(
        self,
        run_id: str,
        context: OperatorContext,
        *,
        limit: int = 100,
    ) -> tuple[RunStage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self._get_authorized_run(run_id, context, RUN_READ_ACTION)
        stages = self.stages.get((context.tenant_id, run_id), ())
        return tuple(sorted(stages, key=lambda stage: stage.stage_id))[:limit]

    async def list_attempts(
        self,
        run_id: str,
        stage_id: str,
        context: OperatorContext,
        *,
        limit: int = 100,
    ) -> tuple[RunAttempt, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self._get_authorized_run(run_id, context, RUN_READ_ACTION)
        attempts = self.attempts.get((context.tenant_id, run_id, stage_id), ())
        return tuple(sorted(attempts, key=lambda attempt: attempt.attempt))[:limit]

    async def apply_control(
        self, control: RunControl, context: OperatorContext
    ) -> RunRecord:
        control.authorize(context, at=NOW)
        record = self._get_authorized_run(
            control.run_id, context, control.action.required_action
        )
        if (
            control.expected_sequence is not None
            and control.expected_sequence != record.sequence
        ):
            raise RunConflictError("run sequence precondition failed")
        return record

    async def attach_external_job(
        self, link: ExternalEngineJobLink, context: OperatorContext
    ) -> ExternalEngineJobLink:
        self._get_authorized_run(link.run_id, context, RUN_ATTACH_ACTION)
        return link

    async def attach_artifact(
        self, artifact: ArtifactRecord, context: OperatorContext
    ) -> ArtifactRecord:
        self._get_authorized_run(artifact.run_id, context, RUN_ATTACH_ACTION)
        self.artifacts[(context.tenant_id, artifact.artifact_id)] = artifact
        return artifact

    async def get_artifact(
        self, artifact_id: str, context: OperatorContext
    ) -> ArtifactRecord:
        try:
            artifact = self.artifacts[(context.tenant_id, artifact_id)]
        except KeyError as error:
            raise RunNotFoundError("artifact not found") from error
        self._get_authorized_run(
            artifact.run_id, context, ARTIFACT_READ_ACTION
        )
        return artifact


@pytest.mark.asyncio
async def test_run_service_is_structural_async_and_launches_are_idempotent() -> None:
    service = _FixtureRunService()
    context = _context()
    launch = _launch()

    assert isinstance(service, RunService)
    first = await service.launch_run(launch, context)
    replay = await service.launch_run(launch, context)
    assert replay is first
    assert replay.accepted_at == first.accepted_at

    with pytest.raises(RunConflictError, match="conflicting"):
        await service.launch_run(_launch(kind="deploy"), context)
    with pytest.raises(RunConflictError, match="conflicting"):
        await service.launch_run(_launch(run_id="run-2"), context)
    with pytest.raises(RunConflictError, match="another key"):
        await service.launch_run(
            _launch(idempotency_key="different-launch-key"), context
        )


@pytest.mark.asyncio
async def test_run_service_fixture_enforces_freshness_and_each_action() -> None:
    service = _FixtureRunService()
    context = _context()
    await service.launch_run(_launch(), context)
    stage = RunStage("run-1", "compile", "command")
    later_stage = RunStage("run-1", "test", "command")
    attempt = RunAttempt("run-1", "compile", "attempt-1", 1)
    later_attempt = RunAttempt("run-1", "compile", "attempt-2", 2)
    service.stages[("tenant-1", "run-1")] = (later_stage, stage)
    service.attempts[("tenant-1", "run-1", "compile")] = (
        later_attempt,
        attempt,
    )

    assert await service.list_stages("run-1", context, limit=1) == (stage,)
    assert await service.list_attempts("run-1", "compile", context) == (
        attempt,
        later_attempt,
    )
    assert await service.apply_control(
        RunControl("run-1", RunControlAction.PAUSE, "pause-1"),
        _context(granted_actions={RUN_PAUSE_ACTION}),
    ) == await service.get_run("run-1", context)
    assert await service.apply_control(
        RunControl(
            "run-1",
            RunControlAction.PAUSE,
            "pause-2",
            expected_sequence=0,
        ),
        _context(granted_actions={RUN_PAUSE_ACTION}),
    ) == await service.get_run("run-1", context)
    with pytest.raises(RunConflictError, match="sequence"):
        await service.apply_control(
            RunControl(
                "run-1",
                RunControlAction.PAUSE,
                "pause-3",
                expected_sequence=1,
            ),
            _context(granted_actions={RUN_PAUSE_ACTION}),
        )
    with pytest.raises(ValueError, match="between"):
        await service.list_attempts("run-1", "compile", context, limit=101)

    read_methods = (
        service.get_run("run-1", _context(granted_actions={RUN_LAUNCH_ACTION})),
        service.list_runs(
            RunQuery(), _context(granted_actions={RUN_LAUNCH_ACTION})
        ),
        service.list_stages(
            "run-1", _context(granted_actions={RUN_LAUNCH_ACTION})
        ),
        service.list_attempts(
            "run-1", "compile", _context(granted_actions={RUN_LAUNCH_ACTION})
        ),
    )
    for call in read_methods:
        with pytest.raises(OperatorAuthorizationError):
            await call

    artifact = _artifact()
    await service.attach_artifact(artifact, context)
    assert await service.get_artifact("artifact-1", context) is artifact
    with pytest.raises(OperatorAuthorizationError):
        await service.get_artifact(
            "artifact-1", _context(granted_actions={RUN_READ_ACTION})
        )
    with pytest.raises(OperatorAuthorizationError, match="fresh"):
        await service.get_run(
            "run-1",
            _context(expires_at=datetime(2026, 8, 10, 12, 5, tzinfo=UTC)),
        )


@pytest.mark.asyncio
async def test_attachments_require_run_attach_not_run_read() -> None:
    service = _FixtureRunService()
    await service.launch_run(_launch(), _context())
    artifact = _artifact()
    link = ExternalEngineJobLink(
        run_id="run-1",
        stage_id="compile",
        attempt_id="attempt-1",
        engine="generic-runner",
        external_job_id="queue/job/8394",
    )
    read_only = _context(granted_actions={RUN_READ_ACTION})
    attach_only = _context(granted_actions={RUN_ATTACH_ACTION})

    with pytest.raises(OperatorAuthorizationError):
        await service.attach_external_job(link, read_only)
    with pytest.raises(OperatorAuthorizationError):
        await service.attach_artifact(artifact, read_only)

    assert await service.attach_external_job(link, attach_only) is link
    assert await service.attach_artifact(artifact, attach_only) is artifact
    expired_attach = _context(
        granted_actions={RUN_ATTACH_ACTION},
        expires_at=datetime(2026, 8, 10, 12, 5, tzinfo=UTC),
    )
    with pytest.raises(OperatorAuthorizationError, match="fresh"):
        await service.attach_external_job(link, expired_attach)
    with pytest.raises(OperatorAuthorizationError, match="fresh"):
        await service.attach_artifact(artifact, expired_attach)
    with pytest.raises(OperatorAuthorizationError):
        await service.get_run("run-1", attach_only)
    with pytest.raises(OperatorAuthorizationError):
        await service.get_artifact("artifact-1", attach_only)


@pytest.mark.asyncio
async def test_missing_and_cross_tenant_runs_share_typed_not_found_error() -> None:
    service = _FixtureRunService()
    await service.launch_run(_launch(), _context())

    with pytest.raises(RunNotFoundError, match="run not found"):
        await service.get_run("missing", _context())
    with pytest.raises(RunNotFoundError, match="run not found"):
        await service.get_run("run-1", _context(tenant_id="tenant-2"))
    control_context = _context(granted_actions={RUN_PAUSE_ACTION})
    with pytest.raises(RunNotFoundError, match="run not found"):
        await service.apply_control(
            RunControl("missing", RunControlAction.PAUSE, "pause-missing"),
            control_context,
        )
    with pytest.raises(RunNotFoundError, match="run not found"):
        await service.apply_control(
            RunControl("run-1", RunControlAction.PAUSE, "pause-cross-tenant"),
            _context(
                tenant_id="tenant-2", granted_actions={RUN_PAUSE_ACTION}
            ),
        )


@pytest.mark.asyncio
async def test_post_launch_operations_deny_same_tenant_cross_boundary() -> None:
    service = _FixtureRunService()
    authorized = _context()
    await service.launch_run(_launch(), authorized)
    service.stages[("tenant-1", "run-1")] = (
        RunStage("run-1", "compile", "command"),
    )
    service.attempts[("tenant-1", "run-1", "compile")] = (
        RunAttempt("run-1", "compile", "attempt-1", 1),
    )
    artifact = _artifact()
    await service.attach_artifact(artifact, authorized)
    denied = _context(permitted_boundary_ids={"workspace-2"})

    assert (await service.list_runs(RunQuery(), denied)).records == ()
    operations = (
        service.get_run("run-1", denied),
        service.list_stages("run-1", denied),
        service.list_attempts("run-1", "compile", denied),
        service.apply_control(
            RunControl("run-1", RunControlAction.PAUSE, "pause-boundary"),
            denied,
        ),
        service.attach_external_job(
            ExternalEngineJobLink(
                "run-1",
                "compile",
                "attempt-1",
                "generic-runner",
                "job-1",
            ),
            denied,
        ),
        service.attach_artifact(
            _artifact(
                artifact_id="artifact-2",
                href="/authorized/artifacts/artifact-2",
            ),
            denied,
        ),
        service.get_artifact("artifact-1", denied),
    )
    for operation in operations:
        with pytest.raises(OperatorAuthorizationError, match="boundary"):
            await operation
