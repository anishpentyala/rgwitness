# rgwitness

Check Parquet row-group min/max bounds and null counts against the values actually decoded without filters. A file can be readable, and have min <= max, yet lose rows when a query engine uses incorrect footer statistics to skip data.

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
| 2 | INVALID | The file could not be admitted, decoded, or handled within the supported layout |
| 3 | INCOMPLETE | No contradiction found, but some claims or types could not be checked |

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

Large integer bounds use tagged decimal strings in JSON, for example `{"kind":"integer","value":"18446744073709551615"}`. Temporal bounds and observations use exact integer ticks, with a `comparison` object recording the representation and unit (`days`, `ms`, `us` or `ns`). This avoids truncating nanoseconds through Python datetime values and does not depend on pandas. Decimal and binary bounds also have lossless representations. Counts remain JSON integers. The Python API `audit_bytes(data)` returns native scalar values, with integer ticks for temporal columns; `rgwitness.report.json_report` prepares the JSON-safe form without changing comparison evidence.

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

## Scope and limits

Supported: flat scalar integer, boolean, UTF-8 string, binary, decimal, date, time and timestamp columns supported by the installed PyArrow decoder. Temporal comparisons use raw stored bounds and integer-cast decoded ticks. An unexpected temporal physical storage type is unchecked rather than compared in mismatched units. Float and null-type columns are reported as unchecked, so a mixed file cannot silently receive CONSISTENT. Floating point NaN and signed-zero ordering require a separate design and are deliberately excluded.

Nested layouts, duplicate field names, dotted field names and external column chunks are refused. The dotted-name restriction avoids ambiguity in reader column projection. No page-statistics, column-index, bloom-filter, checksum, encoding, sort-order or schema-evolution audit is claimed. A decoder's interpretation and row boundaries are still part of the evidence, not independently verified format parsing.

Admission policies: one regular local file, at most 64 MiB; at most 2,000,000 declared rows, 128 columns, 4,000,000 declared cells and 4,096 row-group/column pairs. The file is read into a bounded byte snapshot before parsing. Reads use nonblocking open where supported to refuse writerless FIFOs rather than wait on them. These limits are policy, not Parquet format limits. This is not a hostile-file sandbox, a decompression budget, a filesystem timeout or a memory/time guarantee. Do not use it on untrusted files without process isolation.

Validated locally on Linux CPython 3.10.12 with PyArrow 25.0.1. Other Python versions, operating systems and PyArrow versions have not yet been validated. The dependency range is deliberately narrow, not a compatibility claim for every release in it.

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

MIT license. No telemetry or network access is implemented by rgwitness.
