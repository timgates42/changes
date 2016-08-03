from __future__ import absolute_import, print_function

import os
import requests

from collections import defaultdict
from datetime import datetime

from flask import current_app
from requests.exceptions import ConnectionError, HTTPError, Timeout, SSLError
from sqlalchemy import distinct
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import func

from changes.constants import Status, Result
from changes.config import db, statsreporter
from changes.db.utils import try_create
from changes.jobs.sync_artifact import sync_artifact
from changes.lib.artifact_store_lib import ArtifactStoreClient
from changes.models.artifact import Artifact
from changes.models.failurereason import FailureReason
from changes.models.filecoverage import FileCoverage
from changes.models.itemstat import ItemStat
from changes.models.jobphase import JobPhase
from changes.models.jobplan import JobPlan
from changes.models.jobstep import JobStep
from changes.models.log import LogSource
from changes.models.option import ItemOption
from changes.models.snapshot import SnapshotImage
from changes.models.task import Task
from changes.models.test import TestCase
from changes.queue.task import tracked_task
from changes.db.utils import get_or_create
from changes.storage.artifactstore import ARTIFACTSTORE_PREFIX, ArtifactStoreFileStorage

INFRA_FAILURE_REASONS = ['malformed_manifest_json', 'missing_manifest_json']


def abort_step(task):
    step = JobStep.query.get(task.kwargs['step_id'])
    step.status = Status.finished
    step.result = Result.aborted
    db.session.add(step)
    db.session.commit()
    current_app.logger.exception('Unrecoverable exception syncing step %s', step.id)


def is_missing_tests(step, jobplan):
    return _expects_tests(jobplan) and is_final_jobphase(step.phase) and not _has_tests(step)


def _result_from_failure_reasons(step):
    if step.replacement_id is not None:
        return None
    reasons = [r for r, in db.session.query(
        distinct(FailureReason.reason)
    ).filter(
        FailureReason.step_id == step.id,
    ).all()]
    if any(infra_reason in reasons for infra_reason in INFRA_FAILURE_REASONS):
        return Result.infra_failed
    elif reasons:
        return Result.failed
    return None


def _is_snapshot_job(jobplan):
    """
    Args:
        jobplan (JobPlan): The JobPlan to check.
    Returns:
        bool: Whether this plan is for a job that is creating a snapshot.
    """
    if not jobplan:
        return False

    is_snapshot = db.session.query(SnapshotImage.query.filter(
        SnapshotImage.job_id == jobplan.job_id
    ).exists()).scalar()
    return bool(is_snapshot)


def _expects_tests(jobplan):
    """Check whether a jobplan expects tests or not.

    Usually this is encoded within the jobplan itself under a snapshot
    of the ItemOptions associated with the plan, but if not we fall
    back to looking at the plan itself.

    Since snapshot builds never return tests, we override this for
    snapshot builds and never expect tests for them (which allows
    building a snapshot for a plan that has buld.expect-tests enabled)
    """
    if _is_snapshot_job(jobplan):
        return False

    if 'snapshot' in jobplan.data:
        options = jobplan.data['snapshot']['options']
    else:
        options = dict(db.session.query(
            ItemOption.name, ItemOption.value,
        ).filter(
            ItemOption.item_id == jobplan.plan.id,
            ItemOption.name == 'build.expect-tests',
        ))

    return options.get('build.expect-tests') == '1'


def is_final_jobphase(phase):
    # TODO(dcramer): there is probably a better way we can be explicit about
    # this?
    jobphase_query = JobPhase.query.filter(
        JobPhase.job_id == phase.job_id,
        JobPhase.id != phase.id,
        JobPhase.date_created > phase.date_created,
    )
    return not db.session.query(jobphase_query.exists()).scalar()


def _has_tests(step):
    has_tests = db.session.query(TestCase.query.filter(
        TestCase.step_id == step.id,
    ).exists()).scalar()

    return has_tests


def has_test_failures(step):
    return db.session.query(TestCase.query.filter(
        TestCase.step_id == step.id,
        TestCase.result == Result.failed,
    ).exists()).scalar()


# Extra time to allot snapshot builds, as they are expected to take longer.
_SNAPSHOT_TIMEOUT_BONUS_MINUTES = 40


