# Metadata extraction — Oozie → Airflow/Spark 4 migration

This directory holds the Airflow workflow definitions replacing the metadata extraction (and
cache builder) Oozie workflows so that they can run in the new Kubernetes environment on Spark 4.

## Deliverables

### Java Spark jobs (in the `iis` repository)

| New class | Replaces | Notes |
|---|---|---|
| `eu.dnetlib.iis.wf.metadataextraction.MetadataExtractorJob` | `MetadataExtractorMapper` (MapReduce) + the `metadataextraction/core` Oozie workflow | PDF metadata extraction via Grobid or embedded CERMINE; writes `meta`/`fault`/`transientfault` datastores and `import.metadataExtraction.processed.*` report entries. Exposes `extract(...)`/`emptyResult(...)` reused in-process. |
| `eu.dnetlib.iis.wf.metadataextraction.cache.MetadataExtractionCacheChainJob` | `cache/chain` + `cache/create` + `cache/update` Oozie workflows | Cache management (skip-extracted, cache id, lock, create/update, union of returned meta, fault handling) offloaded into Spark, using `CacheStorageUtils`/`CacheMetadataManagingProcess`/`LockManagerUtils` — the same pattern as `CachedWebCrawlerJob`. |
| `eu.dnetlib.iis.wf.importer.content.DocumentContentUrlBasedImporterJob` | `importer/content` (MapReduce `DocumentContentUrlBasedImporterMapper`) | Object-store content retrieval (`DocumentContentUrl` → `DocumentContent`). Reuses `ObjectStoreContentProviderUtils`/`ContentRetrievalContext`. |
| `eu.dnetlib.iis.wf.importer.content.DocumentContentUrlDedupJob` | `content_url/dedup` PIG `dedup.pig` | Deduplicates by `(id, contentChecksum)`. |
| `eu.dnetlib.iis.wf.importer.content.DocumentContentUrlDispatcherJob` | `content_url/chain` MapReduce `DocumentContentUrlDispatcher` | Mime-type based dispatch to `pdf`/`html`/`xmlpmc`/`wos` ports + per-type report entries. |

Already ported Spark jobs are **reused as-is**: `ChecksumPreprocessingJob`,
`SkipExtractedWithoutMetaJob` (logic absorbed by the cache chain job), `UnionJob`,
`ChecksumMetaPostprocessingJob`, `ChecksumFaultPostprocessingJob`, `IdReplacerJob`,
`ExistenceFilterJob`, `HiveBasedDocumentContentUrlImporterJob`.

### Airflow DAGs (in this repository)

| DAG | Replaces | Must stay Airflow |
|---|---|---|
| `import/spark_content_url_chain_dag.py` (`spark_content_url_chain`) | `importer/content_url/chain` | yes — reused by many IIS workflows |
| `metadataextraction/spark_metadataextraction_cache_identify_by_checksum_dag.py` | `metadataextraction/cache/identify_by_checksum` | yes — top level workflow |
| `metadataextraction/spark_metadataextraction_cache_builder_dag.py` | `metadataextraction/cache/builder` | yes — top level workflow |

## Approach: 1-to-1 mapping vs. simplification

Per the task description the choice should be driven by **reusability** and the **existence of
already-utilised Airflow workflows**. Both factors were applied:

* The three workflows explicitly listed as reusable remain **Airflow DAGs** and are called rather
  than duplicated: `identify_by_checksum`, `cache/builder` and `importer/content_url/chain`
  (the latter triggered via `TriggerDagRunOperator` so it can be reused by other workflows).
* Everything else that was **not** reusable and only existed for the sake of Oozie's cumbersome
  conditional transitions was **offloaded into Spark** — most notably the whole cache hierarchy
  (`cache/chain` + `cache/create` + `cache/update`) with its lock and cache-id management
  utility actions. This removes ZooKeeper/HDFS lock actions, the `check_input_isempty` +
  `decision` pairs and the `cache_create`/`cache_update` duplication from the workflow layer,
  exactly as suggested by the `CachedWebCrawlerJob` precedent.
