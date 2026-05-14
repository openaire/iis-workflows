import dag_utils
import json

from typing import TYPE_CHECKING, Callable
from airflow.models import Variable
from airflow.sdk.bases.decorator import task_decorator_factory
from airflow.providers.standard.operators.empty import EmptyOperator

from airflow.providers.cncf.kubernetes.operators.spark_kubernetes import SparkKubernetesOperator

try:
    from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
except ImportError:
    SparkSubmitOperator = None

if TYPE_CHECKING:
    from airflow.sdk.bases.decorator import TaskDecorator

spark_provider = Variable.get("spark_provider", default_var="operator")
env_template = json.loads(Variable.get("spark_template", default_var="{}"))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_config(config: dict, defaults: dict) -> dict:
    """Merge callable return value over constructor defaults (callable wins).

    Returns a dict with all resolved fields plus two derived helpers:
        is_python     — True when main_class ends with .py
        effective_jar — explicit jar, or dhp default for JVM, or None for Python
    """
    r = {
        "jar":              config.get("jar",              defaults.get("jar")),
        "main_class":       config.get("main_class",       defaults.get("main_class", "")),
        "arguments":        config.get("arguments",        defaults.get("arguments", [])) or [],
        "spark_extra_conf": config.get("spark_extra_conf", defaults.get("spark_extra_conf", {})) or {},
        "image":            config.get("image",            defaults.get("image")),
        "py_files":         config.get("py_files",         defaults.get("py_files", [])) or [],
    }
    r["is_python"]     = r["main_class"].endswith(".py")
    r["effective_jar"] = r["jar"] or (None if r["is_python"] else dag_utils.get_dhp_jar())
    return r


def _build_k8s_spec(task_id: str, r: dict) -> dict:
    """Build a SparkApplication template_spec dict from a resolved config dict."""
    if r["is_python"]:
        deps = {}
        if r["py_files"]:
            deps["pyFiles"] = r["py_files"]
        if r["effective_jar"]:
            deps["jars"] = [r["effective_jar"]]
        spec = {
            "type": "Python",
            "mainApplicationFile": r["main_class"],
            "arguments": r["arguments"],
        }
        if deps:
            spec["deps"] = deps
    else:
        spec = {
            "mainClass":           r["main_class"],
            "mainApplicationFile": r["effective_jar"],
            "deps":                {"jars": [r["effective_jar"]]},
            "arguments":           r["arguments"],
        }

    template_spec = dag_utils.merge_dicts(
        {
            "metadata": {"name": task_id.lower() + "-{{ ds }}-{{ task_instance.try_number }}"},
            "spec": spec,
        },
        {"spec": {"sparkConf": r["spark_extra_conf"]}},
        env_template,
    )

    if r["image"]:
        template_spec["spec"]["image"] = r["image"]

    return template_spec


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class SparkKubernetesAppOperator(SparkKubernetesOperator):
    """Spark-on-Kubernetes operator with deferred config resolution.

    All Spark parameters (jar, main_class, arguments, …) can be set as
    constructor arguments or overridden at runtime by the python_callable's
    return value.  The callable is invoked inside execute() so Jinja template
    variables and Airflow context are fully resolved at that point.
    """

    def __init__(
            self,
            python_callable: Callable,
            op_args=None,
            op_kwargs=None,
            jar: str = None,
            main_class: str = "",
            arguments: list = [],
            spark_extra_conf: dict[str, str] = None,
            image: str = None,
            py_files: list[str] = None,
            **kwargs,
    ):
        self._python_callable = python_callable
        self._op_args = op_args or []
        self._op_kwargs = op_kwargs or {}
        self._defaults = {
            "jar":              jar,
            "main_class":       main_class,
            "arguments":        arguments,
            "spark_extra_conf": spark_extra_conf or {},
            "image":            image,
            "py_files":         py_files or [],
        }

        # Minimal placeholder so the parent can initialise cleanly.
        # execute() will replace template_spec and namespace with real values.
        placeholder_spec = dag_utils.merge_dicts({
            "metadata": {
                "name": kwargs.get("task_id", "spark-task").lower()
                        + "-{{ ds }}-{{ task_instance.try_number }}"
            },
            "spec": {},
        }, env_template)

        super().__init__(
            namespace=placeholder_spec.get("metadata", {}).get("namespace", "default"),
            template_spec=placeholder_spec,
            kubernetes_conn_id="kubernetes_default",
            log_events_on_failure=True,
            delete_on_termination=True,
            deferrable=False,
            **kwargs,
        )
        # All templating uses inline {{ }} expressions, never file references.
        # Disable file-based template loading so that strings ending in .json
        # (e.g. classpath paths in `arguments`) are not mistakenly resolved as
        # Jinja template files from the DAG bundle filesystem.
        self.template_ext = ()

    def execute(self, context):
        config = self._python_callable(*self._op_args, **self._op_kwargs)
        if not isinstance(config, dict):
            raise TypeError("python_callable must return a dict")

        jinja_env = self.get_template_env()
        rendered_defaults = self.render_template(self._defaults, context, jinja_env)

        r = _resolve_config(config, rendered_defaults)
        template_spec = _build_k8s_spec(self.task_id, r)

        self.template_spec = self.render_template(template_spec, context, jinja_env)
        self.namespace = self.template_spec["metadata"]["namespace"]

        return super().execute(context)