def has_timed_out(step, jobplan, default_timeout):
    """
    Args:
        default_timeout (int): Timeout in minutes to be used when
            no timeout is specified for this build. Required because
            nothing is expected to run forever.
    """
    if step.status != Status.in_progress:
        # HACK: We don't really want to timeout jobsteps that are
        # stuck for infrastructural reasons here, but we don't yet
        # have code to handle other cases well.
        # To be sure that jobsteps can die, we cast a wide net here.
        if step.status == Status.finished:
            return False

    # date_started is preferred if available, but we fall back to
    # date_created (always set) so jobsteps that never start don't get to run forever.
    start_time = step.date_started or step.date_created

    # TODO(adai): specify timeout in project YAML file, currently uses global
    # defaults for step timeout duration if the job is autogenerated
    # TODO(dcramer): we make an assumption that there is a single step
    options = jobplan.get_steps()[0].options if not jobplan.plan.autogenerated() else {}

    timeout = int(options.get('build.timeout', '0')) or default_timeout

    # timeout is in minutes
    timeout = timeout * 60

    # Snapshots are given a few extra minutes before being considered as timed out.
    if _is_snapshot_job(jobplan):
        timeout += 60 * _SNAPSHOT_TIMEOUT_BONUS_MINUTES

    # If jobstep is still queued/pending allocation, apply a separate timeout
    if step.status in (Status.allocated, Status.pending_allocation, Status.queued):
        timeout = TIMEOUT_IN_QUEUE_MIN * 60

    delta = datetime.utcnow() - start_time
    if delta.total_seconds() > timeout:
        return True

    return False


def record_coverage_stats(step):
    coverage_stats = db.session.query(
        func.sum(FileCoverage.lines_covered).label('lines_covered'),
        func.sum(FileCoverage.lines_uncovered).label('lines_uncovered'),
        func.sum(FileCoverage.diff_lines_covered).label('diff_lines_covered'),
        func.sum(FileCoverage.diff_lines_uncovered).label('diff_lines_uncovered'),
    ).filter(
        FileCoverage.step_id == step.id,
    ).group_by(
        FileCoverage.step_id,
    ).first()

    stat_list = (
        'lines_covered', 'lines_uncovered',
        'diff_lines_covered', 'diff_lines_uncovered',
    )
    for stat_name in stat_list:
        try_create(ItemStat, where={
            'item_id': step.id,
            'name': stat_name,
            'value': getattr(coverage_stats, stat_name, 0) or 0,
        })


# In minutes, the timeout applied to jobs without a timeout specified at build time.
# If the job legitimately takes more than an hour, the build
# should specify an appropriate timeout.
DEFAULT_TIMEOUT_MIN = 60

# In minutes, the timeout applied to jobs which are in the queue/pending allocation.
# This is applied uniformly across all jobsteps, and cannot be overridden per jobstep.
TIMEOUT_IN_QUEUE_MIN = 180


# In seconds, the timeout applied to any requests we make to the artifacts
# store. Arbitrarily chose as the amount of delay we can tolerate for each
# sync_job_step.
ARTIFACTS_REQUEST_TIMEOUT_SECS = 5


# List of artifact names recognized to be log source (content which is
# continuously updated during the duration of a test, like console logs and
# infralogs)
LOGSOURCE_WHITELIST = ('console', 'infralog',)