* For Oozie sub-workflows that effectively covered **a single action** (rule of thumb from the
  task description), no Airflow wrapper was created — the corresponding Spark job is called
  directly from the parent DAG (e.g. `transformers_idreplacer`, `transformers_common_existencefilter`,
  `content_url/dedup`, `postprocessing-meta`/`-fault`).

## Notable simplifications / deviations

* **Emptiness checks** (`check_input_isempty` / `generate-empty-output`): handled inside
  `MetadataExtractionCacheChainJob`, which writes empty `meta`/`fault` datastores for empty input.
  The outcome is identical to the Oozie branching, without any Airflow branch.
* **Schema generation** (`AvroSchemaGenerator` Oozie actions): unnecessary in Spark — schemas are
  compile-time constants.
* **`ReportGenerator` / `PigCountersReportGenerator` / `OozieTimeReportGenerator`**: replaced by
  writing `ReportEntry` Avro datastores directly from the Spark jobs (established convention),
  which are then merged by `ReportMerger`.
* **`PigCountersReportGenerator` counters** for the PIG-ported transformers
  (`skip_extracted.fromCache.docMetadata`) are emitted directly by the cache chain job as
  `<prefix>.fromCache.docMetadata` report entries.
* **`OozieTimeReportGenerator`** (execution-time reports based on the Oozie service) has no
  equivalent — task durations are available natively from Airflow.
* **Report builder output path**: the merged `report.json` is written to
  `{workingDir}/report_builder/report.json` (instead of `{workingDir}/report/working_dir/report.json`)
  so that it is not picked up as a partial-report subdirectory on reruns. The report *builder*
  workflow is otherwise ported 1-to-1 via `ProcessWrapper` + `ReportMerger`.
* **`remove_sideproducts` / `init-workingDir`**: left to cluster housekeeping (working-directory
  retention) rather than modelled as Airflow steps.
* **`transientfault`**: written by `MetadataExtractorJob` for analysis only and never persisted in
  cache, matching the original behaviour.
* **Cache layout**: the cache is stored via `CacheStorageUtils` using the standard
  `<cache_location>/<cache_id>/{data,fault}` layout (with `meta.json` holding the current cache id)
  instead of the Oozie-specific `<cache_location>/<cache_id>/{meta,fault}` naming. Since the
  metadata extraction cache is (re)created in the k8s environment this is a non-breaking change and
  makes the cache consistent with the other cached IIS jobs.

## Not covered by this migration

The following Oozie workflows referenced by the metadata extraction hierarchy are **not** part of
this change because they are either PMC/XML specific or unrelated PIG-based phases that are
handled elsewhere (they are not listed among the workflows requiring porting):

* `eu/dnetlib/iis/wf/ingest/pmc/prefetched/oozie_app` and `eu/dnetlib/iis/wf/ingest/pmc/metadata/oozie_app`
  (PMC/XML metadata ingest; `importer/plaintext` + `ingest/pmc/metadata` +
  `transformers/ingest/pmc/metadata`) — needed only when `import_mode=xmlpmc`.
* `eu/dnetlib/iis/wf/transformers/metadataextraction/documenttext/oozie_app`.

## Parameter conventions

All Spark tasks share the established conventions of this repository:

* `JAR_*` — per-module uber (shaded) JAR URL; `SPARK_IMAGE` — spark 4 image;
  `HADOOP_USER_NAME` — propagated to driver/executor via `spark.*Env.HADOOP_USER_NAME`.
* `$UNDEFINED$` is the sentinel for an unbounded/undefined value, matching the Oozie convention.
* Cache handling relies on `-cacheRootDir` + `-lockManagerFactoryClassName`
  (`HadoopFsLockManagerFactory` requires no external coordination service; use
  `ZookeeperLockManagerFactory` only for legacy Hadoop HA clusters).
* `sparkExecutorCores` (default `4`) is set explicitly on every DAG in the chain (builder,
  `identify_by_checksum`, `spark_content_url_chain`). The Kubernetes environment runs with 24
  cores per executor, which leaves each concurrent task far too little heap — this caused the
  `IdReplacerJob` shuffle-writer `OutOfMemoryError`. 4 mirrors the Hadoop cluster where the
  workflow previously succeeded.
