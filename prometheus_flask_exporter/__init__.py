import os
import inspect
import warnings
import functools
import threading
from timeit import default_timer

from flask import request, make_response, current_app
from flask import Flask, Response
from werkzeug.serving import is_running_from_reloader
from prometheus_client import Counter, Histogram, Gauge, Summary
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

NO_PREFIX = '#no_prefix'
"""
Constant indicating that default metrics should not have any prefix applied.
It purposely uses invalid characters defined for metrics names as specified in Prometheus
documentation (see: https://prometheus.io/docs/concepts/data_model/#metric-names-and-labels)
"""


class PrometheusMetrics(object):
    """
    Prometheus metrics export configuration for Flask.

    The default metrics include a Histogram for HTTP request latencies
    and number of HTTP requests plus a Counter for the total number
    of HTTP requests.

    Sample usage:

        app = Flask(__name__)
        metrics = PrometheusMetrics(app)

        # static information as metric
        metrics.info('app_info', 'Application info', version='1.0.3')

        @app.route('/')
        def main():
            pass  # requests tracked by default

        @app.route('/skip')
        @metrics.do_not_track()
        def skip():
            pass  # default metrics are not collected

        @app.route('/<item_type>')
        @metrics.do_not_track()
        @metrics.counter('invocation_by_type', 'Number of invocations by type',
                 labels={'item_type': lambda: request.view_args['type']})
        def by_type(item_type):
            pass  # only the counter is collected, not the default metrics

        @app.route('/long-running')
        @metrics.gauge('in_progress', 'Long running requests in progress')
        def long_running():
            pass

        @app.route('/status/<int:status>')
        @metrics.do_not_track()
        @metrics.summary('requests_by_status', 'Request latencies by status',
                         labels={'status': lambda r: r.status_code})
        @metrics.histogram('requests_by_status_and_path', 'Request latencies by status and path',
                           labels={'status': lambda r: r.status_code, 'path': lambda: request.path})
        def echo_status(status):
            return 'Status: %s' % status, status

    Label values can be defined as callables:

        - With a single argument that will be the Flask Response object
        - Without an argument, possibly to use with the Flask `request` object
    """

    def __init__(self, app, path='/metrics',
                 export_defaults=True, defaults_prefix='flask',
                 group_by='path', buckets=None,
                 registry=None, **kwargs):
        """
        Create a new Prometheus metrics export configuration.

        :param app: the Flask application
        :param path: the metrics path (defaults to `/metrics`)
        :param export_defaults: expose all HTTP request latencies
            and number of HTTP requests
        :param defaults_prefix: string to prefix the default exported
            metrics name with (when either `export_defaults=True` or
            `export_defaults(..)` is called) or in case you don't want
            any prefix then use `NO_PREFIX` constant
        :param group_by: group default HTTP metrics by
            this request property, like `path`, `endpoint`, `url_rule`, etc.
            (defaults to `path`)
        :param buckets: the time buckets for request latencies
            (will use the default when `None`)
        :param registry: the Prometheus Registry to use
        """

        self.app = app
        self.path = path
        self._export_defaults = export_defaults
        self._defaults_prefix = defaults_prefix or 'flask'
        self.buckets = buckets
        self.version = __version__

        if registry:
            self.registry = registry
        else:
            # load the default registry from the underlying
            # Prometheus library here for easier unit testing
            # see https://github.com/rycus86/prometheus_flask_exporter/pull/20
            from prometheus_client import REGISTRY as DEFAULT_REGISTRY
            self.registry = DEFAULT_REGISTRY

        if kwargs.get('group_by_endpoint') is True:
            warnings.warn(
                'The `group_by_endpoint` argument of `PrometheusMetrics` is '
                'deprecated since 0.4.0, please use the '
                'new `group_by` argument.', DeprecationWarning
            )

            self.group_by = 'endpoint'

        elif group_by:
            self.group_by = group_by

        else:
            self.group_by = 'path'

        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """
        This callback can be used to initialize an application for the
        use with this prometheus reporter setup.

        This is usually used with a flask "app factory" configuration. Please
        see: http://flask.pocoo.org/docs/1.0/patterns/appfactories/

        Note, that you need to use `PrometheusMetrics(app=None, ...)`
        for this mode, otherwise it is called automatically.

        :param app: the Flask application
        """

        if self.path:
            self.register_endpoint(self.path, app)

        if self._export_defaults:
            self.export_defaults(
                self.buckets, self.group_by,
                self._defaults_prefix, app
            )

    def register_endpoint(self, path, app=None):
        """
        Register the metrics endpoint on the Flask application.

        :param path: the path of the endpoint
        :param app: the Flask application to register the endpoint on
            (by default it is the application registered with this class)
        """

        if is_running_from_reloader() and not os.environ.get('DEBUG_METRICS'):
            return

        if app is None:
            app = self.app or current_app

        @app.route(path)
        @self.do_not_track()
        def prometheus_metrics():
            # import these here so they don't clash with our own multiprocess module
            from prometheus_client import multiprocess, CollectorRegistry

            if 'prometheus_multiproc_dir' in os.environ:
                registry = CollectorRegistry()
            else:
                registry = self.registry

            if 'name[]' in request.args:
                registry = registry.restricted_registry(request.args.getlist('name[]'))

            if 'prometheus_multiproc_dir' in os.environ:
                multiprocess.MultiProcessCollector(registry)

            headers = {'Content-Type': CONTENT_TYPE_LATEST}
            return generate_latest(registry), 200, headers

    def start_http_server(self, port, host='0.0.0.0', endpoint='/metrics'):
        """
        Start an HTTP server for exposing the metrics.
        This will be an individual Flask application,
        not the one registered with this class.

        :param port: the HTTP port to expose the metrics endpoint on
        :param host: the HTTP host to listen on (default: `0.0.0.0`)
        :param endpoint: the URL path to expose the endpoint on
            (default: `/metrics`)
        """

        if is_running_from_reloader():
            return

        app = Flask('prometheus-flask-exporter-%d' % port)
        self.register_endpoint(endpoint, app)

        def run_app():
            app.run(host=host, port=port)

        thread = threading.Thread(target=run_app)
        thread.setDaemon(True)
        thread.start()

    def export_defaults(self, buckets=None, group_by='path',
                        prefix='flask', app=None, **kwargs):
        """
        Export the default metrics:
            - HTTP request latencies
            - Number of HTTP requests

        :param buckets: the time buckets for request latencies
            (will use the default when `None`)
        :param group_by: group default HTTP metrics by
            this request property, like `path`, `endpoint`, `rule`, etc.
            (defaults to `path`)
        :param prefix: prefix to start the default metrics names with
            or `NO_PREFIX` (to skip prefix)
        :param app: the Flask application
        """

        if app is None:
            app = self.app or current_app

        if not prefix:
            prefix = self._defaults_prefix or 'flask'

        # use the default buckets from prometheus_client if not given here
        buckets_as_kwargs = {}
        if buckets is not None:
            buckets_as_kwargs['buckets'] = buckets

        if kwargs.get('group_by_endpoint') is True:
            warnings.warn(
                'The `group_by_endpoint` argument of '
                '`PrometheusMetrics.export_defaults` is deprecated since 0.4.0, '
                'please use the new `group_by` argument.', DeprecationWarning
            )

            duration_group = 'endpoint'

        elif group_by:
            duration_group = group_by

        else:
            duration_group = 'path'

        if callable(duration_group):
            duration_group_name = duration_group.__name__

        else:
            duration_group_name = duration_group

        if prefix == NO_PREFIX:
            prefix = ""
        else:
            prefix = prefix + "_"

        hostname = os.getenv('HOSTNAME', 'bayesian-api')
        # Add gauge metrics for our average calculations
        # Gauge by default considers pid for labeling for multiprocess_mode in (all, liveall).
        gauge = Gauge(
            '%shttp_request_average' % prefix,
            'Average Response Time of HTTP requests',
            ('method', duration_group_name, 'hostname', 'status'),
            registry=self.registry, multiprocess_mode='liveall'
        )
        
        # We need to extend pid labeling to our Histogram as well
        histogram = Histogram(
            '%shttp_request_duration_seconds' % prefix,
            'Flask HTTP request duration in seconds',
            ('method', duration_group_name, 'pid', 'hostname', 'status'),
            registry=self.registry,
            **buckets_as_kwargs
        )

        # Add group by endpoint or path for our Counter metrics
        counter = Counter(
            '%shttp_request_total' % prefix,
            'Total number of HTTP requests',
            ('method', duration_group_name, 'hostname', 'status'),
            registry=self.registry
        )

        self.info(
            '%sexporter_info' % prefix,
            'Information about the Prometheus Flask exporter',
            version=self.version
        )

        def before_request():
            request.prom_start_time = default_timer()

        def after_request(response):
            if hasattr(request, 'prom_do_not_track'):
                return response

            if hasattr(request, 'prom_start_time'):
                total_time = max(default_timer() - request.prom_start_time, 0)

                if callable(duration_group):
                    group = duration_group(request)
                else:
                    group = getattr(request, duration_group)

                histogram.labels(
                    request.method, group, os.getpid(), hostname, response.status_code
                ).observe(total_time)

            counter.labels(request.method, group, hostname, response.status_code).inc()
            
            # Get moving average data per (endpoint, method_tye, status_code, pid)
            total_count = 1
            total_time = 0
            for sample in histogram._samples():
                """Data Format
                By Path
                ('_sum', {'method': 'GET', 'path': '/api/v1/ping', 'status': '200'}, 0.00014503102283924818)
                By Endpoint
                ('_count', {'method': 'GET', 'endpoint': 'api_v1.ping', 'status': '200'}, 1.0)
                """
                if '_sum' in sample:
                    if request.method == sample[1]['method'] and \
                                    group == sample[1][duration_group_name] and \
                                    str(os.getpid()) == sample[1]['pid'] and \
                                    str(hostname) == sample[1]['hostname']:
                        total_time = sample[2]
                if '_count' in sample:
                    if request.method == sample[1]['method'] and \
                                    str(os.getpid()) == sample[1]['pid'] and \
                                    str(hostname) == sample[1]['hostname'] and \
                                    group == sample[1][duration_group_name]:
                        total_count = sample[2]
                        
            if total_time > 0 and total_count > 0:
                average_time = float(total_time / total_count)
                # Gauge by default aggregates based on PID if multiprocess_mode in (all, liveall)
                gauge.labels(request.method, group, hostname,
                             response.status_code).set(average_time)

            return response

        app.before_request(before_request)
        app.after_request(after_request)

    def histogram(self, name, description, labels=None, **kwargs):
        """
        Use a Histogram to track the execution time and invocation count
        of the method.

        :param name: the name of the metric
        :param description: the description of the metric
        :param labels: a dictionary of `{labelname: callable_or_value}` for labels
        :param kwargs: additional keyword arguments for creating the Histogram
        """

        return self._track(
            Histogram,
            lambda metric, time: metric.observe(time),
            kwargs, name, description, labels,
            registry=self.registry
        )

    def summary(self, name, description, labels=None, **kwargs):
        """
        Use a Summary to track the execution time and invocation count
        of the method.

        :param name: the name of the metric
        :param description: the description of the metric
        :param labels: a dictionary of `{labelname: callable_or_value}` for labels
        :param kwargs: additional keyword arguments for creating the Summary
        """

        return self._track(
            Summary,
            lambda metric, time: metric.observe(time),
            kwargs, name, description, labels,
            registry=self.registry
        )

    def gauge(self, name, description, labels=None, **kwargs):
        """
        Use a Gauge to track the number of invocations in progress
        for the method.

        :param name: the name of the metric
        :param description: the description of the metric
        :param labels: a dictionary of `{labelname: callable_or_value}` for labels
        :param kwargs: additional keyword arguments for creating the Gauge
        """

        return self._track(
            Gauge,
            lambda metric, time: metric.dec(),
            kwargs, name, description, labels,
            registry=self.registry,
            before=lambda metric: metric.inc()
        )

    def counter(self, name, description, labels=None, **kwargs):
        """
        Use a Counter to track the total number of invocations of the method.

        :param name: the name of the metric
        :param description: the description of the metric
        :param labels: a dictionary of `{labelname: callable_or_value}` for labels
        :param kwargs: additional keyword arguments for creating the Counter
        """

        return self._track(
            Counter,
            lambda metric, time: metric.inc(),
            kwargs, name, description, labels,
            registry=self.registry
        )

    @staticmethod
    def _track(metric_type, metric_call, metric_kwargs, name, description, labels,
               registry, before=None):
        """
        Internal method decorator logic.

        :param metric_type: the type of the metric from the `prometheus_client` library
        :param metric_call: the invocation to execute as a callable with `(metric, time)`
        :param metric_kwargs: additional keyword arguments for creating the metric
        :param name: the name of the metric
        :param description: the description of the metric
        :param labels: a dictionary of `{labelname: callable_or_value}` for labels
        :param before: an optional callable to invoke before executing the
            request handler method accepting the single `metric` argument
        :param registry: the Prometheus Registry to use
        """

        if labels is not None and not isinstance(labels, dict):
            raise TypeError('labels needs to be a dictionary of {labelname: callable}')

        label_names = labels.keys() if labels else tuple()
        parent_metric = metric_type(
            name, description, labelnames=label_names, registry=registry,
            **metric_kwargs
        )

        def argspec(func):
            if hasattr(inspect, 'getfullargspec'):
                return inspect.getfullargspec(func)
            else:
                return inspect.getargspec(func)

        def label_value(f):
            if not callable(f):
                return lambda x: f
            if argspec(f).args:
                return lambda x: f(x)
            else:
                return lambda x: f()

        label_generator = tuple(
            (key, label_value(call))
            for key, call in labels.items()
        ) if labels else tuple()

        def get_metric(response):
            if label_names:
                return parent_metric.labels(
                    **{key: call(response) for key, call in label_generator}
                )
            else:
                return parent_metric

        def decorator(f):
            @functools.wraps(f)
            def func(*args, **kwargs):
                if before:
                    metric = get_metric(None)
                    before(metric)

                else:
                    metric = None

                exception = None

                start_time = default_timer()
                try:
                    try:
                        # execute the handler function
                        response = f(*args, **kwargs)
                    except Exception as ex:
                        # let Flask decide to wrap or reraise the Exception
                        response = current_app.handle_user_exception(ex)
                except Exception as ex:
                    # if it was re-raised, treat it as an InternalServerError
                    exception = ex
                    response = make_response('Exception: %s' % ex, 500)

                total_time = max(default_timer() - start_time, 0)

                if not metric:
                    if not isinstance(response, Response) and request.endpoint:
                        view_func = current_app.view_functions[request.endpoint]

                        # There may be decorators 'above' us,
                        # but before the function is registered with Flask
                        while view_func and view_func != f:
                            try:
                                view_func = view_func.__wrapped__
                            except AttributeError:
                                break

                        if view_func == f:
                            # we are in a request handler method
                            response = make_response(response)

                    metric = get_metric(response)

                metric_call(metric, time=total_time)

                if exception:
                    try:
                        # re-raise for the Flask error handler
                        raise exception
                    except Exception as ex:
                        return current_app.handle_user_exception(ex)

                else:
                    return response

            return func

        return decorator

    @staticmethod
    def do_not_track():
        """
        Decorator to skip the default metrics collection for the method.

        *Note*: explicit metrics decorators will still collect the data
        """

        def decorator(f):
            @functools.wraps(f)
            def func(*args, **kwargs):
                request.prom_do_not_track = True
                return f(*args, **kwargs)

            return func

        return decorator

    def info(self, name, description, labelnames=None, labelvalues=None, **labels):
        """
        Report any information as a Prometheus metric.
        This will create a `Gauge` with the initial value of 1.

        The easiest way to use it is:

            metrics = PrometheusMetrics(app)
            metrics.info(
                'app_info', 'Application info',
                version='1.0', major=1, minor=0
            )

        If the order of the labels matters:

            metrics = PrometheusMetrics(app)
            metrics.info(
                'app_info', 'Application info',
                ('version', 'major', 'minor'),
                ('1.0', 1, 0)
            )

        :param name: the name of the metric
        :param description: the description of the metric
        :param labelnames: the names of the labels
        :param labelvalues: the values of the labels
        :param labels: the names and values of the labels
        :return: the newly created `Gauge` metric
        """

        if labels and labelnames:
            raise ValueError(
                'Cannot have labels defined as `dict` '
                'and collections of names and values'
            )

        if labelnames is None and labels:
            labelnames = labels.keys()

        elif labelnames and labelvalues:
            for idx, label_name in enumerate(labelnames):
                labels[label_name] = labelvalues[idx]

        gauge = Gauge(
            name, description, labelnames or tuple(),
            registry=self.registry
        )

        if labels:
            gauge = gauge.labels(**labels)

        gauge.set(1)

        return gauge


__version__ = '0.8.1'