def _sync_from_artifact_store(jobstep):
    """Checks and creates new artifacts from the artifact store."""
    url = '{base}/buckets/{jobstep_id}/artifacts/'.format(
        base=current_app.config.get('ARTIFACTS_SERVER'),
        jobstep_id=jobstep.id.hex,
    )
    job = jobstep.job

    try:
        res = requests.get(url, timeout=ARTIFACTS_REQUEST_TIMEOUT_SECS)
        res.raise_for_status()
        artifacts = res.json()
        for artifact in artifacts:
            # Artifact name is guaranteed to be unique in an artifact store bucket.
            artifact_name = artifact['name']
            artifact_path = artifact['relativePath']
            if artifact_name in LOGSOURCE_WHITELIST:
                _, created = get_or_create(LogSource, where={
                    'name': artifact_name,
                    'job': job,
                    'step': jobstep,
                }, defaults={
                    'project': job.project,
                    'date_created': job.date_started,
                    'in_artifact_store': True,
                })

                if created:
                    try:
                        db.session.commit()
                    except IntegrityError as err:
                        db.session.rollback()
                        current_app.logger.error(
                            'DB Error while inserting LogSource %s',
                            artifact_name, exc_info=True)

                # If this artifact is a logsource, don't add it to the list of
                # test artifacts.
                continue

            art, created = get_or_create(Artifact, where={
                # Don't conflict with same artifacts uploaded by other means (Jenkins/Mesos)
                'name': ARTIFACTSTORE_PREFIX + artifact_path,
                'step_id': jobstep.id,
                'job_id': jobstep.job_id,
                'project_id': jobstep.project_id,
            })
            if created:
                art.file.storage = 'changes.storage.artifactstore.ArtifactStoreFileStorage'
                filename = ArtifactStoreFileStorage.get_filename_from_artifact_name(jobstep.id.hex, artifact_name)
                art.file.set_filename(filename)
                try:
                    db.session.add(art)
                    db.session.commit()
                except IntegrityError as err:
                    db.session.rollback()
                    current_app.logger.error(
                        'DB Error while inserting artifact %s: %s', filename, err)
    except (ConnectionError, HTTPError, SSLError, Timeout) as err:
        if isinstance(err, HTTPError) and err.response is not None and err.response.status_code == 404:
            # While not all plans use the Artifact Store, 404s are normal and expected.
            # No sense in reporting them.
            pass
        else:
            # Log to sentry - unable to contact artifacts store
            current_app.logger.warning('Error fetching url %s: %s', url, err, exc_info=True)
    except Exception as err:
        current_app.logger.error('Error updating artifacts for jobstep %s: %s', jobstep, err, exc_info=True)
        raise err


def _get_artifacts_to_sync(artifacts, artifact_manager, prefer_artifactstore):
    def is_artifact_store(artifact):
        return artifact.file.storage == 'changes.storage.artifactstore.ArtifactStoreFileStorage' and artifact.file

    artifacts_by_name = defaultdict(list)
    # group by filename
    for artifact in artifacts:
        artifacts_by_name[os.path.basename(artifact.name)].append(artifact)

    to_sync = []
    for _, arts in artifacts_by_name.iteritems():
        # don't sync_artifact artifacts that we won't actually process
        arts = [art for art in arts if artifact_manager.can_process(art.name)]
        if len(arts) == 0:
            continue

        artifactstore_arts = [a for a in arts if is_artifact_store(a)]
        other_arts = [a for a in arts if not is_artifact_store(a)]

        # if we have this artifact from both sources, let buildstep choose which to use
        if len(artifactstore_arts) and len(other_arts):
            arts = artifactstore_arts if prefer_artifactstore else other_arts

        to_sync.extend(arts)

    return to_sync


def _sync_artifacts_for_jobstep(step):
    # only generate the sync_artifact tasks for this step once
    if Task.query.filter(
        Task.parent_id == step.id,
        Task.task_name == 'sync_artifact',
    ).first():
        return

    artifacts = Artifact.query.filter(Artifact.step_id == step.id).all()

    _, buildstep = JobPlan.get_build_step_for_job(job_id=step.job_id)
    prefer_artifactstore = buildstep.prefer_artifactstore()
    artifact_manager = buildstep.get_artifact_manager(step)
    to_sync = _get_artifacts_to_sync(artifacts, artifact_manager, prefer_artifactstore)

    # buildstep may want to check for e.g. required artifacts
    buildstep.verify_final_artifacts(step, to_sync)

    for artifact in to_sync:
        sync_artifact.delay_if_needed(
            artifact_id=artifact.id.hex,
            task_id=artifact.id.hex,
            parent_task_id=step.id.hex,
        )