_SparkSubmitBase = SparkSubmitOperator if SparkSubmitOperator is not None else object


class SparkSubmitAppOperator(_SparkSubmitBase):
    """spark-submit operator with deferred config resolution.

    Mirrors SparkKubernetesAppOperator's callable pattern for the spark-submit
    provider.  Real Spark parameters are written to instance variables inside
    execute() after the callable is invoked, so all runtime context is available.
    """

    def __init__(
            self,
            python_callable: Callable,
            op_args=None,
            op_kwargs=None,
            jar: str = None,
            main_class: str = "",
            arguments: list = [],
            spark_extra_conf: dict[str, str] = None,
            image: str = None,
            py_files: list[str] = None,
            **kwargs,
    ):
        self._python_callable = python_callable
        self._op_args = op_args or []
        self._op_kwargs = op_kwargs or {}
        self._defaults = {
            "jar":              jar,
            "main_class":       main_class,
            "arguments":        arguments,
            "spark_extra_conf": spark_extra_conf or {},
            "image":            image,
            "py_files":         py_files or [],
        }

        # application and conf are placeholders; execute() sets the real values
        # on the instance before delegating to super().execute().
        super().__init__(
            application="",
            name=kwargs.get("task_id", "spark-task").lower()
                 + "-{{ ds }}-{{ task_instance.try_number }}",
            conf=dag_utils.merge_dicts(spark_extra_conf or {}, env_template),
            verbose=True,
            **kwargs,
        )

    def execute(self, context):
        config = self._python_callable(*self._op_args, **self._op_kwargs)
        if not isinstance(config, dict):
            raise TypeError("python_callable must return a dict")

        jinja_env = self.get_template_env()
        rendered_defaults = self.render_template(self._defaults, context, jinja_env)

        r = _resolve_config(config, rendered_defaults)

        # SparkSubmitOperator reads these instance vars in _get_hook() / execute(),
        # so we overwrite them here with the fully-resolved values.
        if r["is_python"]:
            self.application = r["main_class"]
            self._java_class  = None
            self.py_files    = ",".join(r["py_files"]) if r["py_files"] else None
            self.jars        = r["effective_jar"]
        else:
            self.application = r["effective_jar"]
            self._java_class  = r["main_class"]
            self.py_files    = None
            self.jars        = r["effective_jar"]

        self.application_args = r["arguments"]
        self.conf = dag_utils.merge_dicts(r["spark_extra_conf"], env_template)

        # Perform Jinja substitution on all mutable fields, mirroring what
        # SparkKubernetesAppOperator does via render_template() before execute().
        rendered = self.render_template(
            {
                "application":      self.application,
                "java_class":       self._java_class,
                "application_args": self.application_args,
                "conf":             self.conf,
                "py_files":         self.py_files,
                "jars":             self.jars,
            },
            context,
            jinja_env,
        )
        self.application      = rendered["application"]
        self._java_class       = rendered["java_class"]
        self.application_args = rendered["application_args"]
        self.conf             = rendered["conf"]
        self.py_files         = rendered["py_files"]
        self.jars             = rendered["jars"]

        return super().execute(context)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

_OPERATOR_CLASSES = {
    "operator": SparkKubernetesAppOperator,
    "submit":   SparkSubmitAppOperator,
}


def sparkapp_task(
        python_callable: Callable | None = None,
        multiple_outputs: bool | None = None,
        **kwargs,
) -> "TaskDecorator":
    operator_class = _OPERATOR_CLASSES.get(spark_provider)
    if operator_class is None:
        raise ValueError(
            f"Unsupported spark_provider '{spark_provider}'. "
            f"Expected one of: {list(_OPERATOR_CLASSES)}"
        )
    return task_decorator_factory(
        python_callable=python_callable,
        multiple_outputs=multiple_outputs,
        decorated_operator_class=operator_class,
        **kwargs,
    )


# HACK: register a synthetic provider so Airflow's task-decorator registry
# picks up @task.sparkapp without a real provider package.
from airflow.providers_manager import ProvidersManager, ProviderInfo

pm = ProvidersManager()
pm.initialize_providers_list()
provider_info = {
    'package-name': 'sparkapp-airflow-providers',
    'name': 'SparkApp',
    'description': "sparkapp provider",
    'task-decorators': [
        {
            'name': 'sparkapp',
            'class-name': 'task_decorators.sparkapp_task',
        },
    ],
}
pm._provider_schema_validator.validate(provider_info)
pm._provider_dict['sparkapp-airflow-providers'] = ProviderInfo('0.0.1', provider_info)
pm._provider_dict = dict(sorted(pm._provider_dict.items()))
