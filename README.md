# rgwitness

Check Parquet row-group min/max bounds and null counts against the values actually decoded without filters. For supported float storage, also compare present NaN counts. A file can be readable, and have min <= max, yet lose rows when a query engine uses incorrect footer statistics to skip data.

This is an experimental, read-only audit for local files. It does not repair files, run SQL, or claim that a query engine is correct.

## Run

```sh
python -m pip install .
rgwitness input.parquet
# or: python -m rgwitness input.parquet
```

Output is one JSON object. Exit codes:

| Code | Verdict | Meaning |
| --- | --- | --- |
| 0 | CONSISTENT | Every supported column/row-group claim checked agrees with the unfiltered scan, with no coverage gaps |
| 1 | CONTRADICTED | At least one recorded bound excludes a decoded value, a recorded null count differs, or another checked claim contradicts the scan |
| 2 | INVALID or output failure | The file could not be admitted, decoded, or handled within the supported layout, or the complete report could not be written |
| 3 | INCOMPLETE | No contradiction found, but some claims or types could not be checked |

The CLI explicitly flushes its report. If stdout is closed, full or otherwise fails, it exits 2 and may produce no complete JSON report. It cannot retract bytes already accepted by a pipe or file. The callable `main(argv)` returns 2 without replacing or closing caller-owned streams; the console/module process runner also prevents a failed buffered write from being retried during interpreter shutdown. Argument-parser usage errors retain argparse behavior. Linux CPython output-failure behavior was tested with a closed pipe and `/dev/full`; no Windows claim is made.

CONSISTENT is limited to these row-group claims. It is not a general Parquet validity certificate, proof of data provenance, or guarantee that filtered queries are safe.

## Example of the failure it detects

A synthetic column contains `[10, 20, null]`, but its footer claims a minimum of 15 and maximum of 20. The file decodes normally. A filter for 10 can skip the entire row group. rgwitness reports:

```json
{
  "verdict": "CONTRADICTED",
  "violation_count": 1,
  "unchecked_count": 0
}
```

The full report identifies row group 0, column `x`, and `lower_excludes_value`, with the claimed and observed bounds. The tests create this example by changing only the footer bytes of a real Parquet fixture, then verify the decoded rows are unchanged.

Large integer bounds use tagged decimal strings in JSON, for example `{"kind":"integer","value":"18446744073709551615"}`. Temporal bounds and observations use exact integer ticks, with a `comparison` object recording the representation and unit (`days`, `ms`, `us` or `ns`). This avoids truncating nanoseconds through Python datetime values and does not depend on pandas. Decimal and binary bounds also have lossless representations. Float report bounds use a tagged `float64` little-endian hex encoding of the Python comparison value, including infinities and signed zero, instead of nonstandard JSON `NaN` or `Infinity`. Float claims also retain their original stored bound bytes as `lower_raw_hex` and `upper_raw_hex`; `comparison.physical_width` distinguishes FLOAT from DOUBLE. A widened Python value is not a claim that decoded float32 NaN payload bits were preserved. Counts remain JSON integers. The Python API `audit_bytes(data)` returns native scalar values, with integer ticks for temporal columns; `rgwitness.report.json_report` prepares the JSON-safe form without changing comparison evidence.

## Input snapshot identity

Every completed audit report includes `input_snapshot.byte_length` and `input_snapshot.sha256`, the lowercase SHA-256 hex digest of the exact byte snapshot passed to the decoder, including its footer. The Python API, ordinary CLI and isolated worker produce the same identity. Renaming a file does not change it. Changing only a footer claim does, even if every decoded value remains the same.

Keep the identity with the report to match it to an input later, for example using `sha256sum input.parquet`. The file at that path might have changed since the audit; the report identifies the bytes read, not a continuing claim about the path. No pathname is added to the report. An INVALID error report has no snapshot identity. The digest is not a signature, a proof of origin, an independent decoder check or a stronger audit verdict.

