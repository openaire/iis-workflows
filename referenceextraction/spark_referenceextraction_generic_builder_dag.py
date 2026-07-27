"""
Generic SQLite DB Builder — Airflow DAG

Ports any Oozie sqlite_builder workflow (referenceextraction/*/sqlite_builder)
to Airflow-on-K8s.  A single pod runs the AbstractDBBuilder subclass via
ProcessWrapper, with all builder-specific values supplied as DAG params.

Supported builders (all in eu.dnetlib.iis.wf.referenceextraction.*):
  service.ServiceDBBuilder
  community.CommunityDBBuilder
  dataset.DatasetDBBuilder         (used for both datacite and opentrials)
  patent.PatentDBBuilder
  project.ProjectDBBuilder
  researchinitiative.ResearchInitiativeDBBuilder

Workflow
--------
  1. Download the uber-JAR from Maven.
  2. Extract the SQL script (and any extra resources like base_*.db) from the JAR.
  3. Generate a minimal Hadoop configuration (core-site.xml) for HDFS access.
  4. Run  eu.dnetlib.iis.common.java.ProcessWrapper  with the chosen builder class.

How the builders differ — all handled via DAG params:

                     Input port   Output port    SQL script              init DB file         Notes
  ────────────────   ──────────   ────────────   ─────────────────────   ───────────────────  ─────
  Service            service      service_db     buildeoscdb.sql         —                     -w create
  Community          community    community_db   buildcummunitiesdb.sql  —                     -w create
  Dataset (datacite) dataset      dataset_db     builddatacitedb.sql     —                     -w create, -Xmx18g
  Dataset (opentrials) dataset    dataset_db     buildopentrialsdb.sql   —                     -w create
  Patent             patents      patents_db     buildpatentdb.sql       **/base_lens.db       -w create, copies base db
  Project            project      project_db     buildprojectdb.sql      **/base_projects.db  -d append, copies base db
  ResearchInitiative initiatives  initiatives_db egi_import.sql          —                     -w create

  The mexec.py flag (-w create vs -d append) is handled inside each builder's Java code.
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
    dag_id="spark_referenceextraction_generic_builder",
    dag_display_name="Build SQLite DB from Avro records using MadIS (generic)",
    default_args=default_args,
    params={
        # ------------------------------------------------------------------ #
        #  Artifact                                                          #
        # ------------------------------------------------------------------ #
        "JAR": Param(
            default=(
                "https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-referenceextraction/1.3.0-SNAPSHOT/iis-wf-referenceextraction-1.3.0-20260727.153050-12-uber.jar"
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
            description="Image with JRE 17, Spark, Python 2.7, APSW, and MadIS",
        ),

        # ------------------------------------------------------------------ #
        #  Hadoop / HDFS                                                     #
        # ------------------------------------------------------------------ #
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type="string",
            description="HDFS user name",
        ),
        "HDFS_NAMENODE": Param(
            "hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020",
            type="string",
            description="Default HDFS NameNode URI (fs.defaultFS)",
        ),

        # ------------------------------------------------------------------ #
        #  Builder class                                                     #
        # ------------------------------------------------------------------ #
        "BUILDER_CLASS": Param(
            default="eu.dnetlib.iis.wf.referenceextraction.service.ServiceDBBuilder",
            type="string",
            description="Fully qualified class name of the AbstractDBBuilder subclass",
        ),

        # ------------------------------------------------------------------ #
        #  I/O paths                                                         #
        # ------------------------------------------------------------------ #
        "INPUT_PATH": Param(
            default=(
                "hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/user/dnet.production/iis/working_dirs/primary/primary_import/metadataimport/service"
            ),
            type="string",
            description="HDFS input Avro path",
        ),
        "OUTPUT_PATH": Param(
            default=(
                "hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/referenceextraction_service/services.db"
            ),
            type="string",
            description="HDFS output path for the SQLite database file",
        ),

        # ------------------------------------------------------------------ #
        #  Script and resources                                               #
        # ------------------------------------------------------------------ #
        "SQL_SCRIPT": Param(
            default="scripts/buildeoscdb.sql",
            type="string",
            description="SQL script filename inside the JAR",
        ),
        "INIT_DB_LOCATION": Param(
            default="$UNDEFINED$",
            type="string",
            description=(
                "Optional path of a base SQLite DB file inside the JAR to use as "
                "initial database content.  Set only for builders that need it: "
                "Patent → '**/base_lens.db',  Project → '**/base_projects.db'. "
                "Leave as $UNDEFINED$ for builders that create the DB from scratch."
            ),
        ),

        # ------------------------------------------------------------------ #
        #  JVM tuning                                                        #
        # ------------------------------------------------------------------ #
        "JAVA_OPTS": Param(
            default="",
            type="string",
            description="Extra JVM arguments, e.g. '-Xmx18g' for Dataset datacite",
        ),
    },
    tags=["openaire", "iis", "referenceextraction", "sqlite_builder"],
    schedule=None,
)
def referenceextraction_sqlite_builder():
    """
    Build a SQLite database from Avro records using any AbstractDBBuilder subclass.
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

    build_db = KubernetesPodOperator(
        task_id="sqlite_builder",
        task_display_name="Build SQLite DB with MadIS",

        # ---- Pod identity ---- #
        name="sqlite-builder-{{ ds }}-{{ task_instance.try_number }}",
        namespace="spark-jobs",
        kubernetes_conn_id="kubernetes_default",

        # ---- Image ---- #
        image="{{ params.IMAGE }}",
        image_pull_policy="Always",

        # ---- Override the Spark entrypoint ---- #
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

# ------------------------------------------------------------------ #
#  1.  Download the uber-JAR                                          #
# ------------------------------------------------------------------ #
echo "[STEP 1] Downloading uber-JAR"
wget -q -O "${TMP_DIR}/${JAR_FILENAME}" "$JAR_URL"

# ------------------------------------------------------------------ #
#  2.  Extract SQL script and optional init DB from the JAR          #
# ------------------------------------------------------------------ #
echo "[STEP 2] Extracting resources from JAR"
mkdir -p "${TMP_DIR}/scripts"

# SQL script — required by all builders
unzip -j -o "${TMP_DIR}/${JAR_FILENAME}" \
    "*/sqlite_builder/**/{{ params.SQL_SCRIPT }}" \
    -d "${TMP_DIR}/scripts" >&2
ls -la "${TMP_DIR}/scripts/$(basename {{ params.SQL_SCRIPT }})"

# Optional init DB — PatentDBBuilder copies base_lens.db,
# ProjectDBBuilder copies base_projects.db as starting database.
{% if params.INIT_DB_LOCATION != "$UNDEFINED$" %}
echo "  Extracting init DB: {{ params.INIT_DB_LOCATION }}"
unzip -j -o "${TMP_DIR}/${JAR_FILENAME}" \
    "{{ params.INIT_DB_LOCATION }}" \
    -d "${TMP_DIR}/scripts" >&2
ls -la "${TMP_DIR}/scripts/$(basename {{ params.INIT_DB_LOCATION }})"
{% endif %}

# ------------------------------------------------------------------ #
#  3.  Generate Hadoop configuration                                   #
# ------------------------------------------------------------------ #
echo "[STEP 3] Generating Hadoop configuration"
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
#  4.  Run ProcessWrapper + AbstractDBBuilder subclass                #
# ------------------------------------------------------------------ #
echo "[STEP 4] Running {{ params.BUILDER_CLASS }}"
echo "  Input:   {{ params.INPUT_PATH }}"
echo "  Output:  {{ params.OUTPUT_PATH }}"
echo "  Script:  {{ params.SQL_SCRIPT }}"
echo '  Init DB: {{ params.INIT_DB_LOCATION }}'
echo "  Java opts: {{ params.JAVA_OPTS }}"

cd "${TMP_DIR}"

SPARK_CP="/opt/spark/jars/*"

# Resolve init DB path safely — single quotes prevent $UNDEFINED$ expansion
RAW_INIT_DB='{{ params.INIT_DB_LOCATION }}'
if [ "$RAW_INIT_DB" = '$UNDEFINED$' ]; then
    INIT_DB_ARG="$RAW_INIT_DB"
else
    INIT_DB_ARG="${TMP_DIR}/scripts/$(basename "$RAW_INIT_DB")"
fi

java \
    {{ params.JAVA_OPTS }} \
    -Djava.io.tmpdir="${TMP_DIR}" \
    -cp "${TMP_DIR}/conf:${SPARK_CP}:${TMP_DIR}/${JAR_FILENAME}" \
    eu.dnetlib.iis.common.java.ProcessWrapper \
    "{{ params.BUILDER_CLASS }}" \
    -Iinput="{{ params.INPUT_PATH }}" \
    -OoutputDb="{{ params.OUTPUT_PATH }}" \
    -PscriptLocation="${TMP_DIR}/scripts/$(basename {{ params.SQL_SCRIPT }})" \
    -PinitDbLocation="$INIT_DB_ARG"

echo "[DONE] SQLite DB built successfully"
        """],

        # ---- Environment ---- #
        env_vars={
            "HADOOP_USER_NAME": "{{ params.HADOOP_USER_NAME }}",
        },

        # ---- Startup timeout ---- #
        startup_timeout_seconds=300,

        # ---- Behaviour ---- #
        get_logs=True,
        is_delete_operator_pod=True,
    )

    build_db


referenceextraction_sqlite_builder()
