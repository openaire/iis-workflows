"""
Service SQLite DB Builder — Airflow DAG

Ports the Oozie workflow at
  iis-wf-referenceextraction/.../service/sqlite_builder/oozie_app/workflow.xml
to Airflow-on-K8s.

Workflow
--------
A single Kubernetes pod (NOT a SparkApplication) runs the same Java classes the
Oozie workflow used, leveraging the existing madis-with-spark Docker image.

  1. Download the uber-JAR from Maven.
  2. Extract both the MadIS Python toolkit and the SQL builder script from the JAR.
  3. Generate a minimal Hadoop configuration so HDFS I/O works inside the pod.
  4. Run  eu.dnetlib.iis.common.java.ProcessWrapper  with
     eu.dnetlib.iis.wf.referenceextraction.service.ServiceDBBuilder.

Architecture recap
------------------
ServiceDBBuilder (inheriting AbstractDBBuilder<Service>):
  • Reads Avro Service records from the HDFS input path.
  • Pipes them line-by-line as JSON to a MadIS subprocess stdin.
  • The subprocess (python /opt/madis/mexec.py -w <db> -f <sql>) creates
    a local SQLite database.
  • Copies the resulting SQLite DB to the HDFS output path.

Why KubernetesPodOperator instead of java_action() / RunJavaSparkJob?
  • java_action() launches a SparkApplication CRD — a driver pod plus the
    Spark operator overhead — even though ServiceDBBuilder is a pure Java
    Process, not a Spark job.  Here we avoid Spark entirely.
  • KubernetesPodOperator runs a plain K8s pod with a bash entrypoint — no
    SparkApplication CRD, no executor pods, no Spark scheduler overhead.
  • The bash entrypoint extracts the SQL script and the MadIS Python files
    from the uber-JAR, generates Hadoop config, and runs the exact same
    Java classes used by the Oozie workflow.

Alternative approach (commented at the bottom of the file):
  A java_action() variant that runs the same Java code via RunJavaSparkJob
  with dynamicAllocation.maxExecutors=0 (driver-only), preserving Spark's
  automatic HDFS config wiring at the cost of SparkApplication CRD overhead.
"""

import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.models.param import Param
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

package_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(package_dir)

if package_dir not in sys.path:
    sys.path.append(package_dir)
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

import dag_utils

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}