## Explicit-file batch manifests

Audit a dataset split across several local files without losing a failed member:

```sh
rgwitness --batch part-000.parquet part-001.parquet part-002.parquet
# Apply the worker budget separately to each file:
rgwitness --batch --isolate --timeout 30 part-000.parquet part-001.parquet
```

`--batch` accepts 1 to 32 explicit file arguments. It emits one `rgwitness/batch-v1` JSON object with `file_count`, `verdict_counts` and a `files` list. Each entry has a one-based `position`, the `path` exactly as supplied and the complete ordinary single-file `report`, including snapshot identity when the audit completes. Unlike ordinary single-file reports, a batch manifest **contains input paths**. Review those paths before sharing it.

Files are audited sequentially in argument order. An unreadable or invalid input gets an INVALID entry; later files are still attempted. Duplicate paths are separate reads, not cached or deduplicated, so they can have different snapshot identities if the file changes. There is no directory crawl, sorting, tilde expansion or glob expansion inside rgwitness. Your shell may expand `*.parquet` before invoking it; use quotes for a literal filename. Use `--` before filenames that start with a dash.

The batch verdict and exit code use this precedence, regardless of argument order:

| Condition | Batch verdict | Exit |
| --- | --- | --- |
| Any member is INVALID | INVALID | 2 |
| No INVALID, at least one CONTRADICTED | CONTRADICTED | 1 |
| Neither above, at least one INCOMPLETE | INCOMPLETE | 3 |
| Every member is CONSISTENT | CONSISTENT | 0 |

A contradiction remains in its member report and in `verdict_counts` even when an invalid member makes the overall verdict INVALID. Empty batches, too many arguments and multiple files without `--batch` are usage errors. Output failures still exit 2; bytes already written cannot be retracted. Interrupting a batch before its final output does not emit a partial success manifest.

The Python API `rgwitness.batch.audit_files(paths, isolate=False, timeout=None, memory_mib=None)` returns the same JSON-safe manifest and accepts an iterable of string paths or string-valued path objects. It consumes at most 33 entries before rejecting an over-limit iterable, before opening any input. Invalid API options raise before auditing. Each accepted member retains the existing single-file admission policies. Worker timeout and memory limits apply **per file**, not to the entire batch. All reports are retained in parent memory until output; this does not cap parent memory or report size. The warnings in "Opt-in worker budgets" still apply.

This is a manifest of separate file audits, not an atomic dataset snapshot, a cross-file schema check or a guarantee about a query over the combined dataset. Existing single-file invocation and report shapes are unchanged. Batch behavior was validated on Linux CPython 3.10.12 with PyArrow 25.0.1 only.

## What is checked

- Each row group's declared row count against its unfiltered decoded count.
- Present null counts against the decoded null count. A missing count is not treated as zero.
- Present lower and upper bounds against decoded extrema, using the supported scalar type's order.
- Lower <= upper and bounds with no non-null values.
- Conservative bounds are accepted. A stored minimum smaller than the actual minimum is not a corruption by itself.
- Missing min/max with non-null values produces INCOMPLETE. An all-null column need not have min/max.

The reader is PyArrow's `ParquetFile.iter_batches`, with explicit row groups and no predicate. rgwitness does not ask a query engine to recompute min/max, because engines can answer those queries from the same suspect metadata.

## Temporal precision correction

The original implementation could silently report false CONSISTENT for time64 nanosecond bounds within one microsecond when pandas was installed. Without pandas, PyArrow 25.0.1 failed loudly on the original temporal conversion path with INVALID (exit 2), rather than producing that silent false verdict. Temporal comparisons now use raw stored bounds and decoded integer ticks, avoiding that conversion dependency. This correction was validated on Linux CPython with PyArrow 25.0.1 only; no wider runtime or platform compatibility is claimed.

## Float coverage

