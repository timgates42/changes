from __future__ import absolute_import, division, unicode_literals

from datetime import date, datetime, timedelta
from flask.ext.restful import reqparse
from sqlalchemy.sql import func

from changes.api.base import APIView
from changes.config import db
from changes.models import FlakyTestStat, Project, TestCase


CHART_DATA_LIMIT = 50


class ProjectFlakyTestsAPIView(APIView):
    parser = reqparse.RequestParser()
    parser.add_argument('date', type=unicode, location='args')

    def get(self, project_id):
        project = Project.get(project_id)
        if not project:
            return '', 404

        args = self.parser.parse_args()
        if args.date:
            try:
                query_date = datetime.strptime(args.date, '%Y-%m-%d').date()
            except:
                return 'Can\'t parse date "%s"' % (args.date), 500
        else:
            query_date = date.today() - timedelta(days=1)

        data = {
            'date': str(query_date),
            'chartData': self.get_chart_data(project_id, query_date),
            'flakyTests': self.get_flaky_tests(project_id, query_date)
        }

        return self.respond(data, serialize=False)

    def get_flaky_tests(self, project_id, query_date):
        subquery = db.session.query(
            FlakyTestStat,
            TestCase
        ).join(
            TestCase
        )

        flakytests_query = subquery.filter(
            FlakyTestStat.date == query_date,
            FlakyTestStat.project_id == project_id
        ).order_by(
            FlakyTestStat.flaky_runs.desc()
        )

        flaky_map = {}
        for flaky_test, test_run in flakytests_query:
            flaky_map[test_run.name_sha] = {
                'id': test_run.id,
                'job_id': test_run.job_id,
                'build_id': test_run.job.build_id,
                'project_id': test_run.project_id,
                'name': test_run.short_name,
                'package': test_run.package,
                'hash': test_run.name_sha,
                'flaky_runs': flaky_test.flaky_runs,
                'passing_runs': flaky_test.passing_runs,
                'history': []
            }

        if flaky_map:
            history_query = subquery.filter(
                FlakyTestStat.date <= query_date,
                FlakyTestStat.date > (query_date - timedelta(days=CHART_DATA_LIMIT)),
                TestCase.name_sha.in_(flaky_map)
            )

            # Create dict with keys in range ]today-CHART_DATA_LIMIT, today]
            calendar = [query_date - timedelta(days=delta) for delta in range(0, CHART_DATA_LIMIT)]
            history = {}
            for day in calendar:
                history[str(day)] = {}

            # Insert stats in the dict
            for flaky_test, test_run in history_query:
                history[str(flaky_test.date)][test_run.name_sha] = {
                    'date': str(flaky_test.date),
                    'flaky_runs': flaky_test.flaky_runs,
                    'passing_runs': flaky_test.passing_runs
                }

            # For each test, generate its history array from global history dict
            for day in reversed(sorted(history)):
                default_data = {
                    'date': day,
                    'flaky_runs': 0,
                    'passing_runs': 0
                }
                for sha in flaky_map:
                    flaky_map[sha]['history'].append(history[day].get(sha, default_data))

        return [flaky_map[sha] for sha in flaky_map]

    def get_chart_data(self, project_id, query_date):
        calendar = db.session.query(
            func.generate_series(
                query_date - timedelta(days=CHART_DATA_LIMIT - 1),
                query_date,
                timedelta(days=1)
            ).label('day')
        ).subquery()

        historical_data = db.session.query(
            calendar.c.day,
            func.coalesce(func.sum(FlakyTestStat.flaky_runs), 0),
            func.coalesce(func.sum(FlakyTestStat.passing_runs), 0)
        ).outerjoin(
            FlakyTestStat, calendar.c.day == FlakyTestStat.date
        ).order_by(
            calendar.c.day.desc()
        ).group_by(
            calendar.c.day
        )

        chart_data = []
        for d, flaky_runs, passing_runs in historical_data:
            chart_data.append({
                'date': str(d.date()),
                'flaky_runs': flaky_runs,
                'passing_runs': passing_runs
            })

        return chart_data