from __future__ import absolute_import

from changes.backends.jenkins.generic_builder import JenkinsGenericBuilder
from .test_builder import BaseTestCase


class JenkinsGenericBuilderTest(BaseTestCase):
    builder_cls = JenkinsGenericBuilder
    builder_options = {
        'base_url': 'http://jenkins.example.com',
        'job_name': 'server',
        'script': 'py.test',
    }

    def test_get_job_parameters(self):
        build = self.create_build(self.project)
        job = self.create_job(build)

        builder = self.get_builder()

        result = builder.get_job_parameters(job)
        assert {'name': 'CHANGES_BID', 'value': job.id.hex} in result
        assert {'name': 'CHANGES_PID', 'value': job.project.slug} in result
        assert {'name': 'REPO_URL', 'value': job.project.repository.url} in result
        assert {'name': 'REPO_VCS', 'value': job.project.repository.backend.name} in result
        assert {'name': 'REVISION', 'value': job.source.revision_sha} in result
        assert {'name': 'SCRIPT', 'value': self.builder_options['script']} in result
        assert len(result) == 6