import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.models.param import Param

package_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(package_dir)

if package_dir not in sys.path:
    sys.path.append(package_dir)
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

from spark_configurator import generate_spark_operator

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}


@dag(
    dag_id="spark_remove_extracted_document_metadata_references",
    dag_display_name="Clear references from merged extracted document metadata",
    default_args=default_args,
    params={
        "JAR_TRANSFORMERS": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-20261020.160549-17-uber.jar",
            type="string",
            description="iis-wf-transformers uber JAR containing RemoveReferencesJob",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2",
            type="string",
            description="Spark Docker image",
        ),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type="string",
            description="Hadoop user name used when reading and writing HDFS data",
        ),
        "inputPath": Param(
            default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/transformers_metadatamerger/output_merged_metadata",
            type="string",
            description="Input Avro data store of ExtractedDocumentMetadataMergedWithOriginal records",
        ),
        "outputPath": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/merged_metadata_without_references",
            type="string",
            description="Output Avro data store with references set to null",
        ),
        "sparkDriverMemory": Param(default="2g", type="string"),
        "sparkExecutorMemory": Param(default="2g", type="string"),
    },
    tags=["openaire", "iis", "transformers", "metadataextraction"],
    schedule=None,
)
def spark_remove_references():
    generate_spark_operator(
        task_id="remove_references",
        task_display_name="Set merged metadata references to null",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.metadatamerger.RemoveReferencesJob",
        arguments=[
            "-input", "{{ params.get('inputPath') }}",
            "-output", "{{ params.get('outputPath') }}",
        ],
        spark_extra_conf={
            "spark.driver.memory": "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
            "spark.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.driverEnv.SPARK_USER": "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.executorEnv.SPARK_USER": "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.kubernetes.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        },
    )


spark_remove_references()
