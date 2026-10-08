# Large-Volume Migration System Guide

## Purpose

This guide describes how to operate the ETL system on the current workstation
for a migration of approximately 4–5 million records, processed in daily batches
of roughly 200,000–400,000 records.

The record count alone does not determine resource usage. Record width, long-text
fields, lookup transformations, retained artifacts, and verification strategy
all affect memory, disk usage, and processing time.

## Current workstation assessment

The following measurements were collected on July 31, 2026:

| Resource | Measured value | Assessment |
|---|---:|---|
| CPU | Intel Core i5-1345U | Good |
| Physical/logical cores | 10 / 12 | Good |
| CPU load during assessment | 6% | Good |
| RAM | 15.53 GB | Marginal |
| Free RAM during assessment | 2.22 GB / 14.27% | Too low for a large run |
| Disk | WD PC SN740 NVMe SSD | Good |
| Disk health | Healthy / OK | Good |
| Disk capacity | 474.72 GB | Good |
| Free disk | 303.35 GB / 63.9% | Good |
| Idle disk queue | 0.004 | Excellent |
| Workspace location | OneDrive-synchronized folder | Operational risk |

### Readiness conclusion

The CPU and NVMe disk are suitable for a chunked migration. RAM and OneDrive are
the principal constraints.

This machine can be used if:

- only one migration or verification process runs at a time;
- all processing remains chunked and bounded in memory;
- Bulk API results are streamed into DuckDB rather than accumulated in Python;
- unnecessary applications are closed before each run;
- OneDrive synchronization is paused while DuckDB databases are open;
- sufficient free disk space is maintained;
- the Windows pagefile remains enabled and system-managed.

The ETL must not materialize an entire 400,000-record batch as Python lists or
dictionaries. A daily batch is a logical unit; internally it must be processed
in much smaller chunks.

## Data flow and retained artifacts

The Data Seeding flow is:

```text
uploaded CSV
    ↓ validation and cleaning
cleaned CSV
    ↓ field mapping and ingestion
DuckDB raw_<Object>
    ↓ transformations and lookup resolution
DuckDB xfm_<Object>
    ↓ export/load
exported xfm CSV
    ↓ Bulk API
target Salesforce
```

A run may retain several representations of the same data:

- original uploaded CSV;
- accepted cleaned CSV;
- rejected CSV;
- DuckDB raw tables;
- DuckDB transformed tables;
- exported transformed CSV;
- downloaded target verification snapshot;
- summaries, mismatches, failures, and retry logs.

Actual storage can therefore be several times larger than the original upload.
Do not estimate production capacity using the current 18-record test run. Run a
representative test containing at least 10,000–25,000 realistic records.

## Recommended operating limits

For the current 16 GB workstation:

- Start with 5,000–10,000 records per internal processing chunk.
- Process one object at a time where possible.
- Run only one ETL or verification process.
- Avoid Python collections containing complete object datasets.
- Use DuckDB SQL joins for verification.
- Stream Bulk API query results into DuckDB or Parquet.
- Write mismatch results incrementally.
- Keep at least 100–150 GB free on the working disk.
- Try to make 6–8 GB of RAM available before starting.

Very wide objects such as Account and Case should be tested more conservatively
than narrow junction or reference objects.

## OneDrive precautions

The workspace currently resides under:

```text
C:\Users\puneetmalhotra\OneDrive - Nagarro\Desktop\VSTS\ETL\ETL_For_Local_Only
```

OneDrive can compete for disk, CPU, memory, and network bandwidth. Synchronizing
an actively changing DuckDB database may also cause file locking, duplicate
conflict files, or unnecessary full-file uploads.

At minimum:

1. Pause OneDrive before starting a migration or verification run.
2. Mark required ETL files as **Always keep on this device**.
3. Do not resume synchronization while DuckDB files are open.
4. Close the ETL process and DuckDB connections before resuming synchronization.

A safer long-term layout is:

```text
Code:
OneDrive\...\ETL_For_Local_Only

Active processing data:
C:\ETL_Data\state
C:\ETL_Data\exports
C:\ETL_Data\temp
C:\ETL_Data\verification
```