@tracked_task(on_abort=abort_step, max_retries=100)
def sync_job_step(step_id):
    """
    Polls a build for updates. May have sync_artifact children.
    """
    step = JobStep.query.get(step_id)
    if not step:
        return

    jobplan, implementation = JobPlan.get_build_step_for_job(job_id=step.job_id)

    # only synchronize if upstream hasn't suggested we're finished
    if step.status != Status.finished:
        implementation.update_step(step=step)

    db.session.flush()

    _sync_from_artifact_store(step)

    if step.status == Status.finished:
        _sync_artifacts_for_jobstep(step)

    is_finished = (step.status == Status.finished and
                   # make sure all child tasks (like sync_artifact) have also finished
                   sync_job_step.verify_all_children() == Status.finished)

    if not is_finished:
        default_timeout = current_app.config['DEFAULT_JOB_TIMEOUT_MIN']
        if has_timed_out(step, jobplan, default_timeout=default_timeout):
            old_status = step.status
            step.data['timed_out'] = True
            implementation.cancel_step(step=step)

            # Not all implementations can actually cancel, but it's dead to us as of now
            # so we mark it as finished.
            step.status = Status.finished
            step.date_finished = datetime.utcnow()

            # Implementations default to marking canceled steps as aborted,
            # but we're not canceling on good terms (it should be done by now)
            # so we consider it a failure here.
            #
            # We check whether the step was marked as in_progress to make a best
            # guess as to whether this is an infrastructure failure, or the
            # repository under test is just taking too long. This won't be 100%
            # reliable, but is probably good enough.
            if old_status == Status.in_progress:
                step.result = Result.failed
            else:
                step.result = Result.infra_failed
            db.session.add(step)

            job = step.job
            try_create(FailureReason, {
                'step_id': step.id,
                'job_id': job.id,
                'build_id': job.build_id,
                'project_id': job.project_id,
                'reason': 'timeout'
            })

            db.session.flush()
            statsreporter.stats().incr('job_step_timed_out')
            # If we timeout something that isn't in progress, that's our fault, and we should know.
            if old_status != Status.in_progress:
                current_app.logger.warning(
                    "Timed out jobstep that wasn't in progress: %s (was %s)", step.id, old_status)

        raise sync_job_step.NotFinished

    # Close the ArtifactStore bucket used by jenkins, if it exists
    bucket_name = step.data.get('jenkins_bucket_name')
    if bucket_name:
        try:
            ArtifactStoreClient(current_app.config['ARTIFACTS_SERVER']).close_bucket(bucket_name)
        except Exception:
            # Closing buckets is not strictly necessary in artifactstore
            pass

    # Ignore any 'failures' if the build did not finish properly.
    # NOTE(josiah): we might want to include "unknown" and "skipped" here as
    # well, or have some named condition like "not meaningful_result(step.result)".
    if step.result in (Result.aborted, Result.infra_failed):
        _report_jobstep_result(step)
        return

    # Check for FailureReason objects generated by child jobs
    failure_result = _result_from_failure_reasons(step)
    if failure_result and failure_result != step.result:
        step.result = failure_result
        db.session.add(step)
        db.session.commit()
        if failure_result == Result.infra_failed:
            _report_jobstep_result(step)
            return

    try:
        record_coverage_stats(step)
    except Exception:
        current_app.logger.exception('Failing recording coverage stats for step %s', step.id)

    missing_tests = is_missing_tests(step, jobplan)

    try_create(ItemStat, where={
        'item_id': step.id,
        'name': 'tests_missing',
        'value': int(missing_tests),
    })

    if missing_tests:
        if step.result != Result.failed:
            step.result = Result.failed
            db.session.add(step)

        try_create(FailureReason, {
            'step_id': step.id,
            'job_id': step.job_id,
            'build_id': step.job.build_id,
            'project_id': step.project_id,
            'reason': 'missing_tests'
        })
        db.session.commit()

    db.session.flush()

    if has_test_failures(step):
        if step.result != Result.failed:
            step.result = Result.failed
            db.session.add(step)

        try_create(FailureReason, {
            'step_id': step.id,
            'job_id': step.job_id,
            'build_id': step.job.build_id,
            'project_id': step.project_id,
            'reason': 'test_failures'
        })
        db.session.commit()
    _report_jobstep_result(step)


def _report_jobstep_result(step):
    """To be called once we're done syncing a JobStep to report the result for monitoring.

    Args:
        step (JobStep): The JobStep to report the result of.
    """
    labels = {
        Result.unknown: 'unknown',
        Result.passed: 'passed',
        Result.failed: 'failed',
        Result.infra_failed: 'infra_failed',
        Result.aborted: 'aborted',
        Result.skipped: 'skipped',
    }
    label = labels.get(step.result, 'OTHER')
    # TODO(kylec): Include the project slug in the metric so we can
    # track on a per-project basis if needed.
    statsreporter.stats().incr('jobstep_result_' + label)
