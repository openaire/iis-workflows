import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.hooks.base import BaseHook
from airflow.models.param import Param

package_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(package_dir)

# Ensure it's in sys.path
if package_dir not in sys.path:
    sys.path.append(package_dir)
# This is important to reference common set of utility methods stored in the main workflow project repository folder
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

import dag_utils
from spark_configurator import generate_spark_operator, java_action

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60)))
}

@dag(
    dag_id="spark_citation_matching",
    dag_display_name="Performs fuzzy citation matching algorithm matching publications by bibliographic references",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-citationmatching/1.3.0-SNAPSHOT/iis-wf-citationmatching-1.3.0-20260623.152848-3-uber.jar",
            type='string',
            description="citation matching shaded jar"
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2",
            type='string',
            description=""),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type='string',
            description=""),
        # --- I/O paths ---
        "input_metadata": Param(
            default="hdfs://nameservice1/tmp/marek.horst/citationmatching/input-small",
            type="string",
            description="Input Avro path with document metadata (ExtractedDocumentMetadataMergedWithOriginal)",
        ),
        "input_matched_citations": Param(
            default="hdfs://nameservice1/tmp/marek.horst/citationmatching/input-matched-citations-small",
            type="string",
            description="Input Avro path with already matched citations to be excluded from processing",
        ),
        "workingDir": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/citationmatching/workingDir",
            type="string",
            description="Working directory for intermediate results",
        ),
        "output_citations": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/k8s-spark/citation_matching/output",
            type="string",
            description="Output Avro path for matched citations (eu.dnetlib.iis.common.citations.schemas.Citation)",
        ),
        "output_report_root_path": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/k8s-spark/citation_matching/reports",
            type="string",
            description="Output Avro path for final reports",
        ),

        # --- Cache settings ---
        "cacheRootDir": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/citationmatching/cache",
            type="string",
            description="Citation matching cache root directory",
        ),
        "cacheOlderThanXYears": Param(
            default="2",
            type="string",
            description="Number of years subtracted from the current year to determine the caching eligibility threshold",
        ),
        "lockManagerFactoryClassName": Param(
            default="eu.dnetlib.iis.common.lock.HadoopFsLockManagerFactory",
            type="string",
            description="Lock manager factory class name for synchronizing access to the cache directory. "
                        "HadoopFsLockManagerFactory uses a sentinel file on the target storage (HDFS/S3/Ceph) "
                        "and requires no external coordination service. "
                        "Use eu.dnetlib.iis.common.lock.ZookeeperLockManagerFactory for legacy Hadoop HA clusters.",
        ),
        "numberOfEmittedFilesInCache": Param(
            default="1000",
            type="string",
            description="Number of files created by citation matching caching module in cache",
        ),
        "numberOfEmittedFiles": Param(
            default="1000",
            type="string",
            description="Number of files generated as citation matching final outcome",
        ),

        # --- Algorithm tuning ---
        "maxHashBucketSize": Param(
            default="10000",
            type="string",
            description="Max number of citation-document pairs for a given hash bucket",
        ),
        "numberOfPartitions": Param(
            default="100",
            type="string",
            description="Number of partitions used for RDDs with citations and documents read from input files",
        ),

        # --- Spark resource tuning ---
        "sparkNetworkTimeout": Param(
            default="1200s",
            type="string",
            description="Spark network timeout; also controls executor heartbeat deadline (oozie: citationMatchingSparkNetworkTimeout)",
        ),
        "sparkShuffleRegistrationTimeout": Param(
            default="30000",
            type="string",
            description="Timeout (ms) for executor registration with the external shuffle service / Celeborn (oozie: citationMatchingSparkShuffleRegistrationTimeout)",
        ),
        "celebornMaxReviveTimes": Param(
            default="10",
            type="string",
            description="Maximum number of Celeborn revive attempts per push before the task is failed",
        ),
        "sparkDriverMemory": Param(
            default="10g",
            type="string",
            description="Memory per driver",
        ),
        "sparkExecutorMemory": Param(
            default="10g",
            type="string",
            description="Memory per executor",
        ),
        "sparkExecutorOverhead": Param(
            default="4096",
            type="string",
            description="Off-heap memory overhead per executor, expressed in kilobytes (e.g., 2048 for 2GB)",
        ),
    },
    tags=["openaire", "iis", "citationmatching"],
    schedule=None
)
def citation_matching():
    spark_conf = {
        "spark.driver.memory": "{{ params.get('sparkDriverMemory') }}",
        "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
        "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorOverhead') }}",
        # Network / shuffle timeouts — critical for the heavy shuffle in citation matching.
        # These mirror the oozie workflow's citationMatchingSparkNetworkTimeout and
        # citationMatchingSparkShuffleRegistrationTimeout properties.
        "spark.network.timeout": "{{ params.get('sparkNetworkTimeout') }}",
        "spark.shuffle.registration.timeout": "{{ params.get('sparkShuffleRegistrationTimeout') }}",
        # Celeborn — allow more revive attempts before a push is considered fatal.
        "spark.celeborn.client.push.maxReviveTimes": "{{ params.get('celebornMaxReviveTimes') }}",
        "spark.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.driverEnv.SPARK_USER": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.SPARK_USER": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}"
    }

    input_transformer = generate_spark_operator(
        task_id="citation_matching_input_transformer",
        task_display_name="Transform input metadata for citation matching",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.citationmatching.input.CitationMatchingInputTransformerJob",
        arguments=[
            "-inputMetadata", "{{ params.get('input_metadata') }}",
            "-inputMatchedCitations", "{{ params.get('input_matched_citations') }}",
            "-cacheRootDir", "{{ params.get('cacheRootDir') }}",
            "-output", "{{ params.get('workingDir') }}/documents_with_authors"
        ],
        spark_extra_conf=spark_conf
    )

    citation_matching_job = generate_spark_operator(
        task_id="citation_matching",
        task_display_name="Perform fuzzy citation matching algorithm",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.citationmatching.IisCitationMatchingJob",
        arguments=[
            "-fullDocumentPath", "{{ params.get('workingDir') }}/documents_with_authors",
            "-outputDirPath", "{{ params.get('workingDir') }}/matched_citations",
            "-outputReportPath", "{{ params.get('output_report_root_path') }}",
            "-maxHashBucketSize", "{{ params.get('maxHashBucketSize') }}",
            "-numberOfPartitions", "{{ params.get('numberOfPartitions') }}"
        ],
        spark_extra_conf=spark_conf
    )

    output_transformer = generate_spark_operator(
        task_id="citation_matching_output_transformer",
        task_display_name="Transform citation matching output and update cache",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.citationmatching.output.CitationMatchingOutputTransformerJob",
        arguments=[
            "-inputMetadata", "{{ params.get('input_metadata') }}",
            "-inputMatchedCitations", "{{ params.get('workingDir') }}/matched_citations",
            "-cacheRootDir", "{{ params.get('cacheRootDir') }}",
            "-cacheOlderThanXYears", "{{ params.get('cacheOlderThanXYears') }}",
            "-lockManagerFactoryClassName", "{{ params.get('lockManagerFactoryClassName') }}",
            "-numberOfEmittedFilesInCache", "{{ params.get('numberOfEmittedFilesInCache') }}",
            "-numberOfEmittedFiles", "{{ params.get('numberOfEmittedFiles') }}",
            "-output", "{{ params.get('output_citations') }}"
        ],
        spark_extra_conf=spark_conf
    )

    input_transformer >> citation_matching_job >> output_transformer


citation_matching()