The application requires configurable data-root paths before adopting this
layout fully.

## Pre-run checklist

Before every production batch:

- [ ] Confirm OneDrive synchronization is paused.
- [ ] Close unnecessary Chrome, Edge, WebView, and VS Code processes.
- [ ] Confirm at least 6 GB of RAM is available where practical.
- [ ] Confirm at least 100–150 GB of disk space remains free.
- [ ] Confirm the pagefile is enabled and system-managed.
- [ ] Confirm no other migration or verification process is running.
- [ ] Confirm the correct source and target Salesforce profiles.
- [ ] Confirm the run-specific DuckDB and export paths.
- [ ] Confirm external IDs are populated and unique.
- [ ] Confirm a retry and rollback procedure exists.
- [ ] Start live resource monitoring.

## Check RAM

```powershell
$os = Get-CimInstance Win32_OperatingSystem

[pscustomobject]@{
    TotalRAM_GB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
    FreeRAM_GB = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
    UsedRAM_GB = [math]::Round(
        ($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1MB,
        2
    )
    FreeRAMPercent = [math]::Round(
        ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize) * 100,
        2
    )
} | Format-List
```

## Find high-memory applications

```powershell
Get-Process |
    Group-Object ProcessName |
    ForEach-Object {
        [pscustomobject]@{
            Process = $_.Name
            Instances = $_.Count
            TotalRAM_GB = [math]::Round(
                (($_.Group | Measure-Object WorkingSet64 -Sum).Sum / 1GB),
                2
            )
        }
    } |
    Sort-Object TotalRAM_GB -Descending |
    Select-Object -First 20 |
    Format-Table -AutoSize
```

The `CPU` value returned by `Get-Process` is cumulative processor time, not the
current CPU percentage.

## Check CPU

```powershell
Get-CimInstance Win32_Processor |
    Select-Object Name,
        NumberOfCores,
        NumberOfLogicalProcessors,
        LoadPercentage |
    Format-List
```

## Check disk capacity

```powershell
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" |
    Select-Object DeviceID,
        @{Name='TotalGB'; Expression={[math]::Round($_.Size / 1GB, 2)}},
        @{Name='FreeGB'; Expression={[math]::Round($_.FreeSpace / 1GB, 2)}},
        @{Name='FreePercent'; Expression={
            [math]::Round(($_.FreeSpace / $_.Size) * 100, 2)
        }} |
    Format-Table -AutoSize
```

## Check disk type and health

```powershell
Get-PhysicalDisk |
    Select-Object FriendlyName,
        MediaType,
        BusType,
        HealthStatus,
        OperationalStatus,
        @{Name='SizeGB'; Expression={[math]::Round($_.Size / 1GB, 2)}} |
    Format-Table -AutoSize
```

## Check the Windows pagefile

```powershell
Get-CimInstance Win32_ComputerSystem |
    Select-Object AutomaticManagedPagefile

Get-CimInstance Win32_PageFileUsage |
    Select-Object Name,
        AllocatedBaseSize,
        CurrentUsage,
        PeakUsage |
    Format-Table -AutoSize
```

The desired value is:

```text
AutomaticManagedPagefile : True
```

Do not disable the pagefile. It protects the run from an abrupt out-of-memory
failure, although sustained pagefile use will reduce performance.

## Live migration monitoring

Run this in a separate PowerShell window during migration:

```powershell
$etlRoot = 'C:\Users\puneetmalhotra\OneDrive - Nagarro\Desktop\VSTS\ETL\ETL_For_Local_Only'

while ($true) {
    Clear-Host

    $os = Get-CimInstance Win32_OperatingSystem
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"

    [pscustomobject]@{
        Time = Get-Date
        FreeRAM_GB = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
        FreeRAMPercent = [math]::Round(
            ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize) * 100,
            2
        )
        FreeDisk_GB = [math]::Round($disk.FreeSpace / 1GB, 2)
    } | Format-List

    Get-Process python, py -ErrorAction SilentlyContinue |
        Select-Object Id,
            @{Name='RAM_GB'; Expression={
                [math]::Round($_.WorkingSet64 / 1GB, 2)
            }},
            @{Name='Private_GB'; Expression={
                [math]::Round($_.PrivateMemorySize64 / 1GB, 2)
            }},
            CPU |
        Format-Table -AutoSize

    Start-Sleep -Seconds 10
}
```