@dag(
    dag_id="spark_referenceextraction_service_builder",
    dag_display_name="Build Service SQLite DB from Avro records using MadIS",
    default_args=default_args,
    params={
        # ------------------------------------------------------------------ #
        #  Artifact                                                          #
        # ------------------------------------------------------------------ #
        "JAR": Param(
            default=(
                "https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-referenceextraction/1.3.0-SNAPSHOT/iis-wf-referenceextraction-1.3.0-20260724.113248-9-uber.jar"
            ),
            type="string",
            description="iis-wf-referenceextraction uber JAR URL",
        ),

        # ------------------------------------------------------------------ #
        #  Docker image (must include Python 2.7 + MadIS + JRE)             #
        # ------------------------------------------------------------------ #
        # madis-with-spark extends the base spark image with Python 2.7,
        # apsw, and the Madis query application (MADIS_HOME=/opt/madis).
        # ServiceDBBuilder uses Runtime.exec("python $MADIS_HOME/mexec.py …")
        # which requires Python 2.7 + MadIS python modules to be present.
        "IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/madis-with-spark:4.1.2",
            type="string",
            description=(
                "Docker image with JRE 17, Spark, Hadoop client, "
                "Python 2.7, APSW, and MadIS (MADIS_HOME set)"
            ),
        ),

        # ------------------------------------------------------------------ #
        #  Hadoop / HDFS                                                     #
        # ------------------------------------------------------------------ #
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type="string",
            description="HDFS user name (driver and executor env)",
        ),
        "HDFS_NAMENODE": Param(
            "hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020",
            type="string",
            description=(
                "Default HDFS NameNode URI.  Used as fs.defaultFS in the "
                "generated core-site.xml so Hadoop FileSystem API can connect."
            ),
        ),

        # ------------------------------------------------------------------ #
        #  I/O paths                                                         #
        # ------------------------------------------------------------------ #
        "inputServicePath": Param(
            default=(
                "hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/user/dnet.production/iis/working_dirs/primary/primary_import/metadataimport/service"
            ),
            type="string",
            description=(
                "Input Avro path with Service records (eu.dnetlib.iis.importer.schemas.Service)"
            ),
        ),
        "outputServiceDbPath": Param(
            default=(
                "hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/referenceextraction_service/services.db"
            ),
            type="string",
            description="Output HDFS path for the SQLite database file",
        ),
    },
    tags=["openaire", "iis", "referenceextraction", "sqlite_builder", "service"],
    schedule=None,
)
def referenceextraction_service_builder():
    """
    Build the Service SQLite database in a single K8s pod.
    """

    # ------------------------------------------------------------------ #
    #  NOTE on HDFS connectivity inside a plain K8s pod                   #
    # ------------------------------------------------------------------ #
    # The Java Hadoop client resolves HDFS paths via Configuration files
    # (core-site.xml, hdfs-site.xml) loaded from the classpath.
    #
    # In a SparkApplication pod these configs are mounted automatically by
    # the Spark operator.  For a plain KubernetesPodOperator they must be
    # provided explicitly.  There are two options:
    #
    #   A) Mount a ConfigMap  (production, recommended):
    #      Mount the same hadoop-config ConfigMap that the Spark operator
    #      uses into /opt/hadoop/etc/hadoop/ and set HADOOP_CONF_DIR.
    #
    #   B) Generate inline     (self-contained, shown here):
    #      The bash script below writes a minimal core-site.xml referencing
    #      the HDFS NameNode from the params.  This works for direct
    #      NameNode URIs.  For HA nameservices (hdfs://nameservice1/…) you
    #      also need hdfs-site.xml — mount a ConfigMap in that case.
    #
    # When inputServicePath uses an HA nameservice and you do NOT mount
    # the Hadoop ConfigMap, set inputServicePath to a direct NameNode URI
    # such as the one shown in outputServiceDbPath default above.
    # ------------------------------------------------------------------ #

    build_service_db = KubernetesPodOperator(
        task_id="build_service_db",
        task_display_name="Build Service SQLite DB with MadIS",

        # ---- Pod identity ---- #
        name="build-service-db-{{ ds }}-{{ task_instance.try_number }}",
        namespace="spark-jobs",
        kubernetes_conn_id="kubernetes_default",

        # ---- Image ---- #
        image="{{ params.IMAGE }}",
        image_pull_policy="Always",

        # ---- Override the Spark entrypoint with our own script ---- #
        cmds=["/bin/bash"],
        arguments=["-c", r"""
set -euo pipefail

# ------------------------------------------------------------------ #
#  0.  Basic setup                                                    #
# ------------------------------------------------------------------ #
JAR_URL="{{ params.JAR }}"
JAR_FILENAME=$(basename "$JAR_URL")
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

export HADOOP_USER_NAME="{{ params.HADOOP_USER_NAME }}"

# The madis-with-spark image ships MadIS under MADIS_HOME and provides
# 'python' → Python 2.7 (used by mexec.py shebangs).
MADIS_HOME="${MADIS_HOME:-/opt/madis}"

# ------------------------------------------------------------------ #
#  1.  Download the uber-JAR                                          #
# ------------------------------------------------------------------ #
echo "[STEP 1] Downloading uber-JAR from $JAR_URL"
curl -fsSL -o "${TMP_DIR}/${JAR_FILENAME}" "$JAR_URL"

# ------------------------------------------------------------------ #
#  2.  Extract SQL builder script from the uber-JAR                  #
# ------------------------------------------------------------------ #
echo "[STEP 2] Extracting SQL builder script from JAR"
mkdir -p "${TMP_DIR}/scripts"
unzip -j -o "${TMP_DIR}/${JAR_FILENAME}" \
    "*/sqlite_builder/oozie_app/lib/scripts/buildeoscdb.sql" \
    -d "${TMP_DIR}/scripts" >&2

ls -la "${TMP_DIR}/scripts/buildeoscdb.sql"

# ------------------------------------------------------------------ #
#  3.  Generate Hadoop configuration                                   #
# ------------------------------------------------------------------ #
echo "[STEP 3] Generating Hadoop configuration"

# Minimal core-site.xml — enough for direct NameNode URIs.
# If your cluster uses HA nameservices, mount a ConfigMap with the full
# hdfs-site.xml instead, or use the commented ConfigMap stanza in the
# KubernetesPodOperator definition above.
mkdir -p "${TMP_DIR}/conf"
cat > "${TMP_DIR}/conf/core-site.xml" << 'CORE_XML'
<?xml version="1.0"?>
<?xml-stylesheet type="text/xsl" href="configuration.xsl"?>
<configuration>
    <property>
        <name>fs.defaultFS</name>
        <value>{{ params.HDFS_NAMENODE }}</value>
    </property>
</configuration>
CORE_XML

# ------------------------------------------------------------------ #
#  4.  Run ProcessWrapper + ServiceDBBuilder                          #
# ------------------------------------------------------------------ #
echo "[STEP 4] Running ServiceDBBuilder via ProcessWrapper"
echo "  Input:   {{ params.inputServicePath }}"
echo "  Output:  {{ params.outputServiceDbPath }}"
echo "  Script:  ${TMP_DIR}/scripts/buildeoscdb.sql"

cd "${TMP_DIR}"

# Build classpath from the image's Spark installation.  The base
# spark:4.1.2 image ships all of its JARs (including Hadoop client
# libraries bundled via the -Phadoop-cloud profile) under /opt/spark/jars/.
# That's sufficient to satisfy the Hadoop/commons dependencies that
# ProcessWrapper and ServiceDBBuilder need at runtime.
SPARK_CP="/opt/spark/jars/*"

java \
    -Djava.io.tmpdir="${TMP_DIR}" \
    -cp "${TMP_DIR}/conf:${SPARK_CP}:${TMP_DIR}/${JAR_FILENAME}" \
    eu.dnetlib.iis.common.java.ProcessWrapper \
    eu.dnetlib.iis.wf.referenceextraction.service.ServiceDBBuilder \
    -Iservice="{{ params.inputServicePath }}" \
    -Oservice_db="{{ params.outputServiceDbPath }}" \
    -PscriptLocation="${TMP_DIR}/scripts/buildeoscdb.sql"

echo "[DONE] Service SQLite DB built successfully"
        """],

        # ---- Environment ---- #
        env_vars={
            "HADOOP_USER_NAME": "{{ params.HADOOP_USER_NAME }}",
            "MADIS_HOME": "/opt/madis",
        },

        # ---- Startup timeout ---- #
        # ServiceDBBuilder needs to download the uber-JAR (potentially
        # large) before the Java process starts.  Give the pod enough
        # time to pull the image and download/extract the JAR.
        startup_timeout_seconds=300,

        # ---- Behaviour ---- #
        get_logs=True,
        is_delete_operator_pod=True,

        # ------------------------------------------------------------------ #
        #  Optional: mount Hadoop ConfigMap for HA nameservice resolution     #
        # ------------------------------------------------------------------ #
        # Uncomment and adjust when your cluster provides a ConfigMap with
        # core-site.xml and hdfs-site.xml for HDFS connectivity:
        #
        # configmaps=[
        #     "hadoop-config",   # name of the ConfigMap in the pod namespace
        # ],
        # env_vars={
        #     **{...},           # merge with env_vars above
        #     "HADOOP_CONF_DIR": "/opt/hadoop/etc/hadoop",
        # },
    )

    build_service_db
