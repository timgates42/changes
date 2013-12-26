from __future__ import absolute_import, division, unicode_literals

from datetime import datetime, timedelta
from flask import Response
from sqlalchemy import and_
from sqlalchemy.orm import joinedload, subqueryload

from changes.api.base import APIView
from changes.config import db
from changes.models import Project, AggregateTestGroup, TestGroup, Job


class ProjectTestIndexAPIView(APIView):
    def _get_project(self, project_id):
        queryset = Project.query.options(
            joinedload(Project.repository),
        )

        project = queryset.filter_by(slug=project_id).first()
        if project is None:
            project = queryset.get(project_id)
        return project

    def get(self, project_id):
        project = self._get_project(project_id)
        if not project:
            return Response(status=404)

        cutoff = datetime.utcnow() - timedelta(days=3)

        test_list = db.session.query(AggregateTestGroup, TestGroup).options(
            subqueryload(AggregateTestGroup.first_job),
            subqueryload(AggregateTestGroup.last_job),
            subqueryload(AggregateTestGroup.parent),
            subqueryload('first_job.author'),
            subqueryload('last_job.author'),
        ).join(
            TestGroup, and_(
                TestGroup.job_id == AggregateTestGroup.last_job_id,
                TestGroup.name_sha == AggregateTestGroup.name_sha,
            )
        ).join(
            AggregateTestGroup.last_job,
        ).filter(
            AggregateTestGroup.parent_id == None,  # NOQA: we have to use == here
            AggregateTestGroup.project_id == project.id,
            Job.date_created > cutoff,
        ).order_by(TestGroup.duration.desc())

        results = []
        for agg, group in test_list:
            agg.last_testgroup = group
            results.append(agg)

        context = {
            'tests': results,
        }

        return self.respond(context)
