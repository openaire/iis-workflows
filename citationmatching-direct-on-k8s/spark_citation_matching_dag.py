import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.hooks.base import BaseHook
from airflow.models.param import Param

package_dir = os.path.dirname(os.path.abspath(__file__))

# Ensure it's in sys.path
if package_dir not in sys.path:
    sys.path.append(package_dir)

import dag_utils
from spark_configurator import generate_spark_operator, java_action

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60)))
}

@dag(
    dag_id="spark_citation_matching_direct",
    dag_display_name="Performs citation matching direct algorithm matching publications by external ids",
    default_args=default_args,
    params={
        "JAR": Param(
            # --- dag_utils.get_dhp_jar(),
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-citationmatching-direct/1.3.0-SNAPSHOT/iis-wf-citationmatching-direct-1.3.0-20260421.112414-1-uber.jar",
            type='string',
            description="citation matching shaded jar"
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.1",
            type='string',
            description=""),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type='string',
            description=""),
        # --- I/O paths (most commonly overridden) ---
        "input": Param(
            default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/transformers_metadatamerger/output_merged_metadata",
            type="string",
            description="Input Avro path with publications metadata including bibliographic references",
        ),
        "inputPmcIdsMappingCSV": Param(
            default="hdfs://nameservice1/cache/external-resources/PMC-ids.csv",
            type="string",
            description="Identifier mapping CSV path for PMC IDs",
        ),
        "output": Param(
            default="hdfs://nameservice1/tmp/k8s-spark/citation_matching_direct/output",
            type="string",
            description="Output Avro path for matched citations",
        ),
        "output_report_root_path": Param(
            default="hdfs://nameservice1/tmp/k8s-spark/citation_matching_direct/reports",
            type="string",
            description="Output Avro path for final reports",
        ),

        # --- Spark resource tuning ---
        # --- TODO how to propagate sparkDriverMemory to the operator ---
        "sparkDriverMemory": Param(
            default="3g",
            type="string",
            description="Memory per driver",
        ),
        "sparkExecutorMemory": Param(
            default="7g",
            type="string",
            description="Memory per executor",
        ),
        "sparkExecutorOverhead": Param(
            default="2048",
            type="string",
            description="Off-heap memory overhead per executor, expressed in kilobytes (e.g., 2048 for 2GB)",
        ),
    },
    tags=["openaire", "citationmatching"],
    schedule=None
)
def citation_matching_direct():
        generate_spark_operator(
            task_id="citation_matching_direct",
            task_display_name="Perform citation matching direct algorithm",
            jar="{{ params.get('JAR') }}",
            image="{{ params.get('SPARK_IMAGE') }}",
            main_class="eu.dnetlib.iis.wf.citationmatching.direct.CitationMatchingDirectJob",
            arguments=[
                "-inputAvroPath", "{{ params.get('input') }}",
                "-inputPmcIdsMappingCSV", "{{ params.get('inputPmcIdsMappingCSV') }}",
                "-outputAvroPath", "{{ params.get('output') }}",
                "-outputReportPath", "{{ params.get('output_report_root_path') }}"               
            ],
            spark_extra_conf={
                "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
                "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorOverhead') }}",
                "spark.driverEnv.HADOOP_USER_NAME" : "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.executorEnv.HADOOP_USER_NAME" : "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.driverEnv.SPARK_USER" : "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.executorEnv.SPARK_USER" : "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.kubernetes.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}"
            }
        )
    


citation_matching_direct()
