"""PDF vs PMC (JATS) execution modes for the metadata extraction cache workflows.

The Oozie workflows selected the extraction chain with ``metadata_extractor_app_name`` and then
relied on a commented block of ``job.properties`` overrides for everything else::

    #ingestpmc cache related
    #metadata_extractor_app_name=ingest_pmc_metadata
    #import_mode=xmlpmc
    #cache_report_relative_path=import_ingestpmc_cache
    #cache_report_properties_prefix=import.ingestPmc
    #cache_location=/user/dnet.production/iis/cache/ingestpmc
    #max_file_size_mb=2

Exposing those as independent Airflow parameters lets a run end up half-PMC: JATS records written
into the PDF cache, PDF report keys over PMC counters, or ``xmlpmc`` content urls fed to Grobid.
Here ``extraction_mode`` decides everything that is *intrinsic* to the mode, and nothing else.

What is derived, and what is not
---------------------------------
Derived -- a value can not be chosen independently of the mode without silently corrupting data:

  * which content url subdirectory is read (``pdf`` / ``xmlpmc``),
  * the content retrieval counter prefix (``import.contents.pdf`` / ``import.contents.jats``),
  * the report directories and report key prefixes (``import.metadataExtraction`` /
    ``import.ingestPmc``); they have to match the counters the jobs actually emit,
  * the content size cap (100 MB for PDF, 2 MB for PMC -- an Oozie legacy tied to the far more
    memory hungry PMC parser).

Deliberately **not** derived: ``cache_location``. The PDF and PMC caches are unrelated HDFS
locations (``/user/dnet.production/iis/cache/metadataextraction`` vs
``/user/dnet.production/iis/cache/ingestpmc``), not one directory nested in the other, so deriving
one from the other would encode a relation that does not exist. It is a plain DAG parameter with
**no default**: switching the mode can not silently reuse the previous mode's cache, because a run
without it refuses to start (:func:`validate_required_params`).

Sources of the preset values:

  * ``cache/builder/job.properties``               (the commented block above)
  * ``cache/identify_by_checksum/job.properties``   (that app already runs PMC by default)
  * ``ingest/pmc/metadata/oozie_app/workflow.xml``  (``report.import.ingestPmc.*`` counters)

The helpers return **Jinja expressions**, not values: the mode is a DAG *run* parameter, so the
concrete value can only be resolved when Airflow renders the task arguments.
"""

from airflow.exceptions import AirflowException

#: Mode identifiers. ``pmc`` also appears in a DAG run conf / CLI as ``-extractionMode pmc``.
PDF = "pdf"
PMC = "pmc"

#: Name of the DAG parameter holding the mode (shared by every DAG using this module).
MODE_PARAM = "extraction_mode"

#: Per-mode preset of the values the Oozie override block used to set by hand.
#:
#: Note the absence of a cache directory: the cache location is not mode-derived (see above).
PRESETS = {
    PDF: {
        "import_mode": "pdf",
        "content_report_properties_prefix": "import.contents.pdf",
        "cache_report_relative_path": "import_metadataextraction_cache",
        "extraction_report_relative_path": "import_metadataextraction",
        "report_properties_prefix": "import.metadataExtraction",
        "max_file_size_mb": "100",
    },
    PMC: {
        "import_mode": "xmlpmc",
        "content_report_properties_prefix": "import.contents.jats",
        "cache_report_relative_path": "import_ingestpmc_cache",
        "extraction_report_relative_path": "import_ingestpmc",
        "report_properties_prefix": "import.ingestPmc",
        "max_file_size_mb": "2",
    },
}


def _switch(pdf_value, pmc_value):
    """Jinja expression evaluating to ``pmc_value`` for pmc runs and ``pdf_value`` otherwise."""
    return "{{ '%s' if params.get('%s') == '%s' else '%s' }}" % (pmc_value, MODE_PARAM, PMC, pdf_value)


def mode_value(key):
    """Jinja expression with the pdf/pmc variant of preset ``key``."""
    return _switch(PRESETS[PDF][key], PRESETS[PMC][key])


def mode_or_param(param_name, key):
    """Jinja expression: the explicit run parameter wins, otherwise the mode preset.

    For genuinely run-specific knobs (currently only ``max_file_size_mb``) where the preset is a
    sensible default but a particular run may need another value.
    """
    return "{{ params.get('%s') or ('%s' if params.get('%s') == '%s' else '%s') }}" % (
        param_name, PRESETS[PMC][key], MODE_PARAM, PMC, PRESETS[PDF][key])


def validate_required_params(params):
    """Fail fast when the run did not state where the cache lives.

    ``cache_location`` is the one storage location that is not derived from the mode, and it has no
    default on purpose: a PMC run silently falling back to the PDF cache would inject JATS derived
    records into it. Raising turns that mistake into an immediate, named failure instead.

    Usable directly as a ``PythonOperator(python_callable=...)``: Airflow passes ``params`` from
    the task context because the parameter name matches.
    """
    if not (params.get("cache_location") or "").strip():
        raise AirflowException(
            "cache_location is required and has no default: the PDF and PMC caches are unrelated "
            "HDFS locations. Pass e.g. /user/dnet.production/iis/cache/metadataextraction for "
            "extraction_mode=pdf or /user/dnet.production/iis/cache/ingestpmc for "
            "extraction_mode=pmc."
        )