Flat FLOAT and DOUBLE columns with an explicit `TYPE_ORDER` are compared against unfiltered decoded values. NaNs are counted separately and excluded from extrema. Signed zeros compare equal under the format's compatibility rules, so a stored +0 minimum does not exclude -0, and a stored -0 maximum does not exclude +0. This does not implement IEEE 754 total order.

A present `nan_count` is checked, including an explicit zero. Missing `nan_count` always leaves INCOMPLETE unless another checked claim is contradicted. PyArrow 25.0.1's writer omitted this field in the fixtures tested here, even for NaN-free input. Ordinary files from that writer therefore remain INCOMPLETE; this addition does not silently certify them. Finite bounds that exclude decoded non-NaN values can still be reported as contradictions when the count is missing.

Missing bounds with non-NaN values leave gaps. NaN bounds are ignored and leave an explicit gap, rather than being used in numeric comparisons. An all-NaN or all-null group with accurate counts and no bounds can be CONSISTENT within this scope. Missing/unknown column order, `IEEE_754_TOTAL_ORDER`, and FLOAT16 storage remain unchecked. There is no NaN-payload-order, page-statistics or writer-conformance audit. Bound containment accepts compatible signed zeros even when a writer did not choose the recommended zero sign.

Float metadata comes from a small bounded Compact Thrift footer reader because the installed PyArrow metadata API does not expose NaN counts or column order. Raw evidence is checked against the decoder's group count, column count, physical type, column path and declared number of values before use. The data scan and its interpretation still depend on PyArrow. The reader is not an independent Parquet data-page decoder.