Press `Ctrl+C` to stop.

## Live disk and memory counters

```powershell
Get-Counter -Counter @(
    '\Processor(_Total)\% Processor Time',
    '\Memory\Available MBytes',
    '\Memory\Committed Bytes',
    '\Memory\Pages/sec',
    '\PhysicalDisk(_Total)\% Disk Time',
    '\PhysicalDisk(_Total)\Avg. Disk Queue Length',
    '\PhysicalDisk(_Total)\Disk Read Bytes/sec',
    '\PhysicalDisk(_Total)\Disk Write Bytes/sec'
) -SampleInterval 5 -Continuous
```

Press `Ctrl+C` to stop.

## Warning thresholds

Investigate or pause the run when:

- free RAM remains below 1 GB;
- Python private memory rises continuously without returning;
- available RAM remains below approximately 5%;
- memory paging remains high for an extended period;
- free disk space approaches 100 GB;
- disk queue length remains consistently high;
- DuckDB, CSV, or log files grow unexpectedly;
- OneDrive begins synchronizing active processing files;
- Salesforce Bulk API jobs show persistent throttling or failures.

The idle disk queue observed during assessment was approximately `0.004`.
Production measurements must be collected while an actual representative run is
active.

## Measure ETL storage

Always set the correct workspace explicitly. Do not run relative-path reports
from `C:\Windows\System32`.

```powershell
$etlRoot = 'C:\Users\puneetmalhotra\OneDrive - Nagarro\Desktop\VSTS\ETL\ETL_For_Local_Only'
Set-Location -LiteralPath $etlRoot

'exports', 'state', 'logs', 'sample_data' | ForEach-Object {
    $path = Join-Path $etlRoot $_
    $files = if (Test-Path -LiteralPath $path) {
        Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue
    } else {
        @()
    }
    $stats = $files | Measure-Object -Property Length -Sum

    [pscustomobject]@{
        Directory = $_
        Files = $stats.Count
        SizeMB = [math]::Round($stats.Sum / 1MB, 2)
        SizeGB = [math]::Round($stats.Sum / 1GB, 3)
    }
} | Format-Table -AutoSize
```

## Capacity benchmark

Before production, run at least one representative dataset containing
10,000–25,000 realistic records. Capture:

- record and field counts;
- uploaded size;
- cleaned size;
- transformed size;
- DuckDB peak size;
- verification snapshot size;
- temporary peak disk usage;
- peak Python memory;
- elapsed ingestion, transformation, loading, and verification time;
- Salesforce success and failure counts.

Calculate:

```text
bytes per record = total run footprint / successfully processed records

estimated daily storage = bytes per record × 400,000

required working storage =
    estimated daily storage
    × concurrently retained days
    × 1.5 safety factor
```

Perform separate benchmarks for narrow and wide objects. Do not use one average
for every Salesforce object.

## Post-run checklist

- [ ] Confirm uploaded, cleaned, and transformed counts.
- [ ] Confirm Bulk API attempted, succeeded, and failed counts.
- [ ] Confirm the target-presence count.
- [ ] Review missing, extra, duplicate, and mismatched records.
- [ ] Confirm retry files are preserved.
- [ ] Close all DuckDB connections.
- [ ] Generate and preserve the run manifest and checksums.
- [ ] Archive the run before deleting reproducible intermediates.
- [ ] Resume OneDrive only after active databases are closed.

## Retention guidance

During an active run, retain all artifacts. After successful reconciliation:

- retain the uploaded source files;
- retain transformed load payloads;
- retain rejected and failed records;
- retain configuration/template snapshots;
- retain summary, mismatch, and reconciliation reports;
- retain checksums and run identifiers;
- compress or convert large archival files to an appropriate format;
- remove reproducible target snapshots and DuckDB intermediates only under an
  approved retention policy.

No automated cleanup should occur until the run has passed reconciliation and
the archive has been verified.
