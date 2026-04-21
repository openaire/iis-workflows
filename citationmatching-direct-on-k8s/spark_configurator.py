import json
import os
import sys
from typing import Any

from airflow.models import Variable
from airflow.operators.empty import EmptyOperator

package_dir = os.path.dirname(os.path.abspath(__file__))

# Ensure it's in sys.path
if package_dir not in sys.path:
    sys.path.append(package_dir)

import dag_utils
from task_decorators import SparkKubernetesAppOperator, SparkSubmitAppOperator

env_template = json.loads(Variable.get("spark_template", default_var="{}"))
spark_provider = Variable.get("spark_provider", default_var="operator")


def generate_spark_operator(
        task_id: str,
        main_class: str,
        arguments: list[str],
        jar: str = None,
        task_display_name: str = '',
        spark_extra_conf: dict[str, str] = None,
        image: str = None,
        py_files: list[str] = None,
) -> Any:
    """Factory for a Spark Operator for JVM (class name) or PySpark (.py path) apps.

    For JVM apps, jar defaults to the dhp shaded jar when not provided.
    For PySpark apps, jar is optional: supply it only when the script depends on
    Java/Scala code (e.g. custom UDFs) that must be on the executor classpath.

    Delegates all spec-building logic to SparkKubernetesAppOperator or
    SparkSubmitAppOperator based on the spark_provider Airflow Variable.
    """
    shared_kwargs = dict(
        task_id=task_id,
        task_display_name=task_display_name,
        jar=jar,
        main_class=main_class,
        arguments=arguments,
        spark_extra_conf=spark_extra_conf,
        image=image,
        py_files=py_files,
    )

    match spark_provider:
        case "operator":
            return SparkKubernetesAppOperator(python_callable=lambda: {}, **shared_kwargs)
        case "submit":
            return SparkSubmitAppOperator(python_callable=lambda: {}, **shared_kwargs)
        case _:
            return EmptyOperator(task_id=task_id, task_display_name=task_display_name)


def java_action(
        task_id: str,
        main_class: str,
        arguments: list[str],
        task_display_name: str = '',
        jar: str = dag_utils.get_dhp_jar(),
) -> Any:
    """Generate an Operator for a Java class wrapped via RunJavaSparkJob."""
    return generate_spark_operator(
        task_id,
        "eu.dnetlib.dhp.oozie.RunJavaSparkJob",
        [main_class] + arguments,
        task_display_name=task_display_name,
        jar=jar,
        spark_extra_conf={
            "spark.dynamicAllocation.enabled": "false",
            "spark.dynamicAllocation.minExecutors": "0",
            "spark.dynamicAllocation.maxExecutors": "1"
        }
    )


def generate_pyspark_task(
        task_id: str,
        script: str,
        arguments: list[str],
        task_display_name: str = '',
        spark_extra_conf: dict[str, str] = None,
        image: str = None,
        py_files: list[str] = None,
        jar: str = None,
) -> Any:
    """Generate a PySpark Operator.

    Args:
        script:   Path to the .py entry-point (single file or package main).
        py_files: Additional .zip/.egg/.py files for packaged module dependencies.
        jar:      Optional JAR to add to the executor classpath. Use when the
                  Python script depends on Java/Scala code (e.g. custom UDFs).
    """
    return generate_spark_operator(
        task_id,
        main_class=script,
        arguments=arguments,
        jar=jar,
        task_display_name=task_display_name,
        spark_extra_conf=spark_extra_conf,
        image=image,
        py_files=py_files,
    )
