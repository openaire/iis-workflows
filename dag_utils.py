from typing import Sequence

from airflow.models import Variable
from airflow.models.param import Param
from airflow.models.taskmixin import DependencyMixin
from airflow.exceptions import AirflowSkipException

def should_skip(task, dag_run) -> bool:
    """
    Decide whether to skip this task, based on dag_run.conf['start_from'].
    - If no start_from given → run everything.
    - If start_from == this task_id → run this task and downstream.
    - If this task is upstream of start_from → skip.
    - Otherwise → run.
    """
    if not dag_run or not dag_run.conf:
        return False

    # Skip option for this task?
    skip_option = "SKIP_" + task.task_id.upper()
    if dag_run.conf.get(skip_option, False):
        return True

    start_from = dag_run.conf.get("START_FROM")
    if start_from:
        if task.task_id == start_from:
            return False

        # Get all upstream tasks of the "start_from" task
        start_task = task.dag.get_task(start_from)
        upstream_of_start = {t.task_id for t in start_task.get_flat_relatives(upstream=True)}

        if task.task_id in upstream_of_start:
            return True

    stop_at = dag_run.conf.get("STOP_AT")
    if stop_at:
        if task.task_id == stop_at:
            return True

        # Get all upstream tasks of the "start_from" task
        stop_task = task.dag.get_task(stop_at)
        downstream_of_stop = {t.task_id for t in stop_task.get_flat_relatives(upstream=False)}

        if task.task_id in downstream_of_stop:
            return True

    return False


def skip_if_needed(fn):
    """
    Decorator for @task functions.
    If should_skip() says skip → raise AirflowSkipException.
    """
    def wrapper(*args, **kwargs):
        context = kwargs.get("context") or {}
        task = context["task"]
        dag_run = context["dag_run"]

        if should_skip(task, dag_run):
            raise AirflowSkipException(f"Skipping {task.task_id} (START_FROM={dag_run.conf.get('START_FROM')})")

        return fn(*args, **kwargs)

    return wrapper

def auto_skip(task):
    """
    Wrap any operator so it respects `start_from`.
    Also updates downstream trigger_rules to tolerate skipped tasks.
    """

    def _pre_execute(context):
        dag_run = context["dag_run"]
        if should_skip(task, dag_run):
            raise AirflowSkipException(
                f"Skipping {task.task_id} (START_FROM={dag_run.conf.get('START_FROM')})"
            )

    if hasattr(task, 'iter_references'):
        for op, name in task.iter_references():
            auto_skip(op)


    if hasattr(task, 'pre_execute'):
        # Chain pre_execute
        if task.pre_execute:
            original = task.pre_execute

            def chained_pre_execute(context):
                _pre_execute(context)
                return original(context)

            task.pre_execute = chained_pre_execute
        else:
            task.pre_execute = _pre_execute

        # Adjust this and downstream trigger rules
        if task.trigger_rule == "all_success":
            task.trigger_rule = "none_failed"

        for downstream in task.get_direct_relatives(upstream=False):
            if downstream.trigger_rule == "all_success":
                downstream.trigger_rule = "none_failed"

    return task

def dag_add_skipping(dag):
    for t in dag.tasks:
        auto_skip(t)

    if "START_FROM" in dag.params:
        dag.params["START_FROM"] = Param(
            default="",
            type=["string", "null"],
            enum=[""] + [t.task_id for t in dag.tasks],
            description="Start DAG execution from this task (skip all upstream tasks).",
        )

    if "STOP_AT" in dag.params:
        dag.params["STOP_AT"] = Param(
            default="",
            type=["string", "null"],
            enum=[""] + [t.task_id for t in dag.tasks],
            description="Stop DAG execution at this task (skip it and all downstream tasks).",
)


def merge_dicts(*dicts):
    """
    Merge multiple dictionaries into one.

    The entries in the earlier dicts win on the later,
    including a deep view of the dicts. If a sub-dict has a missing key,
    it will be filled with existing key's value from an earlier dict.

    :param dicts: Variable number of dictionaries to merge.
    :return: Merged dictionary.
    """
    result = {}
    for d in reversed(dicts):
        for key, value in d.items():
            if isinstance(value, dict) and key in result and isinstance(result[key], dict):
                # If the key already exists as a nested dict,
                # recursively call merge_dicts on these two sub-dicts
                result[key] = merge_dicts( value, result[key])
            else:
                result[key] = value
    return result

def get_fs_prefix():
    return Variable.get("default_fs", default_var="")

def get_dhp_jar():
    return Variable.get("default_jar", default_var=f"{get_fs_prefix()}/binaries/dhp-shade-package-1.2.5-SNAPSHOT.jar")

from airflow.models.baseoperator import chain

def chain_sequence(start, task_groups, end):
    """
    Creates a dependency chain where:
    - `start` triggers all first tasks of each task group.
    - Each task group runs sequentially.
    - `end` waits for the last task of each task group to complete.

    :param start: The starting task/operator.
    :param task_groups: A list of lists, where each inner list is a sequence of tasks.
    :param end: The ending task/operator.
    """
    first_tasks = [tg[0] for tg in task_groups]  # First task in each group
    last_tasks = [tg[-1] for tg in task_groups]  # Last task in each group

    # Start triggers all first tasks
    chain(start, first_tasks)

    # Chain each task group internally
    for tg in task_groups:
        chain(*tg)

    # Last tasks trigger the end
    chain(last_tasks, end)

def chain_sequence(*tasks: DependencyMixin | Sequence[DependencyMixin]) -> None:
    for up_task, down_task in zip(tasks, tasks[1:]):
        up_task_list = up_task
        down_task_list = down_task
        if isinstance(up_task, DependencyMixin):
            up_task_list = [up_task]
        if isinstance(down_task, DependencyMixin):
            down_task_list = [down_task]

        for down_t in down_task_list:
            if isinstance(down_t, Sequence):
                chain(*down_t)
                down_t = down_t[0]
            for up_t in up_task_list:
                if isinstance(up_t, Sequence):
                    chain(*up_t)
                    up_t = up_t[-1]
                up_t.set_downstream(down_t)