* `sparkDriverMemory` and `sparkExecutorMemory` default to `7g` on the same DAGs, matching the
  Hadoop cluster driver/executor memory (7g executor with 4 cores ≈ 1.75g per task).

### Bounding Grobid concurrency (`grobidSparkExecutorInstances`)

`MetadataExtractionCacheChainJob` calls the external Grobid service once per document, so the load
it generates is proportional to the number of Spark tasks running concurrently. One param pins that
number down (same mechanism as `crossref/mute_citations_parsing_and_export.py`):

| Param | Default | Effect |
|---|---|---|
| `grobidSparkExecutorInstances` | `8` | fixed `spark.executor.instances` for that task |
| `sparkExecutorCores` | `4` | concurrent tasks per executor |

In-flight Grobid requests ≈ `grobidSparkExecutorInstances × sparkExecutorCores` = **32** by default.

There is deliberately **no** switch for dynamic allocation. Capping is the entire purpose of this
config, so enabling dynamic allocation would silently reintroduce the unbounded fan-out it guards
against. Both `spark.executor.instances` and the CRD's `spec.dynamicAllocation` are therefore emitted
with a fixed, non-tunable shape (`enabled: false`).

**The guard is applied to `metadataextraction_cached` only** — it is the single task in these DAGs
that reaches Grobid. `preprocessing`, `importer_content` and the `postprocessing-*` tasks do pure
HDFS/object-store work, so they keep the environment's default dynamic allocation and are not
throttled (`spark_extra_conf=spark_conf`); the pinned variant is `grobid_spark_conf` /
`grobid_dynamic_allocation`.

In `identify_by_checksum` the task runs with `-activeMetadataExtraction false`, so no Grobid calls
happen there at all and the guard is dormant. It is still attached to that task so that enabling
extraction later cannot silently reintroduce unlimited concurrency.

Both `metadataextraction_cached` tasks pass `dynamic_allocation={...}` to `generate_spark_operator`,
because the `SparkApplication` CRD exposes dynamic allocation as a first-class
`spec.dynamicAllocation` field whose properties **override** `sparkConf`, and the operator's
defaulting webhook enables it (with its own `maxExecutors`, currently 14) whenever the field is
absent. Without the explicit field the executor count is not controllable — with 24 cores per
executor that meant up to ~336 concurrent Grobid requests.

`dag_utils.merge_dicts` gives the earlier dict precedence, so these values win over the
`spark_template` Airflow Variable. To reduce Grobid load further, lower
`grobidSparkExecutorInstances` (or `sparkExecutorCores`) when triggering the DAG; no code change
needed.

## Object store (S3) content retrieval

`importer_content` downloads the PDFs referenced by `s3://…` content URLs, so it needs the object
store endpoint and credentials. Both DAGs default to the values shipped with the original Oozie
`cache/builder/job.properties`:

| Param | Default | Job flag |
|---|---|---|
| `objectstore_s3_endpoint` | `s3.icm.edu.pl` | `-objectstoreS3Endpoint` |
| `objectstore_s3_keystore_location` | `jceks://hdfs/user/marek.horst/secrets/s3keyfile.jceks` | `-objectstoreS3KeystoreLocation` |

`objectstore_s3_keystore_location` becomes `hadoop.security.credential.provider.path`; the provider
must expose the `fs.s3a.access.key` / `fs.s3a.secret.key` entries (see
`HadoopBasedS3CredentialsProvider`). When they are not found the client **falls back to anonymous
credentials**, which is why a broken keystore path shows up as every content being counted in
`import.contents.pdf.invalid.unavailable` rather than as a hard failure.

Note the keystore lives in the `marek.horst` HDFS home, matching the `HADOOP_USER_NAME` default.

Because `ContentRetrievalContext` is created **inside the Spark task** with a fresh Hadoop
`Configuration`, `DocumentContentUrlBasedImporterJob` copies the driver's Hadoop configuration into
the job params and replays it there. Without that, `jceks://hdfs/…` would be resolved against the
local filesystem (`fs.defaultFS` unset in the task JVM) and the credentials would silently be
dropped.