The comparison rules follow the [Parquet format statistics and column-order definitions](https://github.com/apache/parquet-format/blob/master/src/main/thrift/parquet.thrift). Footer encoding follows [Apache Thrift Compact Protocol](https://github.com/apache/thrift/blob/master/doc/specs/thrift-compact-protocol.md).

## Scope and limits

Supported: flat scalar integer, boolean, UTF-8 string, binary, decimal, date, time and timestamp columns supported by the installed PyArrow decoder, plus the restricted float coverage above. Temporal comparisons use raw stored bounds and integer-cast decoded ticks. An unexpected temporal physical storage type is unchecked rather than compared in mismatched units. Null-type columns remain unchecked, so a mixed file cannot silently receive CONSISTENT.

Nested layouts, duplicate field names, dotted field names and external column chunks are refused. The dotted-name restriction avoids ambiguity in reader column projection. No page-statistics, column-index, bloom-filter, checksum, encoding, sort-order or schema-evolution audit is claimed. A decoder's interpretation and row boundaries are still part of the evidence, not independently verified format parsing.

Admission policies: one regular local file, at most 64 MiB; at most 2,000,000 declared rows, 128 columns, 4,000,000 declared cells and 4,096 row-group/column pairs. The file is read into a bounded byte snapshot before parsing. Reads use nonblocking open where supported to refuse writerless FIFOs rather than wait on them. These limits are policy, not Parquet format limits. Files with float columns also have a 1 MiB footer limit, a 100,000-value Compact parsing budget and a nesting-depth limit of 32; even fields not used for comparison consume that budget. The footer reader refuses malformed fields, duplicate/nonpositive field IDs, overflow, trailing data and unsupported Compact types. These are admission policies, not universal format-validity judgments, and can reject files admitted by earlier releases. PyArrow reads metadata before these extra checks, so the new limits do not bound its initial parsing. This is not a hostile-file sandbox, a decompression budget, a filesystem timeout or a memory/time guarantee. Do not use it on untrusted files without process isolation.

Validated locally on Linux CPython 3.10.12 with PyArrow 25.0.1. Other Python versions, operating systems and PyArrow versions have not yet been validated. The dependency range is deliberately narrow, not a compatibility claim for every release in it.

## Opt-in worker budgets

The CLI can run the complete input read, native decoder import and audit in a child process:

```sh
rgwitness input.parquet --isolate --timeout 30
# Linux only: also cap the child's virtual address space at 4 GiB
rgwitness input.parquet --isolate --timeout 30 --memory-mib 4096
```

`--isolate` defaults to a 30-second wall-clock timeout. A timeout kills and reaps the direct child, discards any partial output and reports INVALID (exit 2). A crash, failed startup, malformed report or mismatched report/exit status also cannot produce a passing verdict. Complete reports retain the same JSON shape and exit codes as the in-process mode. The Python `audit_bytes` API and default CLI mode are unchanged.

`--memory-mib` applies Linux `RLIMIT_AS` before importing PyArrow. It limits virtual address space, not measured resident memory or a whole process tree. A lower inherited hard limit remains in force. Native libraries can reserve large mappings, so a budget that looks generous for a small file may still prevent imports. A budget failure can yield a generic INVALID error rather than a precise out-of-memory diagnosis. Other platforms reject this option rather than silently ignore it. Timeout values must be finite and positive; both budget options require `--isolate`.

This is process containment for accidental decoder stalls and address-space growth, **not a security sandbox**. The child inherits the caller's permissions, environment, working directory and filesystem/network access. There is no privilege boundary, syscall filter, descendant-process containment or global resource guarantee. The parent captures the child's report in memory; these options do not cap parent memory or all I/O. A parent timeout cannot guarantee interruption of an uninterruptible kernel wait. Do not use this mode as permission to open hostile files. Use an OS/container sandbox with separate resource and access controls for that purpose.

Worker behavior was tested on Linux CPython 3.10.12 with PyArrow 25.0.1: matching valid/contradicted/incomplete/invalid reports, real timed-out and killed children, rejection of partial pass output, generous/low address-space controls, and final stdout failure. No Windows/macOS worker or resource-limit compatibility is claimed.

## Related tools and outside evidence

- [Polars issue 16683](https://github.com/pola-rs/polars/issues/16683) records incorrect UInt64 statistics causing silent row-group pruning. It is evidence of the failure class, not a claim of a current Polars defect.
- [Apache parquet-cli](https://github.com/apache/parquet-java/blob/master/parquet-cli/README.md) has `check-stats`. Its current CheckParquet251Command targets suspect legacy binary-writer page statistics. rgwitness instead checks supported flat scalar row-group claims regardless of writer version. It does not replace the Apache tool's page checks.
- [pq by joewalnes](https://github.com/joewalnes/pq) and [pq by OrlovEvgeny](https://github.com/OrlovEvgeny/pq) offer broader inspection/validation workflows. Their inspected current validation implementations perform metadata sanity/readability checks, not this comparison of each stored bound against unfiltered decoded extrema.
- [parquet-analyzer](https://github.com/clee704/parquet-analyzer) is a deeper structural inspector; its documented validation is footer-only.

These are scope distinctions, not an exhaustive claim of novelty or a performance comparison.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests include real altered-footer bounds/null counts with unchanged decoded data, sub-microsecond time bounds that Python datetime values would truncate, exact temporal unit checks, conservative bounds, missing statistics, all-null inputs, multiple row groups, multiple batches, supported type families, exact JSON large integers, strict exit codes, writerless FIFO refusal on POSIX, and a deterministic 500-case comparison oracle.

Fixed real float controls in `tests/float_cases.py` were generated with PyArrow 25.0.1 and rewritten by an independent Apache Thrift 0.22.0 reader/writer, not the production footer reader. The original Parquet bytes are stored as base64 text constants and decoded in memory at test time, without changing the fixtures. The embedded manifest records expected verdicts and decoded physical-width bits. Tests verify decoded values are unchanged, including valid, narrow/wide bounds, NaN/null counts, all-NaN/all-null groups, infinities, signed zeros, partial bounds and unsupported orders. Fixture maintenance only: with `thrift==0.22.0` installed in a separate development environment, run `python tests/generate_float_fixtures.py`. Apache Thrift is not a runtime or ordinary test dependency.

MIT license. No telemetry or network access is implemented by rgwitness.
