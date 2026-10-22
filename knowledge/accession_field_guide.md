# Accession field guide

How submissions arrive in practice. A companion to
[`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md), which
covers what WOD requires. Each item below is a decision made while looking at the file; the
pipeline cannot infer it.

## 1. What not to add

Decide this before working on a folder: wasted staging is cheap, a wrong ingest is not.

| Data | Why not |
| --- | --- |
| Currents / ADCP | WOD does not hold currents; no Table 3a code. Keep the navigation track (§1.1). |
| Imagery (jpg, png, gif, mp4) | Not measurements. `process_accession` skips them (`config.NON_DATA_EXTENSIONS`); say in the report that the folder was imagery rather than leaving it in `skipped`. |
| Plankton biomass / taxonomy | Belongs in WOD through the Biological Header (Table 6a) and Taxa-Records (Table 7a), not the profile path. `wod-etl` writes Taxa-Records when the hauls can be located; see [`plankton_biomass.md`](plankton_biomass.md). Record gear, tow, mesh and preservative while the accession is open; only the submitter has them. |
| Bathymetry / soundings | Bottom depth is secondary header 10, per-cast metadata, not a measured variable. A sounding track has no profile in it. Not output, but useful (§1.1). |
| Nitrite, ammonium, CDOM, turbidity, PAHs, microplastics, eDNA | No Table 3a code. Nitrite is deliberately excluded (manual p.49). |
| Fish and mammal catch, seabird counts | Not ocean profile variables. |
| Anything entirely unanalysed | Count real values per column first. A file whose every cell reads `Not analyzed` has no data, whatever its columns say. |

Documentation files (readmes, cruise reports, format specs) are where coordinates, units and
sentinel legends are defined. Read them before deciding a dataset cannot be staged.

### 1.1 "Not a dataset" is not "not useful"

These folders produce no profile of their own, but can be the best evidence about the casts
you are adding. `classify_dataset` puts a `harvest` field on every exclusion, and
`classify_accession` collects it per folder. Nothing is harvested automatically: matching two
files on position and time is a judgement made during staging, with the source file cited in
`notes`.

| Folder | What it can supply |
| --- | --- |
| Multibeam / single-beam bathymetry | Secondary header 10 (bottom depth) at the cast position; the sea-floor relief that settles header 34 (bottom hit) |
| ADCP / SADCP | A timestamped navigation track: an independent clock to test a time convention (§5) |
| Cruise report, dive log | Instrument model, cruise identity, ship-log times |

**Bottom depth from a bathymetry grid.** Take the median of the soundings within a few hundred
metres of the cast, not the single nearest node, which is one beam over rough ground.
Ship-track multibeam is continuous, so a station usually sits within metres of a sounding.
Record the radius, the count and the observed range in `notes`: the value is derived and is
not in the profile file.

**Bottom hit (header 34).** A drop that ends short of the probe's terminal depth, at a depth
inside the local sea-floor relief, struck the sea floor. This changes how the deepest levels
are read. Establish the terminal depth from the accession's other casts (profiles all stopping
at the same round depth show the probe's limit), not from an unverified probe-type table.

**Do not invent codes.** Headers 3 (platform), 4 (institution) and 29 (probe type) take numeric
values from WOD's website code tables, not the manual (Appendix 2 lists only a subset). Write
the name the accession states and say it is a name, or leave the header out. Never guess a
probe type from a maximum depth.

## 2. Coordinates

### The forms, and what they mean

| Written as | Means | Decimal |
| --- | --- | --- |
| `49-46.9858N` | 49° 46.9858′ N: degrees, hyphen, decimal minutes, hemisphere | 49.783097 |
| `165-51.5694E` | 165° 51.5694′ E | 165.859490 |
| `62 9.6 N` | the same, space-separated | 62.16 |
| `62 09 36 N` | degrees, minutes, seconds | 62.16 |
| `62`,`9.6`,`N` | three separate columns | 62.16 |
| `33.5 S` | decimal degrees with a hemisphere letter | −33.5 |

The hyphen is a separator, not a minus sign. In this form the minutes field is always < 60,
and such files often carry a decimal column beside it: check `degrees + minutes/60` against
it.

`parse_coordinate` reads all of these. A hemisphere letter beats a leading minus: station
lists often write both, and the letter is what the originator meant.

### Non-English headers

Japanese-authored files are usually cp932. Read as utf-8 they raise `UnicodeDecodeError` and
look corrupt. `read_table` tries each of `config.TEXT_ENCODINGS` and reports which worked.

| Header | Meaning |
| --- | --- |
| 経度 | longitude |
| 緯度 | latitude |
| 度表記 | "expressed in degrees": a units note, not a variable |
| 水深 | water depth |

Do not assume latitude comes first: `Date, 経度, 緯度, 水深` puts longitude before latitude.
Swapping them puts casts on land, and a plausible-looking map is not proof. Check against
another file in the accession, or the sample-location file NCEI generates in `about/`.

## 3. Table shapes

### A second header row holding units

```
time (UTC),latitude,longitude,water temperature,conductivity,salinity,chlorophyll
,degree N,degree E,degree C,mS/cm,,ppb
2023/6/11 1:40,40.26220667,145.5238417,15.728,42.589,34.062,0.62
```

Unit detection reads the full column name and nothing else, so a units row left in place is
invisible and every value is taken as already standard. Fold it in with `fold_unit_row` before
staging; that is what makes `mg/l`, `ml/l` and `deg F` detectable.

### Fixed-width logger output with an undocumented flag column

```
# XBT  KM23-05                  20230303 2102 21-31.16N 144-01.84E 1831
        0.0      -5.00                             0N
        1.0      24.12                             0A
```

Underway loggers write a one-line header (cruise, date, time, position, level count), then
fixed-width levels with no column names or units. Before staging:

- The trailing header number is usually the level count, not a depth. Check it against the row
  count; a mismatch means a truncated file.
- An out-of-family value under a flag is a sentinel: `-5.00` in a 24–28 °C surface layer is
  not a measurement. Blank the value; keep the level and the flag.
- Undocumented flags stay untranslated. If neither the readme nor the cruise report defines the
  originator's flag alphabet, do not map it onto Table 12; flag codes are not standardised
  between submitters. Carry the column through staging verbatim so it lands in
  `unmapped_columns` and is reported, quote the per-cast distribution in `notes`, and let the
  pipeline compute WOD's own flags independently.

### Metadata blocks, separated by blank lines or not at all

Titles, legends and metadata blocks sit above or below the data, sometimes with no separator:

- A title row above the header: `read_table(header_row=1)`. The index counts non-blank rows,
  so blank lines do not shift it.
- A blank row between every data row (files written with `\r\r\n`). Undetected, this shifts
  the header and doubles the row count; `read_table` drops them and reports
  `blank_rows_dropped`.
- A legend at the foot (`*DL means "less than detection limit"...`). The text sits in the
  station column, so it groups like a station and a blank-key filter misses it. Drop a group
  whose position will not parse, and log what you dropped.
- Two datasets in one file, separated by a row of `,,,,,,`. Split on the separator and treat
  each as its own table. Nothing detects this automatically.

### Missing-value legends

`**NS` not sampled · `***NA` not analysed · `*DL` below detection limit · `**ND` not detected ·
`****ND` no data. The codes are not standardised across submitters; the legend is usually in
the last rows of the sheet or in the readme. `clean_sentinels` handles the known set and takes
`extra=` for the rest.

`*DL` and `**ND` carry real information: the value lies below a stated threshold. Table 12 has
no observed-level flag for this, so they still become missing, but quantify the loss in the
report and never substitute the detection limit.

## 4. One cast, many files

Both shapes give silently wrong output unless combined first.

- One file per depth: each file holds one level of one cast. Uncombined, this becomes many
  single-level profiles. Combine with `combine_levels`, then sort by depth.
- One file per variable: temperature, oxygen and chemistry in separate files for the same cast
  or track. Combine with `combine_on_key`, joining on depth for a profile or on timestamp for
  an underway track.

```python
cols, rows, info = combine_on_key([(c1, r1), (c2, r2)], key="Depth (m)",
                                  key_aliases=["DEPTH", "depth_m"])
```

Check `info["per_table"]`. A `matched_existing_keys` near zero means the files do not share a
key: the join produced disjoint blocks instead of merged levels, which looks like success. A
high match rate is what justifies the join.

The reverse, one file holding many casts, needs `group_rows`. A `Station` column with hundreds
of rows is the sign.

## 5. Dates and times

Everything must reach UTC. Sources arrive as UTC, as a local zone (ET, JST, ship's clock), or
as a local stamp beside a separate offset column. The last is ambiguous: a column labelled
`Date time (UTC)` next to a `Time zone` column must be tested, because the label is often
wrong.

**Verify rather than assume.** If the accession has an underway GPS track, check station times
against it: the correct interpretation puts the ship at the cast position. An ADCP folder you
are not ingesting still carries this track. Reduce it to one position per ensemble and keep
the reduced copy under `data/staging/` with its source URL, labelled as verification, not
output.

**A stationary ship makes the track test meaningless.** The test works because at the wrong
offset the ship would be elsewhere. Over a station held for hours, every offset matches to a
few hundred metres. Before trusting a match, check that the track moves across the offsets
compared, and that the cast time is not inside a gap in the track.

**A track test proves a shared clock, not the zone.** Both files may share the same non-UTC
clock. When nothing in the accession names a zone, say so in `notes` and in the report, state
which convention you recorded (for example, UTC as an assumption), and give the evidence you
have.

**A four-digit `HHMM` beside a date corrupts silently.** `strptime` reads `2102` under
`%H%M%S` as 21:00:02 instead of failing. `staging.parse_timestamp` punctuates 4- and 6-digit
time fields before parsing; a custom reader must do the same.

### "Julian" in WOD does not mean the Julian calendar

| Term | What it is |
| --- | --- |
| Julian year-day (Table 3a variable 32) | Day-of-year within one year, for SUR/APB/UOR only, where each observation carries its own time. Not a calendar. |
| Secondary header 99 | A time stamp written `YYYYJJJ`, where `JJJ` is the year-day. |
| Julian calendar | Not in the manual. Nor is "proleptic Gregorian". |

There is no Julian-calendar conversion. WOD stores a primary-header year/month/day/time with a
precision field, plus year-day as a measured variable in the three datasets that need
per-observation time.

- Year-day is specified inconsistently. Manual p.37 defines year-day 0.00 as 00:00 on
  1 January of the year of the first measurement, then gives an example (00:00 on 31 Dec 1965
  → 365.00) implying one more. Pick one, record it in the staged header, and flag it.
- A day of 0 is legal. The manual keeps such submissions as-is in case better information
  arrives. Do not change it to day 1.

## 6. NetCDF inputs

`read_any` reads NetCDF and HDF5, with or without an extension: one profile per file, many
profiles per file (Argo), and CF ragged arrays.

If a file seems to have no metadata, check its variables. Originators often store metadata as
variables, not attributes: a `station`, `cruise`, `platform` or `date` array of length
`n_casts` beside the data. A reader that only walks `ncattrs()` drops it. Check both; a scalar
or all-identical variable is usually metadata, not a measurement.

## 7. The intermediate format, and the single conversion routine

Every output file obeys the same contract whatever arrived, so one routine can convert it to
WOD binary without knowing about staging, encodings or sentinels.

| Aspect | Guarantee |
| --- | --- |
| Layout | CF-1.8 contiguous ragged array, `featureType = profile`. Dimensions `casts` and `z_obs`; `z_row_size` on `casts` with `sample_dimension = z_obs`. |
| Alignment | `z` and every measured variable live on the shared `z_obs` dimension, so a measurement is always index-aligned with its depth. Per-variable observation dimensions are a validation error. |
| Per cast | `lat`, `lon`, `time`, `cast_id` (`cf_role = profile_id`). |
| Per level | `z` (`positive = down`, metres), `z_qc`, one variable per WOD code, and a `<var>_qc` for each. |
| Identification | Every measured variable carries `wod_variable_code`; `units` as the UDUNITS string of the Table 3a standard unit (`degree_C`, `1`, `umol kg-1`) so CF readers parse it; `wod_units` with Table 3a's own wording (`Degrees Celsius`); and a CF `standard_name` where one exists. Values are already converted. Missing values are the `_FillValue`. |
| Flags | Table 12 values, with `flag_values` and `flag_meanings`. Nothing is ever deleted. |
| Metadata | `secondary_header_<id>_<name>` attributes, keyed by Table 4a ID. |
| Provenance | `unit_conversions`, and `seawater_density_kg_m3` + `seawater_density_provenance` whenever an assumed density was used. |

The manual specifies neither of the following; they are implementer's choices the converter
must be told:

- Time epoch: `time` is `days since 1770-01-01 00:00:00` with `calendar = "gregorian"`. WOD
  stores year/month/day, so the converter decodes it back. `"gregorian"` is a deprecated CF
  alias for `"standard"`; all WOD data postdates 1582, so it is numerically identical to
  `proleptic_gregorian`.
- Depth is not a variable: `z` is the vertical axis. WOD code 25 is Pressure, a separate
  measurement that may also be present.

Not yet done: interpolation to standard levels (Reiniger-Ross, p.56), the density check that
would set depth flag 2, and basin-aware range bounds. See *Known limitations* in the top-level
README.