## Optional content-url transformations (`input_id_mapping` / `input_id`)

These two parameters are *not* declared by the Oozie `cache/builder` workflow itself — they were
forwarded from the parent (primary) workflow via `<propagate-configuration/>` into the
`import_content_url` sub-workflow, which is consumed by the `importer/content_url/chain` workflow.
In the Airflow port they are declared on the **builder** DAG and forwarded to the
`spark_content_url_chain` DAG through `TriggerDagRunOperator.conf`:

| Parameter | Consumed by | Spark job | Datastore |
|---|---|---|---|
| `input_id_mapping` | `transformers_idreplacer` | `IdReplacerJob` (`-inputIdMapping`) | `eu.dnetlib.iis.common.schemas.IdentifierMapping` |
| `input_id` | `transformers_common_existencefilter` | `ExistenceFilterJob` (`-inputExistentId`) | `eu.dnetlib.iis.common.schemas.Identifier` |

Both default to `$UNDEFINED$`; when left undefined the corresponding step is skipped
(`BranchPythonOperator` decisions in `spark_content_url_chain`), exactly as in Oozie.

To provide them, override the params when triggering the builder DAG, e.g.:

```json
{
  "input_id_mapping": "hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/id_mapping",
  "input_id": "hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/existent_id"
}
```

Note the mapping direction: **`input_id_mapping` belongs to the id-replacer** and
**`input_id` belongs to the existence filter** (the description in the original task statement had
them the other way round).

## Building the uber JARs (`JAR_*` params)

Every module referenced by a `JAR_*` param declares a `shade-uber-jar` Maven profile (the same
profile that already existed in `iis-wf-transformers`), producing a `*-uber.jar` attached with the
`uber` classifier:

```bash
cd ~/iis
mvn package -Pshade-uber-jar -Dmaven.test.skip=true \
  -pl iis-wf/iis-wf-import,iis-wf/iis-wf-transformers,iis-wf/iis-wf-metadataextraction,iis-wf/iis-wf-report
```

Artifacts:

| Module | `JAR_*` param | Artifact |
|---|---|---|
| `iis-wf-import` | `JAR_IMPORT` | `iis-wf-import-<version>-uber.jar` |
| `iis-wf-transformers` | `JAR_TRANSFORMERS` | `iis-wf-transformers-<version>-uber.jar` |
| `iis-wf-metadataextraction` | `JAR_METADATAEXTRACTION` | `iis-wf-metadataextraction-<version>-uber.jar` |
| `iis-wf-report` | `JAR_REPORT` | `iis-wf-report-<version>-uber.jar` |

`spark`/`hadoop` dependencies are `provided`, so they are intentionally **not** bundled — the Spark
image supplies them. With `shadedArtifactAttached=true`, `mvn install`/`deploy` publishes the uber
artifact automatically.

The shade profiles also apply size filters for the AWS SDK v2 stack:

* `models/*.json` (AWS SDK v2 service models, ~0.5 GB raw)
* `software.amazon.awssdk:*` (~0.6 GB jar)
* `software.amazon.s3.analyticsaccelerator:*`

### AWS SDK v1 (`com.amazonaws:*`) — module-specific policy

The image does **not** provide AWS SDK v1 (its bundled Hadoop uses SDK v2), so it must be shipped by
whichever JAR actually performs `s3://` content retrieval:

| Module | SDK v1 in uber JAR | Why |
|---|---|---|
| `iis-wf-import` (`JAR_IMPORT`) | **bundled** | Runs `DocumentContentUrlBasedImporterJob`, which downloads the PDFs from the object store |
| `iis-wf-metadataextraction` (`JAR_METADATAEXTRACTION`) | **excluded** | Only ever launches `MetadataExtractionCacheChainJob`; the extraction path never touches AWS |
| `iis-wf-report` (`JAR_REPORT`) | n/a | No AWS code on its classpath at all |

