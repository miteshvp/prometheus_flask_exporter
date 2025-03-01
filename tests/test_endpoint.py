import time

import werkzeug.exceptions
from flask import request, abort
from unittest_helper import BaseTestCase

try:
    from urllib2 import urlopen
except ImportError:
    # Python 3
    from urllib.request import urlopen


class EndpointTest(BaseTestCase):
    def test_restricted(self):
        self.metrics()

        @self.app.route('/test')
        def test():
            return 'OK'

        self.client.get('/test')

        response = self.client.get('/metrics')

        self.assertIn('flask_exporter_info', str(response.data))
        self.assertIn('flask_http_request_total', str(response.data))
        self.assertIn('flask_http_request_duration_seconds', str(response.data))

        response = self.client.get('/metrics?name[]=flask_exporter_info')

        self.assertIn('flask_exporter_info', str(response.data))
        self.assertNotIn('flask_http_request_total', str(response.data))
        self.assertNotIn('flask_http_request_duration_seconds', str(response.data))

        response = self.client.get('/metrics'
                                   '?name[]=flask_http_request_duration_seconds_bucket'
                                   '&name[]=flask_http_request_duration_seconds_count'
                                   '&name[]=flask_http_request_duration_seconds_sum')

        self.assertNotIn('flask_exporter_info', str(response.data))
        self.assertNotIn('flask_http_request_total', str(response.data))
        self.assertIn('flask_http_request_duration_seconds_bucket', str(response.data))
        self.assertIn('flask_http_request_duration_seconds_count', str(response.data))
        self.assertIn('flask_http_request_duration_seconds_sum', str(response.data))

    def test_http_server(self):
        metrics = self.metrics()

        metrics.start_http_server(32001)
        metrics.start_http_server(32002, endpoint='/test/metrics')
        metrics.start_http_server(32003, host='127.0.0.1')

        def wait_for_startup():
            for _ in range(10):
                try:
                    urlopen('http://localhost:32001/metrics')
                    urlopen('http://localhost:32002/test/metrics')
                    urlopen('http://localhost:32003/metrics')
                    break
                except:
                    time.sleep(0.5)

        wait_for_startup()

        response = urlopen('http://localhost:32001/metrics')

        self.assertEqual(response.getcode(), 200)
        self.assertIn('flask_exporter_info', str(response.read()))

        response = urlopen('http://localhost:32002/test/metrics')

        self.assertEqual(response.getcode(), 200)
        self.assertIn('flask_exporter_info', str(response.read()))

        response = urlopen('http://localhost:32003/metrics')

        self.assertEqual(response.getcode(), 200)
        self.assertIn('flask_exporter_info', str(response.read()))

    def test_abort(self):
        metrics = self.metrics()

        @self.app.route('/error')
        @metrics.summary('http_index_requests_by_status',
                         'Request latencies by status',
                         labels={'status': lambda r: r.status_code})
        @metrics.histogram('http_index_requests_by_status_and_path',
                           'Index requests latencies by status and path',
                           labels={
                               'status': lambda r: r.status_code,
                               'path': lambda: request.path
                           })
        def throw_error():
            return abort(503)

        self.client.get('/error')

        self.assertMetric('http_index_requests_by_status_count', 1.0, ('status', 503))
        self.assertMetric('http_index_requests_by_status_sum', '.', ('status', 503))

        self.assertMetric(
            'http_index_requests_by_status_and_path_count', 1.0,
            ('status', 503), ('path', '/error')
        )
        self.assertMetric(
            'http_index_requests_by_status_and_path_sum', '.',
            ('status', 503), ('path', '/error')
        )
        self.assertMetric(
            'http_index_requests_by_status_and_path_bucket', 1.0,
            ('status', 503), ('path', '/error'), ('le', 0.5)
        )
        self.assertMetric(
            'http_index_requests_by_status_and_path_bucket', 1.0,
            ('status', 503), ('path', '/error'), ('le', 10.0)
        )

    def test_exception(self):
        metrics = self.metrics()

        @self.app.route('/exception')
        @metrics.summary('http_with_exception',
                         'Tracks the method raising an exception',
                         labels={'status': lambda r: r.status_code})
        def raise_exception():
            raise NotImplementedError('On purpose')

        try:
            self.client.get('/exception')
        except NotImplementedError:
            pass

        self.assertMetric('http_with_exception_count', 1.0, ('status', 500))
        self.assertMetric('http_with_exception_sum', '.', ('status', 500))

    def test_abort_before(self):
        @self.app.before_request
        def before_request():
            if request.path == '/metrics':
                return

            raise abort(400)

        self.metrics()

        self.client.get('/abort/before')
        self.client.get('/abort/before')

        self.assertMetric(
            'flask_http_request_total', 2.0,
            ('method', 'GET'), ('status', 400)
        )

    def test_error_handler(self):
        metrics = self.metrics()

        @self.app.errorhandler(NotImplementedError)
        def not_implemented_handler(e):
            return 'Not implemented', 400

        @self.app.errorhandler(werkzeug.exceptions.Conflict)
        def handle_conflict(e):
            return 'Bad request for conflict', 400, {'X-Original': e.code}

        @self.app.route('/exception')
        @metrics.summary('http_with_exception',
                         'Tracks the method raising an exception',
                         labels={'status': lambda r: r.status_code})
        def raise_exception():
            raise NotImplementedError('On purpose')

        @self.app.route('/conflict')
        @metrics.summary('http_with_code',
                         'Tracks the error with the original code',
                         labels={'status': lambda r: r.status_code,
                                 'code': lambda r: r.headers.get('X-Original', -1)})
        def conflicts():
            abort(409)

        for _ in range(3):
            self.client.get('/exception')

        for _ in range(7):
            self.client.get('/conflict')

        self.assertMetric('http_with_exception_count', 3.0, ('status', 400))
        self.assertMetric('http_with_exception_sum', '.', ('status', 400))

        self.assertMetric('http_with_code_count', 7.0, ('status', 400), ('code', 409))
        self.assertMetric('http_with_code_sum', '.', ('status', 400), ('code', 409))

    def test_named_endpoint(self):
        metrics = self.metrics()

        @self.app.route('/testing', endpoint='testing_endpoint')
        @metrics.summary('requests_by_status',
                         'Request latencies by status',
                         labels={'status': lambda r: r.status_code})
        def testing():
            return 'OK'

        for _ in range(5):
            self.client.get('/testing')

        self.assertMetric('requests_by_status_count', 5.0, ('status', 200))
        self.assertMetric('requests_by_status_sum', '.', ('status', 200))