Excluding v1 from `iis-wf-import` caused, at runtime:
`java.lang.NoClassDefFoundError: com/amazonaws/auth/AWSCredentialsProvider`.

Excluding it from `iis-wf-metadataextraction` is safe — verified, not assumed:

1. **Source level** — zero `com.amazonaws` references in `iis-wf-metadataextraction/src` and in
   `iis-common/src`. The only things this module takes from `iis-wf-import` are `HttpClientUtils`,
   `ContentApprover`, `PDFHeaderBasedContentApprover`, `InvalidCountableContentApproverWrapper`, and
   none of them references AWS.
2. **Bytecode level** — scanning every `eu/dnetlib/**` class in the built uber JAR, the *only* IIS
   classes referencing `com.amazonaws` are the three below, and **no class outside
   `eu.dnetlib.iis.wf.importer.content` references any of them**:
   `ContentRetrievalContext`, `ObjectStoreContentProviderUtils`, `HadoopBasedS3CredentialsProvider`.
3. **Reachability** — the only references to those three come from `DocumentContentUrlBasedImporterJob`
   (+ its `ContentRetrievalIterator`), `DocumentContentUrlBasedImporterMapper`,
   `DocumentTextUrlBasedImporterMapper` and `ObjectStoreDocumentContentUrlImporterMapper` — i.e. the
   content-importer entry points, which `JAR_METADATAEXTRACTION` never launches.

Result: 362 MB → **85 MB**. The trade-off is that those importer classes stay in the JAR but become
non-loadable; if this JAR is ever reused to run `DocumentContentUrlBasedImporterJob`, the filter must
be removed.

AWS SDK v2 and the S3 analytics accelerator are only needed by `hadoop-aws` (S3A paths), which the
runtime image provides; the IIS code does not use them.

### Two different S3 mechanisms — do not conflate them

| | `s3://…` content URLs | `s3a://…` Hadoop paths |
|---|---|---|
| Where | `url` field of the `DocumentContent`/`DocumentMetadata` datastore, e.g. `s3://openaire-objectstore-prod/opendoar____::6f49…/arXiv_______::d266….pdf` | `-input` / `-output` / `-cacheRootDir` style path params |
| Read by | `ObjectStoreContentProviderUtils.getContentFromS3()` → `AmazonS3Client` | Hadoop `FileSystem` via `hadoop-aws` |
| API | **AWS SDK v1** (`com.amazonaws`) | **AWS SDK v2** (`software.amazon.awssdk`) |
| Supplied by | **the uber JAR** (bundled — see above) | the `spark:4.1.1` image (`-Phadoop-cloud` → `spark-hadoop-cloud_2.13-4.1.1.jar`) |
| Used by the ported code | **Yes** — this is how PDFs are fetched for extraction | No (`grep s3a://` → 0 hits in IIS Java and in the DAGs; the only `fs.s3a.*` occurrences are two credential *key names* read by `HadoopBasedS3CredentialsProvider`) |

So: content retrieval from the object store is entirely SDK v1 and is self-contained in the
`iis-wf-import` uber JAR. Excluding SDK v2 only affects S3A, which nothing uses yet. The full set of
`com.amazonaws` usages in the repo is 3 classes, all in `importer/content`
(`ContentRetrievalContext`, `HadoopBasedS3CredentialsProvider`, `ObjectStoreContentProviderUtils`),
and all of the referenced classes are verifiably present in the `iis-wf-import` jar.

Gotcha: `s3://` is **not** a Hadoop scheme. Putting an object-store URL into a path param
(`-input`, `-outputRoot`, `-cacheRootDir`, …) fails with *No FileSystem for scheme "s3"* — such paths
must be `hdfs://…` or, if ever needed, `s3a://…`.

Effect on artifact size:

| Module | Unfiltered | After filters |
|---|---|---|
| `iis-wf-import` | ~1.1 GB | ~327 MB (incl. AWS SDK v1) |
| `iis-wf-metadataextraction` | ~1.1 GB | **~85 MB** (AWS SDK v1 excluded) |
| `iis-wf-report` | ~29 MB | ~29 MB (no AWS deps) |
